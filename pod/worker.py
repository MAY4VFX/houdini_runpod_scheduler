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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

START = time.time()

# Root the /files endpoint is confined to. Tests monkeypatch this constant
# (module-level, read at request time) to point at a tmp_path sandbox.
WORKSPACE_ROOT = "/workspace"

# Where detached `/exec` runs keep their script, log and exit-code files.
# Under WORKSPACE_ROOT on purpose: the volume outlives the pod *and* the
# `/files` endpoint can already serve from there, so a caller polls a
# detached run with the same read path it uses for anything else.
EXEC_DIR_REL = ".rpfarm/exec"

# How long DetachedRuns.status() will wait for a finished run's wrapper to
# actually exit once its .rc file has appeared. The wrapper writes .rc and
# then runs `exit $rc`, so the two are never simultaneous -- but the gap is
# microseconds, and this is only ever paid inside it. See status() for why
# reporting "done" without reaping is the bug.
DETACHED_REAP_GRACE_S = 2.0

# Hard ceiling on the *synchronous* `/exec` path (Ruling R31). RunPod's
# pod proxy sits behind Cloudflare, which cuts a response at roughly 100
# seconds -- measured live in Task 14, where a 4.35GB Houdini install
# asked for 829s and got a literal "error code: 524" at 125s. Anything
# above this ceiling is a request the transport cannot deliver, so the
# handler clamps to it rather than pretending otherwise.
EXEC_SYNC_CEILING_S = 90


def get_gpu_info():
    """Query GPU information via nvidia-smi.

    Adapted from v1's worker/heartbeat.py::_get_gpu_info -- same body, same
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
    """Construct the subprocess environment (adapted from v1's worker/executor.py::_build_env).

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
        # in-memory tail, mirroring v1's worker/executor.py Popen loop.
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


def _kill_process_group(proc):
    """SIGKILL a timed-out `/exec` command and everything it started.

    The command runs with ``start_new_session=True``, so its children share
    its process group and one ``killpg`` takes the lot. Killing only the
    direct child (what ``subprocess.run``'s own timeout does) leaves
    grandchildren running against a closed stdout pipe -- how Task 14's
    Houdini install ended up half-written -- and later reparented to the
    worker as unreaped zombies.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:  # pragma: no cover - best effort reaping
        pass


def _under_workspace(path):
    root = os.path.realpath(WORKSPACE_ROOT)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


class DetachedRuns:
    """Detached `/exec` runs (Ruling R31).

    The synchronous `/exec` path cannot carry a long command: RunPod's
    proxy cuts the HTTP response at ~100s, and `subprocess.run(timeout=)`
    SIGKILLs the shell it spawned and closes its stdout pipe -- which does
    not stop a grandchild like SideFX's `houdini.install`, it just makes
    the grandchild die of SIGPIPE at its next progress write. Task 14 lost
    a Houdini install to exactly that and got a partial tree that looked
    complete on disk.

    A detached run therefore gets:

    - its own **file** for stdout/stderr, never the HTTP pipe, so nothing
      the request does can SIGPIPE it;
    - its own session (``start_new_session``), so no signal aimed at the
      request's process group reaches it;
    - an exit code written to ``<handle>.rc`` by the wrapper itself, and
      read back through the existing ``/files`` endpoint. A *completed*
      result therefore survives a worker restart. A run still in flight
      does not: the container restarting kills it, and since only the
      wrapper ever writes ``.rc``, the new instance finds no exit code and
      reports ``running`` forever. A caller that must tolerate that needs
      its own deadline (``exec_wait`` has one) rather than waiting on this
      endpoint indefinitely;
    - a live ``Popen`` this class reaps in :meth:`status`, so a finished
      child does not sit around as a zombie (as PID 1 in the container the
      worker inherits orphans and never reaps them; that zombie was the
      fingerprint that identified the bug).
    """

    def __init__(self, exec_dir, runner=subprocess.Popen):
        self.exec_dir = exec_dir
        self._runner = runner
        self._procs = {}
        self._lock = threading.Lock()

    def paths(self, handle):
        base = os.path.join(self.exec_dir, handle)
        return {"script": base + ".sh", "log": base + ".log", "rc": base + ".rc"}

    def start(self, command, handle=None):
        # NOTE: no uniqueness guard on a caller-supplied `handle`. Nothing
        # in this repo passes one (they are all generated below), but a
        # future caller reusing a live handle would truncate that run's log
        # and drop its Popen from the table, orphaning it. If explicit
        # handles ever get used, this wants a DuplicateTaskError-style
        # refusal like Registry.submit has.
        handle = handle or "exec-{}".format(uuid.uuid4().hex[:16])
        paths = self.paths(handle)
        os.makedirs(self.exec_dir, exist_ok=True)

        # The wrapper writes its own exit code, and writes it *last*, so a
        # present .rc always means "finished" and never "finished, but the
        # code is still on its way".
        with open(paths["script"], "w") as f:
            # The command runs in a subshell so that an `exit` inside it --
            # ordinary in an install script -- ends the command, not the
            # wrapper, which would skip the .rc write and leave the run
            # looking forever "running". (Caught by
            # test_exec_detached_outlives_the_request_and_reports_its_exit_code.)
            f.write(
                "#!/bin/bash\n"
                + "(\n"
                + build_shell_command(command)
                + "\n)\n"
                + "rc=$?\n"
                + 'printf %s "$rc" > {}.tmp\n'.format(paths["rc"])
                + "mv {0}.tmp {0}\n".format(paths["rc"])
                + "exit $rc\n"
            )
        os.chmod(paths["script"], 0o755)

        log = open(paths["log"], "wb")
        try:
            proc = self._runner(
                ["bash", paths["script"]],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=build_env({}),
                start_new_session=True,
            )
        finally:
            log.close()
        with self._lock:
            self._procs[handle] = proc
        return {"handle": handle, "log_path": paths["log"], "rc_path": paths["rc"]}

    @staticmethod
    def _reaped(proc, wait):
        """Has ``proc`` been reaped? With ``wait``, give it a moment first."""
        if proc.poll() is not None:
            return True
        if not wait:
            return False
        try:
            proc.wait(timeout=DETACHED_REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            return False
        return True

    def status(self, handle):
        paths = self.paths(handle)
        if not os.path.isfile(paths["script"]):
            return None

        with self._lock:
            proc = self._procs.get(handle)

        # The .rc file is the authority on the *code*: it is what survives a
        # worker restart, and it is written only once the command is done.
        exit_code = None
        try:
            with open(paths["rc"]) as f:
                exit_code = int(f.read().strip())
        except (OSError, ValueError):
            exit_code = None

        # ... but it is not on its own the authority on "finished". The
        # wrapper writes .rc and only *then* runs `exit $rc`, so between
        # those two there is a window in which the file already says the run
        # is over while the bash that wrote it is still alive -- and
        # therefore has not been reaped. Reporting "done" from inside that
        # window hands the caller a result and leaves a zombie behind, which
        # is precisely the fingerprint Task 14 chased (`<defunct>` with
        # PPID 1, because the worker is PID 1 in the container and inherits
        # orphans without reaping them). So when .rc is there, wait for the
        # wrapper to actually go; if it somehow does not, say "running"
        # rather than claim a completion nobody has collected.
        #
        # proc is None after a worker restart -- there is no child of ours to
        # reap and .rc alone is all the evidence there is, which is the whole
        # point of writing it to the volume.
        if proc is not None and not self._reaped(proc, wait=exit_code is not None):
            exit_code = None

        running = exit_code is None
        return {
            "handle": handle,
            "state": "running" if running else "done",
            "exit_code": exit_code,
            "log_path": paths["log"],
            "rc_path": paths["rc"],
            # The wrapper's pid, or None after a worker restart. Diagnostic:
            # it is what an operator greps for when a run looks stuck on a
            # pod, and what a test uses to ask about *this* run's child
            # rather than about every child the process happens to have.
            "pid": proc.pid if proc is not None else None,
        }


def make_server(host, port, log_dir="/workspace/ledger/logs"):
    token = os.environ.get("RPFARM_TOKEN", "")
    role = os.environ.get("RPFARM_ROLE", "gpu")
    slots = int(os.environ.get("RPFARM_SLOTS", "1"))
    registry = Registry(slots, log_dir)
    detached = DetachedRuns(os.path.join(WORKSPACE_ROOT, EXEC_DIR_REL))

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
            if parts[:1] == ["exec"] and len(parts) == 2:
                return self._handle_exec_status(parts[1])
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
            """Run a command on the sync pod.

            **This endpoint is for SHORT commands only.** The reply travels
            back through RunPod's pod proxy, which sits behind Cloudflare
            and cuts a response at roughly 100 seconds -- past that the
            caller gets an HTML "error code: 524" no matter what
            ``timeout_s`` said. Worse, the timeout here is enforced with
            ``subprocess.run(timeout=)``, which SIGKILLs only the shell it
            started: a grandchild (an installer, a big tar) survives that
            and then dies of SIGPIPE the next time it writes to the pipe
            this handler just closed, leaving a half-finished job that can
            look complete on disk. ``timeout_s`` is therefore clamped to
            :data:`EXEC_SYNC_CEILING_S`.

            Pass ``detach: true`` for anything that can run for minutes
            (Ruling R31). The command is then written to a script, started
            in its own session with its output going to a *file*, and the
            call returns ``202`` immediately with ``handle``/``log_path``/
            ``rc_path``; poll ``GET /exec/<handle>`` for the exit code and
            read the log through ``GET /files``.
            """
            body = self._read_json_body()
            if role != "sync":
                return self._send_json(403, {"error": "exec only on sync pod"})
            if body is None:
                return self._send_json(400, {"error": "invalid JSON"})
            command = body.get("command", "")

            if body.get("detach"):
                try:
                    started = detached.start(command, handle=body.get("handle"))
                except OSError as e:
                    return self._send_json(500, {"error": "could not start: {}".format(e)})
                return self._send_json(202, started)

            timeout_s = min(body.get("timeout_s", 600), EXEC_SYNC_CEILING_S)
            # Own session, so a timeout can kill the whole process *group*.
            # `subprocess.run`'s timeout kills only the direct child, which
            # is what let Task 14's installer keep running as an orphan and
            # then die of SIGPIPE half-done; verified live afterwards -- a
            # clamped-out `sleep` was still sitting on the pod as a zombie.
            proc = subprocess.Popen(
                ["bash", "-c", build_shell_command(command)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=build_env({}),
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout_s)
                self._send_json(
                    200,
                    {
                        "exit_code": proc.returncode,
                        "stdout": stdout[-200000:],
                        "stderr": stderr[-20000:],
                    },
                )
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                self._send_json(
                    504,
                    {
                        "error": "timeout",
                        "hint": "exec is capped at {}s by the RunPod proxy; use detach:true".format(
                            EXEC_SYNC_CEILING_S
                        ),
                    },
                )

        def _handle_exec_status(self, handle):
            if role != "sync":
                return self._send_json(403, {"error": "exec only on sync pod"})
            status = detached.status(handle)
            if status is None:
                return self._send_json(404, {"error": "no such handle"})
            self._send_json(200, status)

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
