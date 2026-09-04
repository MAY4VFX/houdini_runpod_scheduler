import http.client
import json
import os
import sys
import threading
import time
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pod"))
import worker  # noqa: E402


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_TOKEN", "t")
    monkeypatch.setenv("RPFARM_ROLE", "sync")
    monkeypatch.setenv("RPFARM_SLOTS", "1")
    monkeypatch.setenv("HFS", str(tmp_path / "nohfs"))
    # make_server builds the detached-exec dir from WORKSPACE_ROOT, so this
    # has to be redirected BEFORE the server exists -- otherwise a test run
    # reaches for the real /workspace (the same class of mistake that had
    # the suite overwriting the artist's real Houdini prefs).
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    s = worker.make_server("127.0.0.1", 0, log_dir=str(tmp_path))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()
    s.server_close()


def req(port, method, path, body=None, token="t"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-RPFarm-Token": token, "Content-Type": "application/json"}
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data.decode())


def test_auth_required(srv):
    assert req(srv, "GET", "/health", token="wrong")[0] == 401


def test_health(srv):
    st, h = req(srv, "GET", "/health")
    assert st == 200 and h["role"] == "sync" and h["slots"] == 1 and h["busy"] == 0


def test_task_lifecycle(srv):
    st, r = req(srv, "POST", "/tasks", {"task_id": "a1", "command": "echo hi; echo err 1>&2; exit 3", "env": {"X": "1"}})
    assert st == 202
    for _ in range(50):
        st, t = req(srv, "GET", "/tasks/a1")
        if t["state"] != "running":
            break
        time.sleep(0.1)
    assert t["state"] == "failed" and t["exit_code"] == 3
    assert "hi" in t["tail"] and "err" in t["tail"]
    st, log = req(srv, "GET", "/tasks/a1/log")
    assert st == 200 and "hi" in log


def test_slots_429(srv):
    assert req(srv, "POST", "/tasks", {"task_id": "s1", "command": "sleep 5"})[0] == 202
    assert req(srv, "POST", "/tasks", {"task_id": "s2", "command": "echo"})[0] == 429
    assert req(srv, "DELETE", "/tasks/s1")[0] == 200


def test_exec_sync_only(srv):
    st, r = req(srv, "POST", "/exec", {"command": "echo ok"})
    assert st == 200 and r["stdout"].strip() == "ok" and r["exit_code"] == 0


def test_duplicate_task_id_rejected(srv):
    assert req(srv, "POST", "/tasks", {"task_id": "d1", "command": "sleep 5"})[0] == 202
    st, r = req(srv, "POST", "/tasks", {"task_id": "d1", "command": "echo dup"})
    assert st == 409
    st, h = req(srv, "GET", "/health")
    assert st == 200 and h["busy"] == 1
    assert req(srv, "DELETE", "/tasks/d1")[0] == 200


def test_exec_output_capped(srv):
    payload = "0123456789" * 30000  # 300000 deterministic chars
    cmd = "python3 -c \"import sys; sys.stdout.write('%s')\"" % payload
    st, r = req(srv, "POST", "/exec", {"command": cmd})
    assert st == 200
    assert len(r["stdout"]) == 200000
    assert r["stdout"] == payload[-200000:]


def test_files_403_outside_workspace(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    outside = tmp_path.parent / f"outside-{os.getpid()}.txt"
    outside.write_text("secret")
    try:
        st, r = req(srv, "GET", "/files?path=" + quote(str(outside)))
        assert st == 403
    finally:
        outside.unlink()


def test_files_reads_under_workspace(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    f = tmp_path / "sub" / "file.txt"
    f.parent.mkdir()
    f.write_text("hello world")
    st, body = req(srv, "GET", "/files?path=" + quote(str(f)))
    assert st == 200 and "hello world" in body


# -- Ruling R31: detached exec ------------------------------------------------


def _wait_done(port, handle, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, body = req(port, "GET", "/exec/" + handle)
        assert st == 200, body
        if body["state"] == "done":
            return body
        time.sleep(0.1)
    raise AssertionError("detached run never finished")


def test_exec_detached_outlives_the_request_and_reports_its_exit_code(srv, tmp_path):
    """The bug this exists to prevent (Task 14): a command that runs longer
    than the HTTP request must keep running after the response is sent, and
    its exit code must still be retrievable afterwards.

    The synchronous path cannot do this -- the RunPod proxy cuts the response
    at ~100s and the pod-side timeout SIGKILLs the shell, closing the pipe a
    surviving grandchild then dies on.
    """
    st, body = req(srv, "POST", "/exec", {"command": "sleep 1; echo finished-after-response; exit 7", "detach": True})
    assert st == 202
    handle = body["handle"]

    # The response came back immediately, while the command is still running.
    st_now, running = req(srv, "GET", "/exec/" + handle)
    assert st_now == 200 and running["state"] == "running" and running["exit_code"] is None

    done = _wait_done(srv, handle)
    assert done["exit_code"] == 7

    log = open(body["log_path"]).read()
    assert "finished-after-response" in log


def test_exec_detached_leaves_no_zombie(srv):
    """A finished detached child must be reaped. As PID 1 in the container the
    worker inherits orphans and never reaps them -- Task 14 found the dead
    installer as `[houdini.install] <defunct>` with PPID 1, which is how the
    cause was identified in the first place.

    Checked by asking the OS: once the run reports done, this process must
    have no unreaped children left, i.e. `waitpid(-1, WNOHANG)` raises
    ChildProcessError rather than handing back a corpse.
    """
    st, body = req(srv, "POST", "/exec", {"command": "true", "detach": True})
    assert st == 202
    _wait_done(srv, body["handle"])

    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)

    # and the exit code survives being reaped
    _st, again = req(srv, "GET", "/exec/" + body["handle"])
    assert again["state"] == "done" and again["exit_code"] == 0


def test_exec_detached_writes_its_output_to_a_file_not_the_http_pipe(srv):
    st, body = req(srv, "POST", "/exec", {"command": "echo to-a-file; echo to-stderr 1>&2", "detach": True})
    assert st == 202
    _wait_done(srv, body["handle"])
    log = open(body["log_path"]).read()
    assert "to-a-file" in log and "to-stderr" in log  # stderr is merged into the same file


def test_exec_status_unknown_handle_is_404(srv):
    assert req(srv, "GET", "/exec/exec-doesnotexist")[0] == 404


def test_exec_sync_timeout_is_clamped_to_the_proxy_ceiling(srv, monkeypatch):
    """A caller may ask for 829s (the size-derived figure that Task 14's
    install used); the transport cannot deliver a response that long, so the
    handler must not pretend it can."""
    seen = {}
    real_popen = worker.subprocess.Popen

    class SpyPopen(real_popen):
        def communicate(self, *a, **kw):
            seen["timeout"] = kw.get("timeout", a[1] if len(a) > 1 else None)
            return super().communicate(*a, **kw)

    monkeypatch.setattr(worker.subprocess, "Popen", SpyPopen)
    st, body = req(srv, "POST", "/exec", {"command": "true", "timeout_s": 829})
    assert st == 200 and body["exit_code"] == 0
    assert seen["timeout"] == worker.EXEC_SYNC_CEILING_S


def test_exec_sync_timeout_response_points_at_the_detached_path(srv):
    st, body = req(srv, "POST", "/exec", {"command": "sleep 5", "timeout_s": 1})
    assert st == 504
    assert "detach" in body["hint"]


def test_exec_sync_timeout_kills_grandchildren_too(srv, tmp_path):
    """A timed-out /exec must take its whole process group with it.

    `subprocess.run`'s timeout kills only the shell it started; a grandchild
    (an installer, a tar) survives and keeps writing to a pipe nobody is
    reading -- the exact shape of Task 14's corrupted Houdini install, and
    confirmed live afterwards as a leftover zombie on the pod.
    """
    marker = tmp_path / "grandchild-still-alive"
    # bash spawns a *grandchild* that would touch the marker after the
    # timeout has already fired, then waits on it.
    cmd = "bash -c 'sleep 3; touch {}' & wait".format(marker)

    st, body = req(srv, "POST", "/exec", {"command": cmd, "timeout_s": 1})
    assert st == 504 and body["error"] == "timeout"

    time.sleep(5)  # well past when the grandchild would have fired
    assert not marker.exists(), "grandchild outlived the killed request"


def test_exec_sync_timeout_leaves_no_unreaped_child(srv):
    st, _body = req(srv, "POST", "/exec", {"command": "sleep 30", "timeout_s": 1})
    assert st == 504
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)
