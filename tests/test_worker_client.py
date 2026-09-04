import json
import ssl

import pytest

from rpfarm import VERSION
from rpfarm import worker_client as wc
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

    def fake_urlopen(req, timeout=None, context=None):
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

    def fake_urlopen(req, timeout=None, context=None):
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr("rpfarm.worker_client.urllib.request.urlopen", fake_urlopen)
    from rpfarm.worker_client import _urllib_transport

    _urllib_transport("GET", "https://x/health", None, {})
    assert captured["timeout"] == 30


def test_default_transport_uses_a_verifying_ssl_context(monkeypatch):
    """Same Houdini CA-store problem as the RunPod API transport: without an
    explicit context, every call to the pod proxy fails the TLS handshake."""
    seen = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return FakeResponse()

    monkeypatch.setattr(wc.urllib.request, "urlopen", fake_urlopen)
    wc._urllib_transport("GET", "https://p-8000.proxy.runpod.net/health", None, {})
    assert seen["context"] is not None
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


# -- Ruling R31: detached exec ------------------------------------------------


def _detached_transport(states, log_body=b"installer output"):
    """Fake pod: accepts the detached start, then walks `states` on each
    status poll, and serves the log through /files."""
    seq = list(states)
    calls = []

    def t(method, url, body, headers, timeout=30):
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/exec"):
            return 202, json.dumps(
                {"handle": "exec-abc", "log_path": "/workspace/.rpfarm/exec/exec-abc.log",
                 "rc_path": "/workspace/.rpfarm/exec/exec-abc.rc"}
            ).encode()
        if method == "GET" and "/exec/exec-abc" in url:
            return 200, json.dumps(seq.pop(0)).encode()
        if method == "GET" and "/files?" in url:
            return 200, log_body
        raise AssertionError("unexpected " + url)

    return t, calls


def test_exec_wait_polls_until_done_and_returns_the_exit_code():
    t, calls = _detached_transport([
        {"state": "running", "exit_code": None},
        {"state": "running", "exit_code": None},
        {"state": "done", "exit_code": 0},
    ])
    c = WorkerClient("pod1", "tok", transport=t)
    slept = []

    out = c.exec_wait("./houdini.install ...", deadline_s=600, poll_s=5, sleep=slept.append)

    assert out["exit_code"] == 0
    assert out["stdout"] == "installer output"
    assert len(slept) == 2  # slept between the two "running" polls, not after "done"
    assert calls[0][0] == "POST" and calls[0][2]["detach"] is True


def test_exec_wait_reports_a_failed_command_rather_than_swallowing_it():
    t, _calls = _detached_transport([{"state": "done", "exit_code": 3}])
    c = WorkerClient("pod1", "tok", transport=t)
    assert c.exec_wait("boom", deadline_s=60, sleep=lambda s: None)["exit_code"] == 3


def test_exec_wait_deadline_does_not_kill_the_command():
    """Hitting the deadline means "stop watching", not "kill it" -- the old
    pod-side subprocess timeout is exactly what corrupted Task 14's install.
    The message has to say so, and point at the log."""
    t, calls = _detached_transport([{"state": "running", "exit_code": None}] * 50)
    c = WorkerClient("pod1", "tok", transport=t)
    clock = iter([0, 0, 100, 200])

    out = c.exec_wait("slow", deadline_s=1, poll_s=1, sleep=lambda s: None, now=lambda: next(clock))

    assert out["exit_code"] == -1
    assert "NOT killed" in out["stderr"] and "exec-abc.log" in out["stderr"]
    assert not any(m == "DELETE" for m, _u, _b in calls)  # nothing was cancelled


def test_exec_detached_returns_none_when_the_pod_refuses():
    def t(method, url, body, headers, timeout=30):
        return 403, b'{"error":"exec only on sync pod"}'

    assert WorkerClient("pod1", "tok", transport=t).exec_detached("x") is None
