import json
import os
import stat

import pytest

from rpfarm import cli
from rpfarm import config as rpcfg
from rpfarm import houdini_local
from rpfarm import packages as rppkg
from rpfarm.worker_client import WorkerClient


# -- shared fakes -------------------------------------------------------------


class FakeTransport:
    """URL/method-routed fake for RunPodAPI's injectable transport.

    Handlers are looked up by ``(method, url-suffix)``, checked longest
    suffix first so ``/networkvolumes/vol123`` doesn't accidentally match
    a generic ``/networkvolumes`` handler meant for the collection.
    """

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body))
        for (m, suffix), response in sorted(self.handlers.items(), key=lambda kv: -len(kv[0][1])):
            if m == method and url.endswith(suffix):
                return response(body) if callable(response) else response
        raise AssertionError(f"no fake handler for {method} {url}")


def _setup_handlers(volumes=None, templates=None, balance=42.0):
    volumes = volumes if volumes is not None else [{"id": "vol123", "dataCenterId": "EU-RO-1", "size": 50}]
    templates = templates if templates is not None else []
    return {
        ("GET", "/pods"): (200, json.dumps([]).encode()),
        ("POST", "graphql"): lambda body: (
            (200, json.dumps({"data": {"myself": {"clientBalance": balance}}}).encode())
            if "clientBalance" in body["query"]
            else (200, json.dumps({"data": {"gpuTypes": []}}).encode())
        ),
        ("GET", "/networkvolumes"): (200, json.dumps(volumes).encode()),
        ("GET", "/networkvolumes/vol123"): (200, json.dumps(volumes[0] if volumes else {}).encode()),
        ("GET", "/templates"): (200, json.dumps(templates).encode()),
        ("POST", "/templates"): (200, json.dumps({"id": "tpl123", "name": "rpfarm-pod"}).encode()),
    }


class FakeWorkerClient:
    """Stand-in for WorkerClient: routes .exec()/.exec_wait() by substring
    of the command so tests don't need a live pod.

    Models the two transports as faithfully as the difference matters
    (Ruling R31): `exec` refuses a timeout the pod would not honour, and
    `exec_wait` merges the command's stdout and stderr the way a detached
    run's single log file does.
    """

    def __init__(self, responses):
        self._responses = responses  # list of (substring, result_dict)
        self.calls = []
        self.detached = []  # (command, deadline_s) that took the detached path

    def _lookup(self, command):
        for substr, result in self._responses:
            if substr in command:
                return result
        raise AssertionError(f"no fake response for exec({command!r})")

    def exec(self, command, timeout_s=600):
        assert timeout_s <= WorkerClient.EXEC_SYNC_CEILING_S, (
            f"exec({command!r}) asked for {timeout_s}s over a transport that "
            f"SIGKILLs at {WorkerClient.EXEC_SYNC_CEILING_S}s"
        )
        self.calls.append(command)
        return self._lookup(command)

    def exec_wait(self, command, deadline_s, poll_s=None, **kw):
        self.calls.append(command)
        self.detached.append((command, deadline_s))
        result = dict(self._lookup(command))
        merged = (result.get("stdout") or "") + (result.get("stderr") or "")
        result["stdout"], result["stderr"] = merged, ""
        return result


def _write_cfg(tmp_path, **overrides):
    fields = dict(
        api_key="k", user="tester", volume_id="vol123", template_id="tpl123",
        gpu_priority=["NVIDIA GeForce RTX 4090"],
    )
    fields.update(overrides)
    cfg = rpcfg.Config(**fields)
    rpcfg.save(cfg)
    return cfg


# -- setup --------------------------------------------------------------------


def test_setup_non_interactive_creates_config_and_token(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_transport", FakeTransport(_setup_handlers()))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr(rpcfg, "rclone_bin", lambda *a, **k: str(tmp_path / "bin" / "rclone"))

    rc = cli.main(["setup", "--non-interactive", "--api-key", "testkey", "--user", "tester"])
    assert rc == 0

    assert stat.S_IMODE(os.stat(tmp_path / "config.toml").st_mode) == 0o600
    cfg = rpcfg.load()
    assert cfg.api_key == "testkey"
    assert cfg.user == "tester"
    assert cfg.volume_id == "vol123"
    assert cfg.template_id == "tpl123"
    assert cfg.datacenter == "EU-RO-1"

    assert (tmp_path / "token").is_file()
    assert stat.S_IMODE(os.stat(tmp_path / "token").st_mode) == 0o600
    assert (tmp_path / "id_ed25519").is_file()
    assert (tmp_path / "id_ed25519.pub").is_file()
    assert (tmp_path / "src").is_symlink()


def test_setup_non_interactive_without_api_key_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    rc = cli.main(["setup", "--non-interactive"])
    assert rc == 1
    assert not (tmp_path / "config.toml").exists()


def test_setup_creates_volume_and_template_when_none_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    handlers = _setup_handlers(volumes=[], templates=[])
    handlers[("POST", "/networkvolumes")] = (
        200, json.dumps({"id": "newvol", "dataCenterId": "EU-RO-1", "size": 50}).encode()
    )
    handlers[("GET", "/networkvolumes/newvol")] = (
        200, json.dumps({"id": "newvol", "dataCenterId": "EU-RO-1", "size": 50}).encode()
    )
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr(rpcfg, "rclone_bin", lambda *a, **k: str(tmp_path / "bin" / "rclone"))

    rc = cli.main(["setup", "--non-interactive", "--api-key", "k", "--user", "u"])
    assert rc == 0
    cfg = rpcfg.load()
    assert cfg.volume_id == "newvol"
    assert cfg.template_id == "tpl123"


def test_setup_rerun_preserves_hand_edited_and_doctor_written_fields(tmp_path, monkeypatch):
    """Re-running setup is exactly what the tool's own checklist tells the
    user to do after installing Houdini locally -- it must not reset
    gpu_priority/houdini_version/sync_idle_min/measured_mbps back to
    dataclass defaults, and must not re-discover a volume/template that
    are already configured (proven here by giving the fake transport NO
    handler for the list endpoints at all -- a call to either fails the
    test)."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    existing = rpcfg.Config(
        api_key="oldkey", user="tester", volume_id="vol123", template_id="tpl123",
        gpu_priority=["NVIDIA RTX A4500", "NVIDIA GeForce RTX 4090"],
        houdini_version="22.0.393", sync_idle_min=45, measured_mbps=123.4,
    )
    rpcfg.save(existing)

    handlers = {
        ("GET", "/pods"): (200, json.dumps([]).encode()),
        ("POST", "graphql"): (200, json.dumps({"data": {"myself": {"clientBalance": 42.0}}}).encode()),
        ("GET", "/networkvolumes/vol123"): (200, json.dumps({"id": "vol123", "dataCenterId": "EU-RO-1", "size": 50}).encode()),
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr(rpcfg, "rclone_bin", lambda *a, **k: str(tmp_path / "bin" / "rclone"))

    rc = cli.main(["setup", "--non-interactive", "--api-key", "newkey"])
    assert rc == 0

    cfg = rpcfg.load()
    assert cfg.api_key == "newkey"  # explicitly overridden
    assert cfg.user == "tester"  # kept (not overridden, no prompt needed)
    assert cfg.volume_id == "vol123"  # kept, not re-discovered
    assert cfg.template_id == "tpl123"  # kept, not re-discovered
    assert cfg.gpu_priority == ["NVIDIA RTX A4500", "NVIDIA GeForce RTX 4090"]
    assert cfg.houdini_version == "22.0.393"
    assert cfg.sync_idle_min == 45
    assert cfg.measured_mbps == 123.4


def test_setup_rerun_without_api_key_flag_reuses_configured_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    rpcfg.save(rpcfg.Config(api_key="storedkey", user="tester", volume_id="vol123", template_id="tpl123"))

    handlers = {
        ("GET", "/pods"): (200, json.dumps([]).encode()),
        ("POST", "graphql"): (200, json.dumps({"data": {"myself": {"clientBalance": 42.0}}}).encode()),
        ("GET", "/networkvolumes/vol123"): (200, json.dumps({"id": "vol123", "dataCenterId": "EU-RO-1", "size": 50}).encode()),
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr(rpcfg, "rclone_bin", lambda *a, **k: str(tmp_path / "bin" / "rclone"))

    rc = cli.main(["setup", "--non-interactive"])  # no --api-key at all
    assert rc == 0
    assert rpcfg.load().api_key == "storedkey"


def test_setup_leaves_real_rpfarm_home_untouched(tmp_path, monkeypatch):
    """The exact scenario Task 9 got bitten by: running the CLI must never
    write outside $RPFARM_HOME."""
    real_home = tmp_path / "real-home-decoy"
    real_home.mkdir()

    scoped_home = tmp_path / "scoped"
    monkeypatch.setenv("RPFARM_HOME", str(scoped_home))
    monkeypatch.setattr(cli, "_transport", FakeTransport(_setup_handlers()))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr(rpcfg, "rclone_bin", lambda *a, **k: str(scoped_home / "bin" / "rclone"))

    rc = cli.main(["setup", "--non-interactive", "--api-key", "k", "--user", "u"])
    assert rc == 0
    assert list(real_home.iterdir()) == []
    assert (scoped_home / "config.toml").exists()


# -- doctor -------------------------------------------------------------------


def test_doctor_reports_fail_on_bad_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    rc = cli.main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out


def test_doctor_all_ok_with_no_sync_pod_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, rclone_path="true", ssh_key_path=str(tmp_path / "id_ed25519"))
    (tmp_path / "id_ed25519").write_text("x")
    (tmp_path / "id_ed25519.pub").write_text("x")

    handlers = _setup_handlers()
    handlers[("GET", "/networkvolumes/vol123")] = (
        200, json.dumps({"id": "vol123", "size": 50, "dataCenterId": "EU-RO-1"}).encode()
    )
    handlers[("GET", "/templates")] = (200, json.dumps([{"id": "tpl123", "imageName": "img:latest"}]).encode())
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("blocked in test")))

    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "not checked, requires a running pod" in out
    assert "not checked, requires a running sync pod" in out
    # sesinetd is unreachable in the sandbox (socket blocked) -- that's a
    # real FAIL doctor should surface, not something this test should hide.
    assert rc == 1
    assert "[FAIL]" in out


# -- storage --------------------------------------------------------------


def test_storage_rm_without_force_refuses_client_side_exits_2(tmp_path, monkeypatch, capsys):
    """--force is required for ANY deletion now, not just to override
    housekeeping's own outputs-pending guard: a project with nothing
    pending used to delete immediately with no confirmation at all. This
    must refuse before ever touching a pod (brief's own required
    scenario: storage rm without --force -> exit 2)."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    def _must_not_connect(*a, **k):
        raise AssertionError("must not connect to a sync pod without --force")

    monkeypatch.setattr(cli, "_connect_sync_pod", _must_not_connect)

    rc = cli.main(["storage", "rm", "may/shotA"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--force" in err


def test_storage_rm_with_force_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("rm may/shotA --force", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/projects/may/shotA", "bytes_freed": 12345}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "rm", "may/shotA", "--force"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "freed" in out


def test_storage_rm_with_force_but_server_still_refuses_exits_2(tmp_path, monkeypatch, capsys):
    """--force reaches housekeeping too, but housekeeping can still say no
    (protected path, not found) -- that must surface as exit 2, not 0."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("rm may/shotA --force", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": False, "error": "protected path", "path": "/workspace/houdini"}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "rm", "may/shotA", "--force"])
    assert rc == 2
    assert "protected path" in capsys.readouterr().err


def test_storage_rm_shell_quotes_the_project_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("python3 /opt/rpfarm/housekeeping.py rm 'may/shot A' --force", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/projects/may/shot A", "bytes_freed": 1}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "rm", "may/shot A", "--force"])
    assert rc == 0
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py rm 'may/shot A' --force"]


def test_storage_prune_defaults_to_dry_run_and_shows_yes_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("prune --older-days 30 --dry-run", {
            "exit_code": 0,
            "stdout": json.dumps({
                "candidates": [{"user": "may", "project": "old", "bytes": 100, "age_days": 45.0}],
                "deleted": False,
                "boot_logs_rotated": [],
            }),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "prune"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would delete 1 project(s) -- pass --yes to actually delete" in out
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py prune --older-days 30 --dry-run"]


def test_storage_prune_with_yes_actually_deletes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("prune --older-days 30", {
            "exit_code": 0,
            "stdout": json.dumps({"candidates": [], "deleted": True, "boot_logs_rotated": []}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "prune", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "deleted 0 project(s)" in out
    # --dry-run must NOT be sent to housekeeping once --yes overrides it.
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py prune --older-days 30"]


def test_storage_du_prints_children(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("du /workspace/projects/may", {
            "exit_code": 0,
            "stdout": json.dumps([{"path": "/workspace/projects/may/shotA", "bytes": 2048}]),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "du", "/workspace/projects/may"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shotA" in out and "2.0KB" in out
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py du /workspace/projects/may"]


def test_storage_ls_passes_volume_size_gb(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("ls --volume-size-gb 50", {
            "exit_code": 0,
            "stdout": json.dumps({
                "zones": {"houdini": 100}, "projects": [],
                "volume": {"used": 100, "total": 50 * 2**30, "used_pct": 0.0}, "partial": False,
            }),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))
    monkeypatch.setattr(rppkg, "get_volume_size_gb", lambda api, cfg: 50)

    rc = cli.main(["storage", "ls"])
    assert rc == 0
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py ls --volume-size-gb 50"]


def test_storage_recreate_creates_volume_and_updates_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, volume_id="oldvol", datacenter="EU-RO-1")

    handlers = {
        ("POST", "/networkvolumes"): (200, json.dumps({"id": "newvol", "size": 100, "dataCenterId": "EU-RO-1"}).encode()),
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))

    rc = cli.main(["storage", "recreate", "--size", "100"])
    assert rc == 0
    cfg = rpcfg.load()
    assert cfg.volume_id == "newvol"
    out = capsys.readouterr().out
    assert "created new volume newvol" in out
    assert "networkvolumes/oldvol" in out  # the printed delete-old-volume command


def test_storage_recreate_with_tar_and_version_installs_houdini(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, volume_id="oldvol")

    handlers = {
        ("POST", "/networkvolumes"): (200, json.dumps({"id": "newvol", "size": 100, "dataCenterId": "EU-RO-1"}).encode()),
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))

    captured = {}

    def fake_houdini_install(ns):
        captured["tar"] = ns.tar
        captured["version"] = ns.version
        return 0

    monkeypatch.setattr(cli, "cmd_houdini_install", fake_houdini_install)

    rc = cli.main(["storage", "recreate", "--size", "100", "--tar", "/tmp/houdini.tar.gz", "--version", "22.0.393"])
    assert rc == 0
    assert captured == {"tar": "/tmp/houdini.tar.gz", "version": "22.0.393"}


# -- houdini ----------------------------------------------------------------


def test_houdini_ls_prints_versions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("houdini ls", {
            "exit_code": 0,
            "stdout": json.dumps({"versions": [{"version": "22.0.393", "bytes": 10 * 2**30}], "partial": False}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["houdini", "ls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "22.0.393" in out and "10.0GB" in out
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py houdini ls"]


def test_houdini_rm_defaults_to_dry_run_without_yes(tmp_path, monkeypatch, capsys):
    """Ruling R30: houdini rm can delete tens of GB, so it gets the same
    default-safe treatment as storage prune -- --dry-run is sent even when
    the flag isn't spelled out, and the CLI never claims anything was
    actually freed without --yes."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("houdini rm 20.5.684 --dry-run", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/houdini/20.5.684", "bytes_freed": 5 * 2**30, "dry_run": True}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["houdini", "rm", "20.5.684"])  # no --dry-run, no --yes
    assert rc == 0
    out = capsys.readouterr().out
    assert "would free" in out and "5.0GB" in out and "--yes" in out
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py houdini rm 20.5.684 --dry-run"]


def test_houdini_rm_dry_run_flag_is_an_inert_alias_for_the_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("houdini rm 20.5.684 --dry-run", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/houdini/20.5.684", "bytes_freed": 5 * 2**30, "dry_run": True}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["houdini", "rm", "20.5.684", "--dry-run"])
    assert rc == 0
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py houdini rm 20.5.684 --dry-run"]


def test_houdini_rm_with_yes_actually_deletes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("houdini rm 20.5.684", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/houdini/20.5.684", "bytes_freed": 5 * 2**30, "dry_run": False}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["houdini", "rm", "20.5.684", "--yes"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "freed" in out and "would free" not in out
    # --dry-run must NOT be sent to housekeeping once --yes overrides it.
    assert fake_client.calls == ["python3 /opt/rpfarm/housekeeping.py houdini rm 20.5.684"]


def test_houdini_rm_not_found_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("houdini rm 9.9.9", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": False, "error": "not found", "path": "/workspace/houdini/9.9.9"}),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["houdini", "rm", "9.9.9"])
    assert rc == 2


def test_houdini_install_uploads_tar_and_runs_installer(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    tar_path = tmp_path / "houdini-22.0.393-linux_x86_64.tar.gz"
    tar_path.write_bytes(b"fake-tarball-contents")

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester", "publicIp": "1.2.3.4", "portMappings": {"22": 40022}}
    fake_client = FakeWorkerClient([])  # run_upload_item is stubbed out below, no real .exec needed
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))
    monkeypatch.setattr(rppkg, "maybe_grow_volume", lambda api, cfg, client, needed, log=None: "ok")

    captured = {}

    def fake_run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb=None):
        captured["item"] = item
        captured["compress"] = compress
        return {"files": 1, "bytes": len(b"fake-tarball-contents"), "seconds": 0.01}

    monkeypatch.setattr(rppkg, "run_upload_item", fake_run_upload_item)

    rc = cli.main(["houdini", "install", "--tar", str(tar_path), "--version", "22.0.393"])
    assert rc == 0

    item = captured["item"]
    assert item["remote_root"] == "/workspace/apps/dist"
    assert item["files"][0][1] == "/workspace/apps/dist/" + tar_path.name
    assert "22.0.393" in item["post_command"]
    assert "/workspace/houdini/22.0.393" in item["post_command"]
    assert captured["compress"] is False
    out = capsys.readouterr().out
    assert "Houdini 22.0.393 installed" in out


def test_houdini_install_missing_tar_fails_before_connecting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no connect")))

    rc = cli.main(["houdini", "install", "--tar", str(tmp_path / "nope.tar.gz"), "--version", "22.0.393"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_stage_tar_from_sftp_url_builds_rclone_copyto_command(tmp_path):
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        # rclone copyto's destination is the last argument.
        with open(cmd[-1], "wb") as f:
            f.write(b"staged")
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    local = cli._stage_tar_from_sftp_url(
        "sftp://may@mayfx02/home/may/Downloads/houdini-22.0.393.tar.gz",
        "rclone",
        str(tmp_path),
        run=fake_run,
        key_file="/keys/id_rsa",
    )
    assert os.path.basename(local) == "houdini-22.0.393.tar.gz"
    assert open(local, "rb").read() == b"staged"
    cmd = calls[0]
    assert cmd[0] == "rclone" and cmd[1] == "copyto"
    assert cmd[2] == (
        ":sftp,host=mayfx02,user=may,key_file=/keys/id_rsa"
        ":/home/may/Downloads/houdini-22.0.393.tar.gz"
    )


def _fake_sftp_run(calls, ok_when):
    """rclone stub: succeeds only for the remote ``ok_when`` matches,
    otherwise fails the way rclone does when a key is rejected."""
    import subprocess as _sp

    def run(cmd, capture_output, text):
        calls.append(cmd)
        if ok_when(cmd[2]):
            with open(cmd[-1], "wb") as f:
                f.write(b"staged")
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        return _sp.CompletedProcess(
            cmd, 1, stdout="", stderr="NewFs: couldn't connect SSH: ssh: handshake failed: ssh: unable to authenticate"
        )

    return run


def test_stage_tar_from_sftp_url_walks_ssh_identities(tmp_path, monkeypatch):
    """Task 14: rclone's sftp backend reads neither ~/.ssh/config nor the
    stock identity files, and unlike ``ssh`` it gives up after the single
    ``key_file`` it was given. With an empty agent (the normal state on the
    artist's Mac) and a host that accepts id_rsa rather than id_ed25519,
    staging has to try the identities in turn the way ssh does.
    """
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("wrong key")
    (home / ".ssh" / "id_rsa").write_text("right key")
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    calls = []
    run = _fake_sftp_run(calls, lambda remote: remote.endswith(f"key_file={home}/.ssh/id_rsa:/p/houdini.tar.gz"))
    local = cli._stage_tar_from_sftp_url("sftp://root@host/p/houdini.tar.gz", "rclone", str(tmp_path), run=run)

    assert open(local, "rb").read() == b"staged"
    tried = [c[2] for c in calls]
    assert tried[0] == ":sftp,host=host,user=root:/p/houdini.tar.gz"  # ssh-agent first
    assert f"key_file={home}/.ssh/id_ed25519" in tried[1]
    assert f"key_file={home}/.ssh/id_rsa" in tried[2]


def test_stage_tar_from_sftp_url_does_not_retry_non_auth_failure(tmp_path, monkeypatch):
    """A transfer that got past the handshake and then died must not be
    restarted against the next identity -- only auth failures are retried,
    because only those are known to have moved no bytes."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("key")
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

    import subprocess as _sp

    calls = []

    def run(cmd, capture_output, text):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: no space left on device")

    with pytest.raises(RuntimeError, match="no space left"):
        cli._stage_tar_from_sftp_url("sftp://root@host/p/t.tar.gz", "rclone", str(tmp_path), run=run)
    assert len(calls) == 1


def test_default_ssh_key_files_empty_when_no_identity(tmp_path):
    (tmp_path / ".ssh").mkdir()
    assert cli._default_ssh_key_files(home=str(tmp_path)) == []


def test_stage_tar_from_sftp_url_rejects_malformed_url(tmp_path):
    with pytest.raises(ValueError):
        cli._stage_tar_from_sftp_url("not-an-sftp-url", "rclone", str(tmp_path))


def test_storage_grow_rejects_bad_amount_syntax(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    rc = cli.main(["storage", "grow", "20"])  # missing the required '+'
    assert rc == 1
    assert "+N" in capsys.readouterr().err


def test_storage_grow_resizes_volume(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    handlers = {
        ("GET", "/networkvolumes/vol123"): (200, json.dumps({"id": "vol123", "size": 50}).encode()),
        ("PATCH", "/networkvolumes/vol123"): (200, json.dumps({"id": "vol123", "size": 70}).encode()),
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport(handlers))
    rc = cli.main(["storage", "grow", "+20"])
    assert rc == 0
    assert "50 GB -> 70 GB" in capsys.readouterr().out


# -- farm -----------------------------------------------------------------


def test_parse_pod_timestamp_handles_runpods_actual_format():
    """Confirmed live 2026-09-03 against a real GET /pods response --
    RunPod's createdAt is not ISO8601 despite the openapi spec saying
    "string" with no format."""
    ts = cli._parse_pod_timestamp("2026-09-03 21:19:39.775 +0000 UTC")
    assert ts == pytest.approx(1788470379.775, abs=0.01)


def test_parse_pod_timestamp_falls_back_to_iso8601():
    ts = cli._parse_pod_timestamp("2026-09-03T21:19:39Z")
    assert ts is not None


def test_parse_pod_timestamp_none_on_garbage():
    assert cli._parse_pod_timestamp("not a date") is None
    assert cli._parse_pod_timestamp(None) is None


def test_farm_status_computes_uptime_and_cost_from_real_pod_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, user="tester")
    pod = {
        "id": "gpu1", "name": "rpfarm-tester-shotA-abc123-0", "desiredStatus": "RUNNING",
        "costPerHr": 0.34, "createdAt": "2026-09-03 21:19:39.775 +0000 UTC",
    }
    monkeypatch.setattr(cli, "_transport", FakeTransport({("GET", "/pods"): (200, json.dumps([pod]).encode())}))
    monkeypatch.setattr(cli.time, "time", lambda: 1788470379.775 + 3600)  # exactly 1h later
    rc = cli.main(["farm", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1h00m" in out
    assert "$0.34" in out  # 1h * $0.34/h


def test_farm_status_prints_no_pods_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    monkeypatch.setattr(cli, "_transport", FakeTransport({("GET", "/pods"): (200, json.dumps([]).encode())}))
    rc = cli.main(["farm", "status"])
    assert rc == 0
    assert "no rpfarm pods running" in capsys.readouterr().out


def test_farm_kill_sync_terminates_matching_pod_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, user="tester")
    pods = [
        {"id": "sync1", "name": "rpfarm-sync-tester"},
        {"id": "gpu1", "name": "rpfarm-tester-shotA-abc123-0"},
    ]
    terminated = []

    def transport(method, url, body, headers):
        if method == "GET" and url.endswith("/pods"):
            return 200, json.dumps(pods).encode()
        if method == "DELETE":
            terminated.append(url)
            return 204, b""
        raise AssertionError((method, url))

    monkeypatch.setattr(cli, "_transport", transport)
    rc = cli.main(["farm", "kill", "--sync"])
    assert rc == 0
    assert terminated == ["https://rest.runpod.io/v1/pods/sync1"]
    assert "terminated rpfarm-sync-tester" in capsys.readouterr().out


def _farm_kill_transport(pods, terminated):
    def transport(method, url, body, headers):
        if method == "GET" and url.endswith("/pods"):
            return 200, json.dumps(pods).encode()
        if method == "DELETE":
            terminated.append(url)
            return 204, b""
        raise AssertionError((method, url))
    return transport


def test_farm_kill_all_only_terminates_this_users_own_pods(tmp_path, monkeypatch, capsys):
    """--all must never reach another user's cook pods or this user's own
    sync pod -- both are outside its documented scope."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, user="tester")
    pods = [
        {"id": "mine1", "name": "rpfarm-tester-shotA-abc123-0"},
        {"id": "mine2", "name": "rpfarm-tester-shotB-def456-0"},
        {"id": "sync1", "name": "rpfarm-sync-tester"},
        {"id": "other1", "name": "rpfarm-other-shotC-ghi789-0"},
    ]
    terminated = []
    monkeypatch.setattr(cli, "_transport", _farm_kill_transport(pods, terminated))

    rc = cli.main(["farm", "kill", "--all"])
    assert rc == 0
    assert sorted(terminated) == [
        "https://rest.runpod.io/v1/pods/mine1",
        "https://rest.runpod.io/v1/pods/mine2",
    ]


def test_farm_kill_everyone_terminates_every_rpfarm_pod(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path, user="tester")
    pods = [
        {"id": "mine1", "name": "rpfarm-tester-shotA-abc123-0"},
        {"id": "sync1", "name": "rpfarm-sync-tester"},
        {"id": "other1", "name": "rpfarm-other-shotC-ghi789-0"},
    ]
    terminated = []
    monkeypatch.setattr(cli, "_transport", _farm_kill_transport(pods, terminated))

    rc = cli.main(["farm", "kill", "--everyone"])
    assert rc == 0
    assert sorted(terminated) == sorted(f"https://rest.runpod.io/v1/pods/{p['id']}" for p in pods)


def test_farm_kill_no_flag_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    monkeypatch.setattr(cli, "_transport", FakeTransport({}))
    rc = cli.main(["farm", "kill"])
    assert rc == 1
    assert "--all" in capsys.readouterr().err


def test_farm_kill_pod_terminates_exact_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    terminated = []

    def transport(method, url, body, headers):
        if method == "DELETE":
            terminated.append(url)
            return 204, b""
        raise AssertionError((method, url))

    monkeypatch.setattr(cli, "_transport", transport)
    rc = cli.main(["farm", "kill", "--pod", "abc123"])
    assert rc == 0
    assert terminated == ["https://rest.runpod.io/v1/pods/abc123"]


# -- costs ------------------------------------------------------------------


def test_costs_prints_table_from_ledger_fixture(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    ledger_dir = tmp_path / "ledger" / "tester"
    ledger_dir.mkdir(parents=True)
    records = [
        {"cook_id": "c1", "user": "tester", "project": "shotA", "pod": "p1",
         "started": 1000, "ended": 1100, "duration_s": 100, "cost_est": 0.5},
        {"cook_id": "c1", "user": "tester", "project": "shotA", "pod": "p1",
         "started": 1100, "ended": 1300, "duration_s": 200, "cost_est": 1.0},
        {"cook_id": "c2", "user": "tester", "project": "shotB", "pod": "p2",
         "started": 2000, "ended": 2050, "duration_s": 50, "cost_est": 0.25},
    ]
    with open(ledger_dir / "c1.jsonl", "w") as f:
        for r in records[:2]:
            f.write(json.dumps(r) + "\n")
    with open(ledger_dir / "c2.jsonl", "w") as f:
        f.write(json.dumps(records[2]) + "\n")

    rc = cli.main(["costs", "--by", "project"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shotA" in out and "shotB" in out
    assert "$1.50" in out  # shotA: 0.5 + 1.0
    assert "$0.25" in out  # shotB
    assert "total: $1.75" in out


def test_costs_by_user_and_date_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    ledger_dir = tmp_path / "ledger" / "tester"
    ledger_dir.mkdir(parents=True)
    old = {"cook_id": "old", "user": "tester", "project": "shotA", "pod": "p1",
           "started": 0, "ended": 10, "duration_s": 10, "cost_est": 5.0}
    new = {"cook_id": "new", "user": "tester", "project": "shotA", "pod": "p1",
           "started": int(__import__("time").time()), "ended": None, "duration_s": 5, "cost_est": 0.1}
    with open(ledger_dir / "recs.jsonl", "w") as f:
        f.write(json.dumps(old) + "\n")
        f.write(json.dumps(new) + "\n")

    rc = cli.main(["costs", "--by", "user", "--since", "2020-01-01"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "$5.00" not in out  # the 1970 record is filtered out by --since
    assert "$0.10" in out


# -- housekeeping transport (final-review finding 3) -------------------------
#
# The destructive commands used to go over the synchronous /exec path with
# stated timeouts of 120/180s, while pod/worker.py clamps that path to
# EXEC_SYNC_CEILING_S = 90 and SIGKILLs the whole process group when it
# expires. `rpfarm houdini rm 20.5.684 --yes` on an ~11GB tree could
# therefore be killed mid-shutil.rmtree, leaving a half-deleted install
# that `houdini ls` still lists while the CLI reported a timeout.


def _housekeeping_cli(tmp_path, monkeypatch, responses):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)
    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient(responses)
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))
    return fake_client


def test_houdini_rm_deletes_over_the_detached_transport(tmp_path, monkeypatch):
    client = _housekeeping_cli(tmp_path, monkeypatch, [
        ("houdini rm 20.5.684", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/houdini/20.5.684", "bytes_freed": 11 * 2**30}),
            "stderr": "",
        }),
    ])
    assert cli.main(["houdini", "rm", "20.5.684", "--yes"]) == 0
    assert [c for c, _ in client.detached] == ["python3 /opt/rpfarm/housekeeping.py houdini rm 20.5.684"]


def test_houdini_rm_dry_run_is_detached_too(tmp_path, monkeypatch):
    """A dry run walks the same tree to size it; same ceiling problem."""
    client = _housekeeping_cli(tmp_path, monkeypatch, [
        ("houdini rm 20.5.684 --dry-run", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/houdini/20.5.684", "bytes_freed": 1}),
            "stderr": "",
        }),
    ])
    assert cli.main(["houdini", "rm", "20.5.684"]) == 0
    assert len(client.detached) == 1


def test_storage_rm_deletes_over_the_detached_transport(tmp_path, monkeypatch):
    client = _housekeeping_cli(tmp_path, monkeypatch, [
        ("rm may/shotA --force", {
            "exit_code": 0,
            "stdout": json.dumps({"ok": True, "path": "/workspace/projects/may/shotA", "bytes_freed": 1}),
            "stderr": "",
        }),
    ])
    assert cli.main(["storage", "rm", "may/shotA", "--force"]) == 0
    assert [c for c, _ in client.detached] == ["python3 /opt/rpfarm/housekeeping.py rm may/shotA --force"]


def test_storage_prune_deletes_over_the_detached_transport(tmp_path, monkeypatch):
    client = _housekeeping_cli(tmp_path, monkeypatch, [
        ("prune --older-days 30", {
            "exit_code": 0,
            "stdout": json.dumps({"candidates": [], "deleted": True, "boot_logs_rotated": []}),
            "stderr": "",
        }),
    ])
    assert cli.main(["storage", "prune", "--older-days", "30", "--yes"]) == 0
    assert [c for c, _ in client.detached] == ["python3 /opt/rpfarm/housekeeping.py prune --older-days 30.0"]


def test_storage_ls_walks_the_volume_over_the_detached_transport(tmp_path, monkeypatch):
    client = _housekeeping_cli(tmp_path, monkeypatch, [
        ("ls --volume-size-gb 50", {
            "exit_code": 0,
            "stdout": json.dumps({
                "zones": {"houdini": 100}, "projects": [],
                "volume": {"used": 100, "total": 50 * 2**30, "used_pct": 0.0}, "partial": False,
            }),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(rppkg, "get_volume_size_gb", lambda api, cfg: 50)
    assert cli.main(["storage", "ls"]) == 0
    assert len(client.detached) == 1


def test_synchronous_housekeeping_never_promises_past_the_pods_ceiling():
    """Whatever a caller asks for, the synchronous path may only state a
    timeout the pod will actually honour -- otherwise the CLI reports a
    timeout that has nothing to do with what the pod did."""
    seen = []

    class Recording:
        def exec(self, command, timeout_s=600):
            seen.append(timeout_s)
            return {"exit_code": 0, "stdout": "{}", "stderr": ""}

    cli._housekeeping_exec(Recording(), "du /workspace/projects", timeout_s=999)
    assert seen == [WorkerClient.EXEC_SYNC_CEILING_S]


def test_detached_housekeeping_deadline_is_only_a_watching_deadline():
    """The detached path may state any deadline it likes: reaching it stops
    this CLI waiting and never kills the command on the pod."""
    seen = []

    class Recording:
        def exec_wait(self, command, deadline_s, poll_s=None, **kw):
            seen.append(deadline_s)
            return {"exit_code": 0, "stdout": "{}", "stderr": ""}

    cli._housekeeping_exec(Recording(), "rm x --force", timeout_s=600, detach=True)
    assert seen == [600]


def test_housekeeping_json_survives_a_merged_detached_log():
    """A detached run's stdout and stderr land in one log file, so the JSON
    result can arrive with chatter in front of it."""
    payload = json.dumps({"ok": True, "bytes_freed": 7})
    assert cli._parse_housekeeping_json(payload) == {"ok": True, "bytes_freed": 7}
    assert cli._parse_housekeeping_json(f"warning: cache is cold\n{payload}\n") == {"ok": True, "bytes_freed": 7}
    assert cli._parse_housekeeping_json("") == {}
    assert cli._parse_housekeeping_json("not json at all") is None


def test_housekeeping_failure_message_survives_the_merged_log():
    """A detached run has no separate stderr -- everything the command said
    is in the log, which arrives as stdout. The failure report must fall
    back to it instead of printing a generic message."""
    import io as _io

    buf = _io.StringIO()
    cli._report_housekeeping_failure({"exit_code": 1, "stdout": "outputs pending", "stderr": ""}, stream=buf)
    assert "outputs pending" in buf.getvalue()
