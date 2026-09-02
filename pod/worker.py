"""RunPodFarm pod worker: tiny stdlib HTTP daemon over subprocess tasks.

Runs on every pod (the CPU "sync" pod and the GPU render pods) and is the
only control channel from the artist's Houdini session to the pod: submit a
shell task, poll its status, read its log, run a helper command (sync pod
only), and read a file under the shared workspace volume.

stdlib only -- this file is deployed as-is to Ubuntu 22.04 / python3.10 pods,
so it must not depend on anything outside the standard library and must not
use syntax newer than 3.10 (``str | None`` annotations are fine only because
of the ``from __future__ import annotations`` import below).

``task_id`` is a one-shot key: ``POST /tasks`` rejects (409) any ``task_id``
already present in this pod's registry, whether that earlier task is still
running or has already finished. Callers must mint a fresh ``task_id`` for
every run (e.g. include a retry counter) -- this registry never reuses or
overwrites an entry, so a resubmitted id can't orphan an in-flight process,
corrupt its log file, or let ``busy`` undercount the true number of running
tasks against ``RPFARM_SLOTS``.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

START = time.time()

# Root the /files endpoint is confined to. Tests monkeypatch this constant
# (module-level, read at request time) to point at a tmp_path sandbox.
WORKSPACE_ROOT = "/workspace"


def get_gpu_info():
    """Query GPU information via nvidia-smi.

    Adapted from worker/heartbeat.py::_get_gpu_info -- same body, same
    output shape. Returns an empty list when nvidia-smi is unavailable.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append(
                    {
                        "name": parts[0],
                        "memory_total_mb": int(parts[1]),
                        "memory_used_mb": int(parts[2]),
                        "utilization_pct": int(parts[3]),
                        "temperature_c": int(parts[4]),
                    }
                )
        return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("nvidia-smi unavailable: %s", exc)
        return []


def build_env(task_env=None):
    """Construct the subprocess environment (adapted from worker/executor.py::_build_env).

    Priority (highest wins): explicit task env overrides > Houdini PATH
    prepend > inherited process environment (which already carries HFS,
    set by the pod's own environment).
    """
    env = os.environ.copy()
    hfs = env.get("HFS", "")
    if hfs:
        env["PATH"] = os.path.join(hfs, "bin") + ":" + env.get("PATH", "/usr/bin:/bin")
    env.update(task_env or {})
    return env


def build_shell_command(command):
    """Wrap a command so the Houdini environment is sourced first (ruling R5).

    Uses the literal shell variable ``$HFS`` -- it is expanded by bash from
    the environment passed to Popen/run, not substituted by Python. An empty
    HFS simply makes the ``if`` false. Never pipe ``source
    houdini_setup_bash`` into anything: that would run it in a subshell and
    lose PATH/HFS, so it is sourced directly via pushd/popd, no ``cd -``.
    """
    return (
        'if [ -f "$HFS/houdini_setup_bash" ]; then '
        'pushd "$HFS" >/dev/null && source houdini_setup_bash >/dev/null 2>&1; '
        'popd >/dev/null; fi; ' + command
    )


class DuplicateTaskError(Exception):
    """Raised by Registry.submit when task_id is already present in the registry."""


class Task:
    """State for one submitted task."""

    def __init__(self, task_id, command, env, cwd, log_path):
        self.id = task_id
        self.command = command
        self.env = env
        self.cwd = cwd
        self.log_path = log_path
        self.state = "running"
        self.exit_code = None
        self.started = time.time()
        self.ended = None
        self.tail = collections.deque(maxlen=50)
        self.proc = None
        self.kill_requested = False
        self.state_lock = threading.Lock()


class Registry:
    """Tracks tasks and enforces the pod's slot limit."""

    def __init__(self, slots, log_dir):
        self.slots = slots
        self.log_dir = log_dir
        self.tasks = {}
        self.lock = threading.Lock()

    def busy(self):
        return sum(1 for t in self.tasks.values() if t.state == "running")

    def submit(self, body):
        with self.lock:
            task_id = body["task_id"]
            if task_id in self.tasks:
                raise DuplicateTaskError(task_id)
            if self.busy() >= self.slots:
                return None
            log_path = body.get("log_path") or os.path.join(self.log_dir, f"{task_id}.log")
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            task = Task(task_id, body["command"], body.get("env") or {}, body.get("cwd"), log_path)
            self.tasks[task_id] = task
        threading.Thread(target=self._run, args=(task,), daemon=True).start()
        return task

    def _run(self, task):
        # Streams stdout+stderr line by line to the log file and the
        # in-memory tail, mirroring worker/executor.py's Popen loop.
        try:
            with open(task.log_path, "w") as log:
                proc = subprocess.Popen(
                    ["bash", "-c", build_shell_command(task.command)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=build_env(task.env),
                    cwd=task.cwd or None,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                task.proc = proc
                if task.kill_requested:
                    self._terminate(proc)
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    log.write(line + "\n")
                    log.flush()
                    task.tail.append(line)
                proc.stdout.close()
                proc.wait()
            task.exit_code = proc.returncode
        except Exception as exc:  # keep the pod alive even if a task blows up
            logger.exception("task %s crashed", task.id)
            task.exit_code = -1
            task.tail.append(f"[worker] error: {exc}")
        finally:
            task.ended = time.time()
            with task.state_lock:
                if task.state == "running":
                    task.state = "succeeded" if task.exit_code == 0 else "failed"

    def kill(self, task):
        with task.state_lock:
            task.kill_requested = True
            if task.state == "running":
                task.state = "killed"
        proc = task.proc
        if proc is not None and proc.poll() is None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc):
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def _force_kill():
            if proc.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        threading.Timer(10, _force_kill).start()


def _under_workspace(path):
    root = os.path.realpath(WORKSPACE_ROOT)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def make_server(host, port, log_dir="/workspace/ledger/logs"):
    token = os.environ.get("RPFARM_TOKEN", "")
    role = os.environ.get("RPFARM_ROLE", "gpu")
    slots = int(os.environ.get("RPFARM_SLOTS", "1"))
    registry = Registry(slots, log_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence default stderr logging
            pass

        def _send_json(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, code, text):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self):
            """Parse the request body as JSON. Returns None on malformed JSON
            (caller must check for None and answer 400 instead of raising)."""
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        def _drain_body(self):
            # Discard any unread request body so a following request on the
            # same keep-alive connection doesn't get misread as body bytes.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)

        def _authorized(self):
            return self.headers.get("X-RPFarm-Token") == token

        def _require_auth(self):
            if not self._authorized():
                self._drain_body()
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        # -- routing -----------------------------------------------------

        def do_GET(self):
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.path == "/health":
                return self._handle_health()
            if parts[:1] == ["tasks"] and len(parts) == 2:
                return self._handle_get_task(parts[1])
            if parts[:1] == ["tasks"] and len(parts) == 3 and parts[2] == "log":
                return self._handle_get_log(parts[1])
            if parsed.path == "/files":
                path = (parse_qs(parsed.query).get("path") or [""])[0]
                return self._handle_get_file(path)
            self._send_json(404, {"error": "no route"})

        def do_POST(self):
            if not self._require_auth():
                return
            if self.path == "/tasks":
                return self._handle_post_task()
            if self.path == "/exec":
                return self._handle_exec()
            self._send_json(404, {"error": "no route"})

        def do_DELETE(self):
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if parts[:1] == ["tasks"] and len(parts) == 2:
                return self._handle_kill(parts[1])
            self._send_json(404, {"error": "no route"})

        # -- handlers ------------------------------------------------------

        def _handle_health(self):
            hfs = os.environ.get("HFS", "")
            houdini_ok = bool(hfs) and os.path.exists(os.path.join(hfs, "bin", "hython"))
            self._send_json(
                200,
                {
                    "role": role,
                    "pod_id": os.environ.get("RUNPOD_POD_ID", ""),
                    "slots": slots,
                    "busy": registry.busy(),
                    "gpus": get_gpu_info(),
                    "uptime_s": int(time.time() - START),
                    "hfs": hfs,
                    "houdini_ok": houdini_ok,
                },
            )

        def _handle_get_task(self, task_id):
            task = registry.tasks.get(task_id)
            if task is None:
                return self._send_json(404, {"error": "unknown task"})
            ended = task.ended if task.ended is not None else time.time()
            self._send_json(
                200,
                {
                    "state": task.state,
                    "exit_code": task.exit_code,
                    "started": task.started,
                    "ended": task.ended,
                    "duration_s": ended - task.started,
                    "tail": list(task.tail),
                },
            )

        def _handle_get_log(self, task_id):
            task = registry.tasks.get(task_id)
            if task is None:
                return self._send_json(404, {"error": "unknown task"})
            if os.path.exists(task.log_path):
                with open(task.log_path, "r", errors="replace") as f:
                    return self._send_text(200, f.read())
            return self._send_text(200, "\n".join(task.tail))

        def _handle_get_file(self, path):
            if not path or not _under_workspace(path):
                return self._send_json(403, {"error": "forbidden"})
            real = os.path.realpath(path)
            if not os.path.isfile(real):
                return self._send_json(404, {"error": "not found"})
            with open(real, "r", errors="replace") as f:
                return self._send_text(200, f.read())

        def _handle_post_task(self):
            body = self._read_json_body()
            if body is None:
                return self._send_json(400, {"error": "invalid JSON"})
            if not body.get("task_id") or not body.get("command"):
                return self._send_json(400, {"error": "task_id and command required"})
            try:
                task = registry.submit(body)
            except DuplicateTaskError:
                return self._send_json(409, {"error": "task_id already exists"})
            if task is None:
                return self._send_json(429, {"error": "busy"})
            self._send_json(202, {"task_id": task.id})

        def _handle_exec(self):
            body = self._read_json_body()
            if role != "sync":
                return self._send_json(403, {"error": "exec only on sync pod"})
            if body is None:
                return self._send_json(400, {"error": "invalid JSON"})
            command = body.get("command", "")
            timeout_s = body.get("timeout_s", 600)
            try:
                result = subprocess.run(
                    ["bash", "-c", build_shell_command(command)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=build_env({}),
                    text=True,
                    timeout=timeout_s,
                )
                self._send_json(
                    200,
                    {
                        "exit_code": result.returncode,
                        "stdout": result.stdout[-200000:],
                        "stderr": result.stderr[-20000:],
                    },
                )
            except subprocess.TimeoutExpired:
                self._send_json(504, {"error": "timeout"})

        def _handle_kill(self, task_id):
            task = registry.tasks.get(task_id)
            if task is None:
                return self._send_json(404, {"error": "unknown task"})
            registry.kill(task)
            self._send_json(200, {"state": task.state})

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    _port = int(os.environ.get("RPFARM_PORT", "8000"))
    make_server("0.0.0.0", _port).serve_forever()
