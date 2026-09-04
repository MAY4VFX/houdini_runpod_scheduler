"""Tests for ``rpfarm.smoke`` -- everything in it that does not need Houdini.

The cook itself cannot be unit tested (it needs ``hou``, a GPU pod and money),
so what is covered here is the part that decides whether a live run *passed*:
the output and ledger verification, the stage timing, the pod watcher and the
cleanup path. A bug in any of those would either fail a good run or, much
worse, pass a run that rendered nothing -- so they get the same treatment as
the rest of the package.

Every test that touches ``rpfarm.config`` isolates ``RPFARM_HOME``; the
session-wide fixture in ``conftest.py`` isolates ``HOUDINI_USER_PREF_DIR``.
"""

import json
import os
import time

import pytest

from rpfarm import cli
from rpfarm import config as rpcfg
from rpfarm import houdini_local
from rpfarm import smoke


# -- fakes --------------------------------------------------------------------


class FakeItem:
    """The bits of ``pdg.WorkItem`` the report/timing helpers touch."""

    def __init__(self, name, state, attribs=None, duration=1.0):
        self.name = name
        self.state = "workItemState." + state
        self._attribs = attribs or {}
        self.cookDuration = duration
        self.logMessages = ""
        self.logURI = ""

    def attribValue(self, name):
        if name not in self._attribs:
            raise RuntimeError("no attribute " + name)
        return self._attribs[name]

    def stringAttribValue(self, name):
        return str(self._attribs.get(name, ""))


class FakePDGNode:
    def __init__(self, items):
        self.workItems = items


class FakeNode:
    def __init__(self, name, items=(), errors=(), warnings=()):
        self._name = name
        self._items = list(items)
        self._errors = list(errors)
        self._warnings = list(warnings)

    def name(self):
        return self._name

    def path(self):
        return "/obj/topnet1/" + self._name

    def getPDGNode(self):
        return FakePDGNode(self._items)

    def errors(self):
        return self._errors

    def warnings(self):
        return self._warnings


def collector():
    lines = []
    return lines, lines.append


# -- report_items / node_messages ---------------------------------------------


def test_report_items_counts_successes_and_prints_attributes():
    node = FakeNode("upload", [
        FakeItem("upload_000", "CookedSuccess", {"bytes": 1024, "files": 3, "mbps": 1.5}),
        FakeItem("upload_001", "CookedFail"),
    ])
    lines, log = collector()
    succeeded, total, items = smoke.report_items(node, log)
    assert (succeeded, total) == (1, 2)
    assert len(items) == 2
    text = "\n".join(lines)
    assert "bytes=1024" in text and "files=3" in text and "mbps=1.50" in text
    # A missing attribute is omitted, not an error.
    assert "seconds=" not in text


def test_report_items_survives_a_node_that_never_generated():
    class Ungenerated(FakeNode):
        def getPDGNode(self):
            return None

    lines, log = collector()
    assert smoke.report_items(Ungenerated("render"), log) == (0, 0, [])


def test_node_messages_reports_errors_and_skips_missing_nodes():
    lines, log = collector()
    smoke.node_messages([FakeNode("a", errors=["boom"], warnings=["hmm"]), None], log)
    text = "\n".join(lines)
    assert "error: boom" in text and "warning: hmm" in text


# -- StageTimer ---------------------------------------------------------------


def test_stage_timer_records_first_running_and_last_done():
    running = FakeItem("render_1", "Cooking")
    node = FakeNode("render", [running])
    timer = smoke.StageTimer({"render": node})

    timer.poll()
    first, done = timer.window("render")
    assert first is not None and done is None

    running.state = "workItemState.CookedSuccess"
    timer.poll()
    first2, done2 = timer.window("render")
    assert first2 == first  # first_running is not moved by later polls
    assert done2 is not None and done2 >= first


def test_stage_timer_counts_an_item_that_finished_between_polls():
    """A sub-poll-interval task is never *seen* running; it must still open
    the stage, or its window would be (None, t) and the table would show '?'."""
    node = FakeNode("probe", [FakeItem("probe_1", "CookedSuccess")])
    timer = smoke.StageTimer({"probe": node})
    timer.poll()
    first, done = timer.window("probe")
    assert first is not None and done is not None


def test_stage_timer_last_done_tracks_the_slowest_item():
    a = FakeItem("render_1", "CookedSuccess")
    b = FakeItem("render_2", "Cooking")
    node = FakeNode("render", [a, b])
    timer = smoke.StageTimer({"render": node})
    timer.poll()
    _first, done_a = timer.window("render")
    time.sleep(0.01)
    b.state = "workItemState.CookedSuccess"
    timer.poll()
    _first, done_b = timer.window("render")
    assert done_b > done_a


def test_stage_seconds_leaves_unreached_stages_as_none():
    timer = smoke.StageTimer({})
    stages = smoke._stage_seconds(timer, cook_started=time.time(), elapsed=12.5)
    assert stages["cook"] == 12.5
    assert stages["render"] is None
    assert stages["download"] is None


# -- state_tracker ------------------------------------------------------------


def test_state_tracker_logs_each_transition_once():
    item = FakeItem("render_1", "Waiting")
    node = FakeNode("render", [item])
    lines, log = collector()
    on_poll = smoke.state_tracker(node, log)

    on_poll(0.0)
    on_poll(0.5)
    item.state = "workItemState.CookedSuccess"
    on_poll(1.0)

    transitions = [ln for ln in lines if "->" in ln]
    assert len(transitions) == 2
    assert transitions[0].endswith("Waiting")
    assert transitions[1].endswith("CookedSuccess")


def test_state_tracker_throttles_the_heartbeat():
    node = FakeNode("render", [FakeItem("render_1", "Cooking")])
    lines, log = collector()
    on_poll = smoke.state_tracker(node, log, heartbeat_every=30.0)
    for elapsed in (0.0, 1.0, 2.0, 40.0):
        on_poll(elapsed)
    beats = [ln for ln in lines if "heartbeat" in ln]
    assert len(beats) == 2  # t=0 and t=40, not one per poll


# -- ledger -------------------------------------------------------------------


def _write_ledger(home, cook_id, rows):
    d = home / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{cook_id}.jsonl"
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def test_read_ledger_file_skips_blank_and_corrupt_lines(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n')
    assert smoke.read_ledger_file(str(path)) == [{"a": 1}, {"b": 2}]


def test_read_ledger_file_on_a_missing_file_is_empty(tmp_path):
    assert smoke.read_ledger_file(str(tmp_path / "nope.jsonl")) == []


def test_verify_ledger_accepts_a_complete_cook(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_ledger(tmp_path, "abcd1234", [
        {"work_item": 1, "exit_code": 0},
        {"work_item": 2, "exit_code": 0},
        {"work_item": 3, "exit_code": 0},
        {"record": "cook_summary", "cost_est": 0.0123},
    ])
    lines, log = collector()
    ok, cost, tasks = smoke._verify_ledger("abcd1234", time.time() - 60, log)
    assert ok is True
    assert cost == pytest.approx(0.0123)
    assert len(tasks) == 3


def test_verify_ledger_rejects_a_non_zero_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_ledger(tmp_path, "abcd1234", [
        {"work_item": 1, "exit_code": 0},
        {"work_item": 2, "exit_code": 3},
        {"work_item": 3, "exit_code": 0},
        {"record": "cook_summary", "cost_est": 0.01},
    ])
    lines, log = collector()
    ok, _cost, _tasks = smoke._verify_ledger("abcd1234", time.time() - 60, log)
    assert ok is False
    assert any("non-zero exit code" in ln for ln in lines)


def test_verify_ledger_rejects_a_missing_cook_summary(tmp_path, monkeypatch):
    """No summary means onStopCook never finished -- pods may still be up."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_ledger(tmp_path, "abcd1234", [
        {"work_item": i, "exit_code": 0} for i in range(3)
    ])
    lines, log = collector()
    ok, cost, _tasks = smoke._verify_ledger("abcd1234", time.time() - 60, log)
    assert ok is False and cost is None
    assert any("cook_summary" in ln for ln in lines)


def test_verify_ledger_rejects_too_few_task_records(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_ledger(tmp_path, "abcd1234", [
        {"work_item": 1, "exit_code": 0},
        {"record": "cook_summary", "cost_est": 0.01},
    ])
    lines, log = collector()
    ok, _cost, tasks = smoke._verify_ledger("abcd1234", time.time() - 60, log)
    assert ok is False and len(tasks) == 1


def test_verify_ledger_falls_back_when_the_cook_id_is_unknown(tmp_path, monkeypatch):
    """The cook can die before onStartCook mints an id; the run's own ledger
    file is still the best evidence available and must not be ignored."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write_ledger(tmp_path, "deadbeef", [
        {"work_item": i, "exit_code": 0} for i in range(3)
    ] + [{"record": "cook_summary", "cost_est": 0.02}])
    lines, log = collector()
    ok, cost, tasks = smoke._verify_ledger(None, time.time() - 60, log)
    assert ok is True and cost == pytest.approx(0.02) and len(tasks) == 3


def test_verify_ledger_reports_no_ledger_at_all(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    (tmp_path / "ledger").mkdir()
    lines, log = collector()
    ok, cost, tasks = smoke._verify_ledger(None, time.time(), log)
    assert ok is False and cost is None and tasks == []


def test_verify_ledger_ignores_a_ledger_from_an_earlier_run(tmp_path, monkeypatch):
    """Without the mtime window, yesterday's cook would satisfy today's run."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    path = _write_ledger(tmp_path, "0ldc00k", [
        {"work_item": 1, "exit_code": 0}, {"record": "cook_summary", "cost_est": 1.0}])
    old = time.time() - 86400
    os.utime(path, (old, old))
    lines, log = collector()
    ok, _cost, _tasks = smoke._verify_ledger(None, time.time(), log)
    assert ok is False


# -- output verification -------------------------------------------------------


def _make_outputs(run_dir, frames=smoke.EXPECTED_FRAMES, probe=True, size=64):
    render = run_dir / smoke.RENDER_SUBDIR
    render.mkdir(parents=True, exist_ok=True)
    for i in range(1, frames + 1):
        (render / f"smoke.{i:04d}.exr").write_bytes(b"x" * size)
    if probe:
        probe_dir = run_dir / smoke.PROBE_SUBDIR
        probe_dir.mkdir(parents=True, exist_ok=True)
        (probe_dir / "probe_1.txt").write_text("rpfarm smoke probe\n")


def test_verify_outputs_accepts_a_full_fresh_run(tmp_path):
    _make_outputs(tmp_path)
    lines, log = collector()
    ok, frames, probes = smoke._verify_outputs(str(tmp_path), time.time() - 60, log)
    assert ok is True
    assert len(frames) == smoke.EXPECTED_FRAMES and len(probes) == 1


def test_verify_outputs_rejects_stale_frames(tmp_path):
    """The project directory on the volume has a fixed name, so a cook that
    rendered nothing could otherwise "pass" on the previous run's frames."""
    _make_outputs(tmp_path)
    old = time.time() - 86400
    for name in os.listdir(tmp_path / smoke.RENDER_SUBDIR):
        os.utime(tmp_path / smoke.RENDER_SUBDIR / name, (old, old))
    lines, log = collector()
    ok, _frames, _probes = smoke._verify_outputs(str(tmp_path), time.time(), log)
    assert ok is False
    assert any("STALE" in ln for ln in lines)


def test_verify_outputs_rejects_a_missing_frame(tmp_path):
    _make_outputs(tmp_path, frames=smoke.EXPECTED_FRAMES - 1)
    lines, log = collector()
    ok, frames, _probes = smoke._verify_outputs(str(tmp_path), time.time() - 60, log)
    assert ok is False and len(frames) == smoke.EXPECTED_FRAMES - 1


def test_verify_outputs_rejects_a_zero_byte_frame(tmp_path):
    _make_outputs(tmp_path, size=0)
    lines, log = collector()
    ok, _frames, _probes = smoke._verify_outputs(str(tmp_path), time.time() - 60, log)
    assert ok is False


def test_verify_outputs_rejects_a_missing_probe_file(tmp_path):
    _make_outputs(tmp_path, probe=False)
    lines, log = collector()
    ok, _frames, probes = smoke._verify_outputs(str(tmp_path), time.time() - 60, log)
    assert ok is False and probes == []


# -- pod watcher ---------------------------------------------------------------


def test_pod_watcher_records_rate_gpu_and_first_public_ip():
    cfg = rpcfg.Config(api_key="k", user="may", volume_id="v", template_id="t")
    watcher = smoke.PodWatcher(cfg)
    watcher._record({"id": "p1", "name": "rpfarm-may-smoke-0", "costPerHr": 0.25,
                     "machine": {"gpuTypeId": "NVIDIA RTX A4500"}})
    entry = watcher.pods["p1"]
    assert entry["rate"] == pytest.approx(0.25)
    assert entry["gpu"] == "NVIDIA RTX A4500"
    assert entry["first_ip"] is None

    watcher._record({"id": "p1", "name": "rpfarm-may-smoke-0", "costPerHr": 0.25,
                     "publicIp": "1.2.3.4"})
    assert watcher.pods["p1"]["first_ip"] is not None
    first_ip = watcher.pods["p1"]["first_ip"]

    watcher._record({"id": "p1", "name": "rpfarm-may-smoke-0", "publicIp": "1.2.3.4"})
    assert watcher.pods["p1"]["first_ip"] == first_ip  # first, not latest
    # A later payload without costPerHr must not zero the rate we already saw.
    assert watcher.pods["p1"]["rate"] == pytest.approx(0.25)


# -- pod cleanup ---------------------------------------------------------------


class FakeApi:
    def __init__(self, pods, fail_terminate=()):
        self._pods = list(pods)
        self._fail = set(fail_terminate)
        self.terminated = []

    def list_pods(self, prefix=""):
        return [p for p in self._pods if p.get("name", "").startswith(prefix)]

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)
        if pod_id in self._fail:
            raise RuntimeError("nope")
        self._pods = [p for p in self._pods if p["id"] != pod_id]


def _patch_api(monkeypatch, api):
    cfg = rpcfg.Config(api_key="k", user="may", volume_id="v", template_id="t")
    monkeypatch.setattr(smoke, "_api", lambda c=None: (cfg, api))
    return cfg


def test_terminate_all_pods_kills_every_rpfarm_pod_including_sync(monkeypatch):
    api = FakeApi([
        {"id": "p1", "name": "rpfarm-sync-may"},
        {"id": "p2", "name": "rpfarm-may-smoke-ab12-0"},
    ])
    _patch_api(monkeypatch, api)
    lines, log = collector()
    remaining = smoke.terminate_all_pods(log, settle=0)
    assert sorted(api.terminated) == ["p1", "p2"]
    assert remaining == []


def test_terminate_all_pods_reports_a_pod_it_could_not_kill(monkeypatch):
    api = FakeApi([{"id": "p1", "name": "rpfarm-sync-may"}], fail_terminate=["p1"])
    _patch_api(monkeypatch, api)
    lines, log = collector()
    remaining = smoke.terminate_all_pods(log, settle=0)
    assert [p["id"] for p in remaining] == ["p1"]
    assert any("MAY STILL BE BILLING" in ln for ln in lines)


def test_terminate_all_pods_never_raises_when_runpod_is_unreachable(monkeypatch):
    class Dead:
        def list_pods(self, prefix=""):
            raise RuntimeError("network down")

    _patch_api(monkeypatch, Dead())
    lines, log = collector()
    assert smoke.terminate_all_pods(log, settle=0) == []
    assert any("CLEANUP FAILED" in ln for ln in lines)


def test_list_farm_pods_never_raises(monkeypatch):
    class Dead:
        def list_pods(self, prefix=""):
            raise RuntimeError("network down")

    _patch_api(monkeypatch, Dead())
    lines, log = collector()
    assert smoke.list_farm_pods(log) == []
    assert any("pod listing failed" in ln for ln in lines)


# -- houdini selection ---------------------------------------------------------


class FakeInstall:
    def __init__(self, version, hython, pref_dir):
        self.version = version
        self.hython = hython
        self.user_pref_dir = pref_dir


def test_pick_houdini_skips_an_install_without_hython(monkeypatch, tmp_path):
    good = FakeInstall("22.0.368", tmp_path / "hython", tmp_path)
    monkeypatch.setattr(houdini_local, "find_houdini_installations",
                        lambda: [FakeInstall("21.0.1", None, tmp_path), good])
    assert smoke._pick_houdini() is good


def test_pick_houdini_raises_when_there_is_none(monkeypatch):
    monkeypatch.setattr(houdini_local, "find_houdini_installations", lambda: [])
    with pytest.raises(RuntimeError, match="no local Houdini"):
        smoke._pick_houdini()


def test_check_hdas_lists_exactly_what_is_missing(tmp_path):
    otls = tmp_path / "otls"
    otls.mkdir()
    for name in houdini_local.HDA_NAMES[:2]:
        (otls / f"{name}.hda").write_text("x")
    inst = FakeInstall("22.0.368", tmp_path / "hython", tmp_path)
    assert smoke._check_hdas(inst) == houdini_local.HDA_NAMES[2:]


# -- child process environment -------------------------------------------------


def test_child_env_puts_the_checkout_first_on_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    env = smoke._child_env("/repo", "/run")
    assert env["PYTHONPATH"].split(os.pathsep)[0] == "/repo"
    assert "/somewhere/else" in env["PYTHONPATH"]
    assert env["RPFARM_ROOT"] == "/repo"
    assert env["JOB"] == "/run"


def test_child_env_without_an_existing_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = smoke._child_env("/repo", "/run")
    assert env["PYTHONPATH"] == "/repo"


def test_hython_command_uses_dash_m():
    inst = FakeInstall("22.0.368", "/hfs/bin/hython", "/prefs")
    assert smoke._hython_command(inst, "/repo", "/run/payload.json") == [
        "/hfs/bin/hython", "-m", "rpfarm.smoke", "/run/payload.json"]


# -- cook id parsing -----------------------------------------------------------


class FakeParm:
    def __init__(self, text):
        self._text = text

    def evalAsString(self):
        return self._text


class FakeSched:
    def __init__(self, text):
        self._parm = FakeParm(text) if text is not None else None

    def parm(self, _name):
        return self._parm


def test_cook_id_read_from_the_status_text():
    sched = FakeSched("cook a82c7558  project smoke\nsync pod xyz\ncost: 0.010 USD")
    lines, log = collector()
    assert smoke._cook_id_from(sched, log) == "a82c7558"


def test_cook_id_none_when_the_status_text_is_empty():
    sched = FakeSched("")
    lines, log = collector()
    assert smoke._cook_id_from(sched, log) is None
    assert any("could not read the cook id" in ln for ln in lines)


def test_cook_id_none_when_the_parm_does_not_exist():
    assert smoke._cook_id_from(FakeSched(None), lambda _m: None) is None


@pytest.mark.parametrize("token,expected", [
    ("a82c7558", True), ("A82C7558", False), ("a82c755", False),
    ("a82c75588", False), ("zzzzzzzz", False), ("smoke", False),
])
def test_is_cook_id(token, expected):
    assert smoke._is_cook_id(token) is expected


# -- stage table ---------------------------------------------------------------


def test_stage_rows_renders_unmeasured_stages_as_question_marks():
    cfg = rpcfg.Config(api_key="k", user="may", volume_id="v", template_id="t")
    watcher = smoke.PodWatcher(cfg)
    watcher._record({"id": "p1", "name": "rpfarm-sync-may", "costPerHr": 0.06,
                     "publicIp": "1.2.3.4"})
    result = {"hip": "/run/smoke.hip", "upload_bytes": 2048,
              "stages": {"load": 3.0, "upload": 12.0}, "counts": {"upload": 1}}
    rows = smoke._stage_rows(result, watcher, time.time() - 5)
    labels = [r[0] for r in rows]
    assert labels[0] == "scene load" and labels[-1] == "total"
    assert "pod rpfarm-sync-may" in labels
    by_label = {r[0]: r[2] for r in rows}
    assert by_label["scene load"] == "3s"
    assert by_label["render"] == "?"  # never reached, not silently zero


# -- CLI wiring ----------------------------------------------------------------


def test_smoke_is_a_real_subcommand_with_the_documented_defaults():
    args = cli.build_parser().parse_args(["smoke"])
    assert args.command == "smoke"
    assert args.timeout == smoke.DEFAULT_TIMEOUT
    assert args.keep is False
    assert args.gpu is None and args.workdir is None and args.houdini is None


def test_smoke_accepts_a_gpu_override_and_keep():
    args = cli.build_parser().parse_args(["smoke", "--gpu", "NVIDIA RTX A4500", "--keep"])
    assert args.gpu == "NVIDIA RTX A4500" and args.keep is True


def test_smoke_without_a_config_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "empty"))
    args = cli.build_parser().parse_args(["smoke"])
    assert cli.main(["smoke"]) == 1
    assert "run `rpfarm setup`" in capsys.readouterr().err


# -- the fixture itself --------------------------------------------------------


def test_the_fixture_scene_is_checked_in_and_small():
    """A missing fixture turns `rpfarm smoke` into a confusing Houdini error
    three steps later; an over-large one bloats every clone."""
    path = os.path.join(str(houdini_local.repo_root()), smoke.FIXTURE_REL)
    assert os.path.isfile(path), f"{path} missing -- run scripts/build_smoke_fixture.py"
    assert os.path.getsize(path) < 2 * 2**20
