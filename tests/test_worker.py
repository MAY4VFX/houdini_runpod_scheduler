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
