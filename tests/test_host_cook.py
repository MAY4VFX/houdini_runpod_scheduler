"""Tests for pod/host_cook.py (Ruling R71, Submit As Job / mode 3).

Imported the same way tests/test_worker.py imports worker.py -- this file
lives in pod/, deployed stdlib-only to the pod image, not part of the
rpfarm package.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pod"))
import host_cook  # noqa: E402


def test_find_topcook_globs_the_version_specific_libs_dir(tmp_path):
    libs = tmp_path / "houdini" / "python3.13libs" / "pdgjob"
    libs.mkdir(parents=True)
    (libs / "topcook.py").write_text("# stub")

    found = host_cook.find_topcook(str(tmp_path))

    assert found == str(libs / "topcook.py")


def test_find_topcook_returns_none_when_missing(tmp_path):
    assert host_cook.find_topcook(str(tmp_path)) is None


def test_build_topcook_command_shape():
    cmd = host_cook.build_topcook_command("hython", "/x/topcook.py", "/x/scene.hip", "/obj/topnet1")
    assert cmd == [
        "hython", "--pdg", "/x/topcook.py",
        "--report", "none",
        "--hip", "/x/scene.hip",
        "--verbosity", "2",
        "--logs",
        "--toppath", "/obj/topnet1",
    ]


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_terminate_pod_calls_delete_and_logs_only_the_pod_id_and_status(monkeypatch, capsys):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _FakeResponse(200, b"{}")

    monkeypatch.setattr(host_cook.urllib.request, "urlopen", fake_urlopen)

    host_cook.terminate_pod("pod123", "secret-key-value")

    assert seen["method"] == "DELETE"
    assert seen["url"] == "https://rest.runpod.io/v1/pods/pod123"
    assert seen["auth"] == "Bearer secret-key-value"
    out = capsys.readouterr().out
    assert "pod123" in out
    assert "200" in out
    assert "secret-key-value" not in out


def test_terminate_pod_never_raises_on_a_network_error(monkeypatch, capsys):
    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(host_cook.urllib.request, "urlopen", fake_urlopen)

    host_cook.terminate_pod("pod123", "secret-key-value")  # must not raise

    out = capsys.readouterr().out
    assert "secret-key-value" not in out
    assert "connection refused" not in out  # only the exception TYPE is logged


def test_terminate_pod_never_leaks_the_key_even_on_an_http_error(monkeypatch, capsys):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("secret-key-value leaked in a real bug would show here")

    monkeypatch.setattr(host_cook.urllib.request, "urlopen", fake_urlopen)

    host_cook.terminate_pod("pod123", "secret-key-value")

    out = capsys.readouterr().out
    assert "secret-key-value" not in out


def test_list_cook_pods_filters_by_user_prefix_and_cook_id_segment(monkeypatch):
    pods = [
        {"id": "a", "name": "rpfarm-may-airship-cookid1-1"},
        {"id": "b", "name": "rpfarm-may-airship-othercook-1"},   # different cook
        {"id": "c", "name": "rpfarm-other-airship-cookid1-1"},   # different user
        {"id": "d", "name": "rpfarm-sync-may"},                  # sync pod, no cook segment
    ]

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(200, json.dumps(pods).encode())

    monkeypatch.setattr(host_cook.urllib.request, "urlopen", fake_urlopen)

    found = host_cook.list_cook_pods("may", "cookid1", "key")

    assert [p["id"] for p in found] == ["a"]


def test_list_cook_pods_degrades_to_empty_on_any_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("blocked")

    monkeypatch.setattr(host_cook.urllib.request, "urlopen", fake_urlopen)

    assert host_cook.list_cook_pods("may", "cookid1", "key") == []


def test_sweep_render_pods_terminates_everything_but_itself(monkeypatch):
    pods = [
        {"id": "self", "name": "rpfarm-may-airship-cookid1-host"},
        {"id": "render1", "name": "rpfarm-may-airship-cookid1-1"},
        {"id": "render2", "name": "rpfarm-may-airship-cookid1-2"},
    ]
    monkeypatch.setattr(host_cook, "list_cook_pods", lambda user, cook_id, key: pods)
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))

    host_cook.sweep_render_pods("self", "may", "cookid1", "key")

    assert terminated == ["render1", "render2"]


def test_watchdog_fires_after_its_deadline():
    fired = threading.Event()
    wd = host_cook.Watchdog(max_seconds=0.05, on_expire=fired.set, poll_s=0.02)
    wd.start()
    fired.wait(timeout=2)
    wd.cancel()
    wd.join(timeout=2)
    assert fired.is_set()


def test_watchdog_cancel_stops_it_before_it_fires():
    fired = threading.Event()
    wd = host_cook.Watchdog(max_seconds=5, on_expire=fired.set, poll_s=0.02)
    wd.start()
    time.sleep(0.05)
    wd.cancel()
    wd.join(timeout=2)
    assert not fired.is_set()


def test_main_terminates_self_when_required_env_is_missing(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod1")
    for var in ("RPFARM_HOST_HIP", "RPFARM_HOST_TOPPATH", "RPFARM_HOST_PKGDIR"):
        monkeypatch.delenv(var, raising=False)
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))

    rc = host_cook.main()

    assert rc == 1
    assert terminated == ["pod1"]


def test_main_terminates_self_and_sweeps_when_topcook_is_not_found(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod1")
    monkeypatch.setenv("RPFARM_USER", "may")
    monkeypatch.setenv("RPFARM_COOK", "cookid1")
    monkeypatch.setenv("RPFARM_HOST_HIP", str(tmp_path / "scene.hip"))
    monkeypatch.setenv("RPFARM_HOST_TOPPATH", "/obj/topnet1")
    monkeypatch.setenv("RPFARM_HOST_PKGDIR", str(tmp_path / "host_pkg"))
    monkeypatch.setenv("RPFARM_HOST_SSH_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekeydata\n-----END OPENSSH PRIVATE KEY-----\n")
    monkeypatch.setenv("HFS", str(tmp_path / "nohfs"))  # no topcook.py under here
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))

    rc = host_cook.main()

    assert rc == 1
    assert terminated == ["pod1"]


def test_main_runs_topcook_and_terminates_self_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod1")
    monkeypatch.setenv("RPFARM_USER", "may")
    monkeypatch.setenv("RPFARM_COOK", "cookid1")
    monkeypatch.setenv("RPFARM_HOST_HIP", str(tmp_path / "scene.hip"))
    monkeypatch.setenv("RPFARM_HOST_TOPPATH", "/obj/topnet1")
    monkeypatch.setenv("RPFARM_HOST_PKGDIR", str(tmp_path / "host_pkg"))
    monkeypatch.setenv("RPFARM_HOST_SSH_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekeydata\n-----END OPENSSH PRIVATE KEY-----\n")
    monkeypatch.setenv("HFS", str(tmp_path / "hfs"))
    libs = tmp_path / "hfs" / "houdini" / "python3.13libs" / "pdgjob"
    libs.mkdir(parents=True)
    (libs / "topcook.py").write_text("# stub")

    calls = []
    envs = []

    def fake_run(command, env=None, timeout=None, stdout=None, stderr=None, text=None):
        calls.append(command)
        envs.append(env)
        return subprocess.CompletedProcess(command, 0, stdout="cook ok\n")

    monkeypatch.setattr(host_cook.subprocess, "run", fake_run)
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))
    monkeypatch.setattr(host_cook, "sweep_render_pods", lambda *a, **k: None)

    rc = host_cook.main()

    assert rc == 0
    assert calls and calls[0][0] == "hython"
    assert "--toppath" in calls[0] and "/obj/topnet1" in calls[0]
    assert terminated == ["pod1"]

    # The three rpcfg.config.load() overrides (Ruling R71) -- the nested
    # scheduler must see a pod-local ssh key and rclone, not the shipped
    # config.toml's Mac-only paths.
    env = envs[0]
    assert env["RPFARM_SSH_KEY_PATH"] == "/tmp/.rpfarm_host_key"
    assert env["RPFARM_RCLONE_PATH"]
    assert env["RPFARM_ROOT"] == str(tmp_path / "host_pkg")
    assert env["HOUDINI_OTLSCAN_PATH"].startswith(str(tmp_path / "host_pkg" / "hda"))
    # The key itself was written to a LOCAL container path, never under
    # /workspace (the shared volume) -- and readable only by this pod.
    import stat
    key_path = "/tmp/.rpfarm_host_key"
    assert os.path.exists(key_path)
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert open(key_path).read().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    os.remove(key_path)


def test_main_terminates_self_even_when_the_cook_times_out(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod1")
    monkeypatch.setenv("RPFARM_USER", "may")
    monkeypatch.setenv("RPFARM_COOK", "cookid1")
    monkeypatch.setenv("RPFARM_HOST_HIP", str(tmp_path / "scene.hip"))
    monkeypatch.setenv("RPFARM_HOST_TOPPATH", "/obj/topnet1")
    monkeypatch.setenv("RPFARM_HOST_PKGDIR", str(tmp_path / "host_pkg"))
    monkeypatch.setenv("RPFARM_HOST_SSH_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekeydata\n-----END OPENSSH PRIVATE KEY-----\n")
    monkeypatch.setenv("RPFARM_HOST_MAX_MINUTES", "1")
    monkeypatch.setenv("HFS", str(tmp_path / "hfs"))
    libs = tmp_path / "hfs" / "houdini" / "python3.13libs" / "pdgjob"
    libs.mkdir(parents=True)
    (libs / "topcook.py").write_text("# stub")

    def fake_run(command, env=None, timeout=None, stdout=None, stderr=None, text=None):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(host_cook.subprocess, "run", fake_run)
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))
    monkeypatch.setattr(host_cook, "sweep_render_pods", lambda *a, **k: None)

    rc = host_cook.main()

    assert rc == 1
    assert terminated == ["pod1"]


def test_main_terminates_self_even_on_an_unexpected_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNPOD_API_KEY", "key")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod1")
    monkeypatch.setenv("RPFARM_USER", "may")
    monkeypatch.setenv("RPFARM_COOK", "cookid1")
    monkeypatch.setenv("RPFARM_HOST_HIP", str(tmp_path / "scene.hip"))
    monkeypatch.setenv("RPFARM_HOST_TOPPATH", "/obj/topnet1")
    monkeypatch.setenv("RPFARM_HOST_PKGDIR", str(tmp_path / "host_pkg"))
    monkeypatch.setenv("RPFARM_HOST_SSH_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nfakekeydata\n-----END OPENSSH PRIVATE KEY-----\n")
    monkeypatch.setenv("HFS", str(tmp_path / "hfs"))
    libs = tmp_path / "hfs" / "houdini" / "python3.13libs" / "pdgjob"
    libs.mkdir(parents=True)
    (libs / "topcook.py").write_text("# stub")

    def fake_run(*a, **k):
        raise RuntimeError("something the process cannot recover from")

    monkeypatch.setattr(host_cook.subprocess, "run", fake_run)
    terminated = []
    monkeypatch.setattr(host_cook, "terminate_pod", lambda pod_id, key: terminated.append(pod_id))
    monkeypatch.setattr(host_cook, "sweep_render_pods", lambda *a, **k: None)

    rc = host_cook.main()  # must not raise

    assert terminated == ["pod1"]
