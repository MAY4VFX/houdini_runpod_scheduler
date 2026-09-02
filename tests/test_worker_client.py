import json

import pytest

from rpfarm import VERSION
from rpfarm.worker_client import WorkerClient, WorkerError


def test_submit_429_returns_busy():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url, body, headers))
        return 429, b'{"error":"busy"}'

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.submit("t1", "echo", {}) == "busy"
    assert calls[0][1] == "https://pod1-8000.proxy.runpod.net/tasks" and calls[0][3]["X-RPFarm-Token"] == "tok"


def test_submit_202_returns_accepted():
    def t(method, url, body, headers, timeout=30):
        return 202, b'{"task_id":"t1"}'

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.submit("t1", "echo hi", {"X": "1"}, cwd="/workspace", log_path="/workspace/l.log") == "accepted"


def test_submit_409_returns_duplicate():
    def t(method, url, body, headers, timeout=30):
        return 409, b'{"error":"task_id already exists"}'

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.submit("t1", "echo", {}) == "duplicate"


def test_submit_other_status_raises_worker_error():
    def t(method, url, body, headers, timeout=30):
        return 500, b'{"error":"boom"}'

    c = WorkerClient("pod1", "tok", transport=t)
    with pytest.raises(WorkerError) as e:
        c.submit("t1", "echo", {})
    assert e.value.status == 500


def test_submit_transport_error_raises_worker_error():
    c = WorkerClient("pod1", "tok", transport=lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(WorkerError) as e:
        c.submit("t1", "echo", {})
    assert e.value.status is None


def test_health_none_on_error():
    c = WorkerClient("pod1", "tok", transport=lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    assert c.health() is None


def test_health_none_on_404_html():
    def t(method, url, body, headers, timeout=30):
        return 404, b"<html>not found</html>"

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.health() is None


def test_health_none_on_non_json_200():
    def t(method, url, body, headers, timeout=30):
        return 200, b"not json"

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.health() is None


def test_health_ok():
    def t(method, url, body, headers, timeout=30):
        return 200, json.dumps({"role": "gpu", "busy": 0}).encode()

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.health() == {"role": "gpu", "busy": 0}


def test_user_agent_header_sent():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append(headers)
        return 200, b"{}"

    c = WorkerClient("pod1", "tok", transport=t)
    c.health()
    assert calls[0]["User-Agent"] == f"rpfarm/{VERSION}"


def test_status_url_and_method():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url))
        return 200, json.dumps({"state": "running"}).encode()

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.status("t1") == {"state": "running"}
    assert calls[0] == ("GET", "https://pod1-8000.proxy.runpod.net/tasks/t1")


def test_status_none_on_404():
    def t(method, url, body, headers, timeout=30):
        return 404, b'{"error":"unknown task"}'

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.status("nope") is None


def test_log_returns_text():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url))
        return 200, b"line1\nline2\n"

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.log("t1") == "line1\nline2\n"
    assert calls[0] == ("GET", "https://pod1-8000.proxy.runpod.net/tasks/t1/log")


def test_kill_calls_delete():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url))
        return 200, b'{"state":"killed"}'

    c = WorkerClient("pod1", "tok", transport=t)
    c.kill("t1")
    assert calls[0] == ("DELETE", "https://pod1-8000.proxy.runpod.net/tasks/t1")


def test_exec_posts_command_and_timeout():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url, body, timeout))
        return 200, json.dumps({"exit_code": 0, "stdout": "hi", "stderr": ""}).encode()

    c = WorkerClient("pod1", "tok", transport=t)
    result = c.exec("echo hi", timeout_s=60)
    assert result == {"exit_code": 0, "stdout": "hi", "stderr": ""}
    assert calls[0][0] == "POST" and calls[0][1] == "https://pod1-8000.proxy.runpod.net/exec"
    assert calls[0][2]["command"] == "echo hi" and calls[0][2]["timeout_s"] == 60
    # WorkerClient.exec passes an explicit transport-level timeout of
    # timeout_s + 10 (separate from the timeout_s in the JSON body, which
    # the worker's own subprocess.run enforces).
    assert calls[0][3] == 70


def test_non_exec_calls_use_default_transport_timeout_of_30():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append(timeout)
        return 200, b"{}"

    c = WorkerClient("pod1", "tok", transport=t)
    c.health()
    assert calls[0] == 30


def test_exec_504_timeout_returns_error_shape():
    def t(method, url, body, headers, timeout=30):
        return 504, b'{"error":"timeout"}'

    c = WorkerClient("pod1", "tok", transport=t)
    result = c.exec("sleep 999", timeout_s=5)
    assert result["exit_code"] != 0
    assert "timeout" in result["stderr"]


def test_exec_transport_error_returns_error_shape():
    c = WorkerClient("pod1", "tok", transport=lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    result = c.exec("echo hi")
    assert result["exit_code"] != 0


def test_read_file_returns_text():
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url))
        return 200, b"file contents"

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.read_file("/workspace/x.txt") == "file contents"
    assert calls[0][0] == "GET"
    assert calls[0][1].startswith("https://pod1-8000.proxy.runpod.net/files?path=")


def test_read_file_none_on_404():
    def t(method, url, body, headers, timeout=30):
        return 404, b'{"error":"not found"}'

    c = WorkerClient("pod1", "tok", transport=t)
    assert c.read_file("/workspace/gone.txt") is None


def test_default_transport_forwards_explicit_timeout(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr("rpfarm.worker_client.urllib.request.urlopen", fake_urlopen)
    from rpfarm.worker_client import _urllib_transport

    _urllib_transport("POST", "https://x/exec", {"command": "x", "timeout_s": 20}, {}, timeout=30)
    assert captured["timeout"] == 30


def test_default_transport_defaults_timeout_to_30(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr("rpfarm.worker_client.urllib.request.urlopen", fake_urlopen)
    from rpfarm.worker_client import _urllib_transport

    _urllib_transport("GET", "https://x/health", None, {})
    assert captured["timeout"] == 30
