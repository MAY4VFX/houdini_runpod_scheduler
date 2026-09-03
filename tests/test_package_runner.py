import json
import sys
import types

import pytest

from rpfarm import package_runner


class FakeCfg:
    rclone_path = "/bin/true"
    api_key = "k"
    ssh_key_path = "/tmp/fake_key"


def _write_payload(tmp_path, item, compress=False):
    payload = {"item": item, "compress": compress}
    path = tmp_path / "item.json"
    path.write_text(json.dumps(payload))
    return str(path)


def _patch_pipeline(monkeypatch, run_upload_item_result, run_upload_item_calls):
    def fake_run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb):
        run_upload_item_calls.append((item, compress))
        if progress_cb:
            progress_cb(1, 2, 3)
        return run_upload_item_result

    monkeypatch.setattr("rpfarm.packages.run_upload_item", fake_run_upload_item)
    monkeypatch.setattr("rpfarm.config.load", lambda: FakeCfg())
    monkeypatch.setattr("rpfarm.config.session_token", lambda: "tok")
    monkeypatch.setattr("rpfarm.pods.ensure_sync_pod", lambda api, cfg, token, pubkey: {"id": "pod1"})
    monkeypatch.setattr("rpfarm.runpod_api.pod_public_endpoint", lambda pod, port: ("1.2.3.4", 22))

    class FakeWorkerClient:
        def __init__(self, pod_id, token):
            pass

    monkeypatch.setattr("rpfarm.worker_client.WorkerClient", FakeWorkerClient)
    monkeypatch.setattr("builtins.open", _fake_open_wrapping(open))


def _fake_open_wrapping(real_open):
    def opener(path, *a, **kw):
        if str(path).endswith(".pub"):
            return _StrFile("ssh-ed25519 AAAA")
        return real_open(path, *a, **kw)

    return opener


class _StrFile:
    def __init__(self, content):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._content


def test_main_reports_success_and_attributes(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/plug",
        "files": [[str(tmp_path / "a.so"), "/workspace/apps/plug/a.so", 10]],
        "bytes": 10,
        "post_command": "",
    }
    calls = []
    _patch_pipeline(monkeypatch, {"files": 1, "bytes": 10, "seconds": 0.5}, calls)

    reported = {}

    class FakePdgcmd:
        @staticmethod
        def setStringAttrib(name, value, index):
            reported[name] = value

        @staticmethod
        def setIntAttrib(name, value, index):
            reported[name] = value

        @staticmethod
        def setFloatAttrib(name, value, index):
            reported[name] = value

    fake_module = types.ModuleType("pdgcmd")
    fake_module.setStringAttrib = FakePdgcmd.setStringAttrib
    fake_module.setIntAttrib = FakePdgcmd.setIntAttrib
    fake_module.setFloatAttrib = FakePdgcmd.setFloatAttrib
    monkeypatch.setitem(sys.modules, "pdgcmd", fake_module)
    monkeypatch.setenv("PDG_SCRIPTDIR", str(tmp_path))

    payload_path = _write_payload(tmp_path, item, compress=False)
    rc = package_runner.main([payload_path])

    assert rc == 0
    assert len(calls) == 1 and calls[0][0] == item and calls[0][1] is False
    assert reported["bytes"] == 10
    assert reported["files"] == 1
    assert reported["seconds"] == pytest.approx(0, abs=5)
    assert "mbps" in reported
    assert reported["progress"] == "0/0 MB"


def test_main_without_pdg_scriptdir_still_succeeds(tmp_path, monkeypatch):
    """No PDG_SCRIPTDIR (e.g. run by hand): pdgcmd reporting is skipped,
    but the upload itself still runs and main() still returns 0."""
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/plug",
        "files": [],
        "bytes": 0,
        "post_command": "",
    }
    calls = []
    _patch_pipeline(monkeypatch, {"files": 0, "bytes": 0, "seconds": 0.1}, calls)
    monkeypatch.delenv("PDG_SCRIPTDIR", raising=False)

    payload_path = _write_payload(tmp_path, item)
    rc = package_runner.main([payload_path])

    assert rc == 0
    assert len(calls) == 1


def test_main_returns_nonzero_on_failure(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/plug",
        "files": [["x", "/workspace/apps/plug/x", 1]],
        "bytes": 1,
        "post_command": "",
    }

    def boom(*a, **kw):
        raise RuntimeError("sync failed")

    monkeypatch.setattr("rpfarm.packages.run_upload_item", boom)
    monkeypatch.setattr("rpfarm.config.load", lambda: FakeCfg())
    monkeypatch.setattr("rpfarm.config.session_token", lambda: "tok")
    monkeypatch.setattr("rpfarm.pods.ensure_sync_pod", lambda api, cfg, token, pubkey: {"id": "pod1"})
    monkeypatch.setattr("builtins.open", _fake_open_wrapping(open))
    monkeypatch.delenv("PDG_SCRIPTDIR", raising=False)

    payload_path = _write_payload(tmp_path, item)
    rc = package_runner.main([payload_path])
    assert rc != 0


def test_main_requires_exactly_one_argument():
    assert package_runner.main([]) != 0
    assert package_runner.main(["a", "b"]) != 0


# -- kind="download" (Task 10) -------------------------------------------------


def _write_download_payload(tmp_path, item, overwrite="newer"):
    payload = {"kind": "download", "item": item, "overwrite": overwrite}
    path = tmp_path / "item.json"
    path.write_text(json.dumps(payload))
    return str(path)


def _patch_download_pipeline(monkeypatch, run_download_item_result, calls):
    def fake_run_download_item(item, cfg, sftp, sync_client, overwrite, progress_cb):
        calls.append((item, overwrite))
        if progress_cb:
            progress_cb(1, 2, 3)
        return run_download_item_result

    monkeypatch.setattr("rpfarm.packages.run_download_item", fake_run_download_item)
    monkeypatch.setattr("rpfarm.config.load", lambda: FakeCfg())
    monkeypatch.setattr("rpfarm.config.session_token", lambda: "tok")
    monkeypatch.setattr("rpfarm.pods.ensure_sync_pod", lambda api, cfg, token, pubkey: {"id": "pod1"})
    monkeypatch.setattr("rpfarm.runpod_api.pod_public_endpoint", lambda pod, port: ("1.2.3.4", 22))

    class FakeWorkerClient:
        def __init__(self, pod_id, token):
            pass

    monkeypatch.setattr("rpfarm.worker_client.WorkerClient", FakeWorkerClient)
    monkeypatch.setattr("builtins.open", _fake_open_wrapping(open))


def test_main_dispatches_to_run_download_item_with_overwrite(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shot/render",
        "files": [["/local/a.exr", "/workspace/projects/may/shot/render/a.exr", 10]],
        "bytes": 10,
    }
    calls = []
    _patch_download_pipeline(monkeypatch, {"files": 1, "bytes": 10, "seconds": 0.5}, calls)
    monkeypatch.delenv("PDG_SCRIPTDIR", raising=False)

    payload_path = _write_download_payload(tmp_path, item, overwrite="never")
    rc = package_runner.main([payload_path])

    assert rc == 0
    assert len(calls) == 1
    assert calls[0] == (item, "never")


def test_main_download_reports_pdgcmd_attributes(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shot/render",
        "files": [["/local/a.exr", "/workspace/projects/may/shot/render/a.exr", 10]],
        "bytes": 10,
    }
    calls = []
    _patch_download_pipeline(monkeypatch, {"files": 1, "bytes": 10, "seconds": 0.5}, calls)

    reported = {}

    class FakePdgcmd:
        @staticmethod
        def setStringAttrib(name, value, index):
            reported[name] = value

        @staticmethod
        def setIntAttrib(name, value, index):
            reported[name] = value

        @staticmethod
        def setFloatAttrib(name, value, index):
            reported[name] = value

    fake_module = types.ModuleType("pdgcmd")
    fake_module.setStringAttrib = FakePdgcmd.setStringAttrib
    fake_module.setIntAttrib = FakePdgcmd.setIntAttrib
    fake_module.setFloatAttrib = FakePdgcmd.setFloatAttrib
    monkeypatch.setitem(sys.modules, "pdgcmd", fake_module)
    monkeypatch.setenv("PDG_SCRIPTDIR", str(tmp_path))

    payload_path = _write_download_payload(tmp_path, item, overwrite="newer")
    rc = package_runner.main([payload_path])

    assert rc == 0
    assert reported["bytes"] == 10
    assert reported["files"] == 1
    assert "mbps" in reported
    assert reported["progress"] == "0/0 MB"


def test_main_download_returns_nonzero_on_failure(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/x",
        "files": [["/local/a", "/workspace/x/a", 1]],
        "bytes": 1,
    }

    def boom(*a, **kw):
        raise RuntimeError("sync failed")

    monkeypatch.setattr("rpfarm.packages.run_download_item", boom)
    monkeypatch.setattr("rpfarm.config.load", lambda: FakeCfg())
    monkeypatch.setattr("rpfarm.config.session_token", lambda: "tok")
    monkeypatch.setattr("rpfarm.pods.ensure_sync_pod", lambda api, cfg, token, pubkey: {"id": "pod1"})
    monkeypatch.setattr("builtins.open", _fake_open_wrapping(open))
    monkeypatch.delenv("PDG_SCRIPTDIR", raising=False)

    payload_path = _write_download_payload(tmp_path, item)
    rc = package_runner.main([payload_path])
    assert rc != 0


def test_main_defaults_to_upload_kind_when_absent(tmp_path, monkeypatch):
    """Backward compatibility: a payload with no "kind" key (every upload
    item written before Task 10) still dispatches to run_upload_item."""
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/plug",
        "files": [],
        "bytes": 0,
        "post_command": "",
    }
    calls = []
    _patch_pipeline(monkeypatch, {"files": 0, "bytes": 0, "seconds": 0.1}, calls)
    monkeypatch.delenv("PDG_SCRIPTDIR", raising=False)

    payload_path = _write_payload(tmp_path, item)
    rc = package_runner.main([payload_path])

    assert rc == 0
    assert len(calls) == 1
