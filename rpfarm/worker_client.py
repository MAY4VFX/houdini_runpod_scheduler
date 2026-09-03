"""Stdlib-only HTTP client for the per-pod worker daemon (``pod/worker.py``).

Talks to a pod's ``https://<podId>-8000.proxy.runpod.net`` RunPod HTTP proxy.
Stdlib-only for the same reason as :mod:`rpfarm.runpod_api` (runs inside
Houdini's bundled Python too). All network I/O goes through an injectable
``transport(method, url, body, headers, timeout=30) -> (status, bytes)``
callable so tests never make real HTTP calls.

The proxy sits behind Cloudflare, which 403s Python's default User-Agent, so
every request carries an explicit ``User-Agent: rpfarm/<VERSION>`` header.
While a pod's 8000/http port isn't up yet, the proxy answers with a 404 HTML
page rather than anything from the worker -- callers that only care "is it
up" (``health``) must treat any non-200 or non-JSON body as "not yet", not
as an error.

``submit()`` returns one of ``"accepted"`` (202), ``"busy"`` (429 -- the pod
is at its slot limit, retry later), or ``"duplicate"`` (409 -- the task_id
was already used, a caller bug that will never resolve by retrying); any
other status or a transport-level failure raises :class:`WorkerError`
(ruling R17).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION, tls


def _urllib_transport(method, url, body, headers, timeout=30):
    """Default transport: a plain ``urllib`` request.

    ``timeout`` (socket timeout, seconds) is supplied by the caller -- 30s
    for most calls, or ``timeout_s + 10`` for ``/exec`` (see
    ``WorkerClient.exec``), long enough for the worker's own
    ``subprocess.run(..., timeout=timeout_s)`` to hit its timeout and reply
    with a 504 before our socket gives up.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls.ssl_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class WorkerError(Exception):
    """Raised by ``submit()`` for any response other than 202/429/409, and
    for a transport-level failure (network error, no response at all)."""

    def __init__(self, status, body):
        super().__init__(f"worker error, status={status}: {body!r}")
        self.status = status
        self.body = body


class WorkerClient:
    def __init__(self, pod_id, token, transport=_urllib_transport):
        self.pod_id = pod_id
        self.token = token
        self.base = f"https://{pod_id}-8000.proxy.runpod.net"
        self._transport = transport

    def _headers(self):
        return {
            "X-RPFarm-Token": self.token,
            "Content-Type": "application/json",
            "User-Agent": f"rpfarm/{VERSION}",
        }

    def _call(self, method, path, body=None, timeout=30):
        """Return ``(status, raw_bytes)``, or ``(None, None)`` on a network
        error (connection refused, DNS failure, timeout, ...)."""
        url = self.base + path
        try:
            return self._transport(method, url, body, self._headers(), timeout=timeout)
        except OSError:
            return None, None

    def _json(self, method, path, body=None, ok_status=200):
        status, raw = self._call(method, path, body)
        if status != ok_status or not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    # -- calls -------------------------------------------------------------

    def health(self) -> dict | None:
        return self._json("GET", "/health")

    def submit(self, task_id, command, env, cwd=None, log_path=None) -> str:
        """Submit a task. Returns ``"accepted"``, ``"busy"``, or
        ``"duplicate"``; raises :class:`WorkerError` on anything else."""
        body = {"task_id": task_id, "command": command, "env": env}
        if cwd is not None:
            body["cwd"] = cwd
        if log_path is not None:
            body["log_path"] = log_path
        status, raw = self._call("POST", "/tasks", body)
        if status == 202:
            return "accepted"
        if status == 429:
            return "busy"
        if status == 409:
            return "duplicate"
        raise WorkerError(status, raw)

    def status(self, task_id) -> dict | None:
        return self._json("GET", f"/tasks/{task_id}")

    def log(self, task_id) -> str:
        status, raw = self._call("GET", f"/tasks/{task_id}/log")
        if status != 200 or raw is None:
            return ""
        return raw.decode(errors="replace")

    def kill(self, task_id) -> None:
        self._call("DELETE", f"/tasks/{task_id}")

    def exec(self, command, timeout_s=600) -> dict:
        status, raw = self._call(
            "POST", "/exec", {"command": command, "timeout_s": timeout_s}, timeout=timeout_s + 10
        )
        if raw is None:
            return {"exit_code": -1, "stdout": "", "stderr": "transport error"}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"exit_code": -1, "stdout": "", "stderr": raw.decode(errors="replace")}
        if status == 504:
            return {"exit_code": -1, "stdout": "", "stderr": data.get("error", "timeout")}
        return data

    def read_file(self, path) -> str | None:
        q = urllib.parse.urlencode({"path": path})
        status, raw = self._call("GET", f"/files?{q}")
        if status != 200 or raw is None:
            return None
        return raw.decode(errors="replace")
