import json
import os
import stat

import pytest

from rpfarm import cli
from rpfarm import config as rpcfg
from rpfarm import houdini_local


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
    """Stand-in for WorkerClient: routes .exec() by substring of the
    command so tests don't need a live pod."""

    def __init__(self, responses):
        self._responses = responses  # list of (substring, result_dict)
        self.calls = []

    def exec(self, command, timeout_s=600):
        self.calls.append(command)
        for substr, result in self._responses:
            if substr in command:
                return result
        raise AssertionError(f"no fake response for exec({command!r})")


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


def test_storage_rm_without_force_on_outputs_pending_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_cfg(tmp_path)

    fake_pod = {"id": "sync1", "name": "rpfarm-sync-tester"}
    fake_client = FakeWorkerClient([
        ("rm may/shotA", {
            "exit_code": 0,
            "stdout": json.dumps({
                "ok": False,
                "error": "outputs pending, not downloaded -- pass --force to delete anyway",
                "path": "/workspace/projects/may/shotA",
                "outputs_pending": True,
            }),
            "stderr": "",
        }),
    ])
    monkeypatch.setattr(cli, "_connect_sync_pod", lambda api, cfg, token, log=print: (fake_pod, fake_client))

    rc = cli.main(["storage", "rm", "may/shotA"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "outputs pending" in err


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
