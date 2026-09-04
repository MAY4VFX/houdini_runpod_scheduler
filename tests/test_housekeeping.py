import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pod"))
import housekeeping as hk  # noqa: E402


def mk(tmp_path):
    for d in (
        "houdini/22.0.393/bin",
        "projects/may/shotA/render",
        "projects/may/shotB",
        "ledger",
        ".rpfarm",
    ):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "projects/may/shotA/render/f.exr").write_bytes(b"x" * 1000)
    (tmp_path / "projects/may/shotB/s.hip").write_bytes(b"y" * 10)
    return str(tmp_path)


# -- brief's own tests, verbatim --------------------------------------------


def test_ls_zones_and_projects(tmp_path):
    root = mk(tmp_path)
    out = hk.cmd_ls(root)
    assert out["zones"]["projects"] == 1010
    assert {p["project"] for p in out["projects"]} == {"shotA", "shotB"}
    assert next(p for p in out["projects"] if p["project"] == "shotA")["outputs_pending"] is True


def test_touch_and_rm_guard(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotA", "download")
    assert hk.cmd_ls(root)["projects"][0]["outputs_pending"] is False
    (tmp_path / "projects/may/shotA/render/g.exr").write_bytes(b"z")
    time.sleep(0.01)
    assert hk.cmd_rm(root, "may/shotA", force=False)["ok"] is False
    assert hk.cmd_rm(root, "may/shotA", force=True)["ok"] is True
    assert not (tmp_path / "projects/may/shotA").exists()


def test_prune_never_touches_protected(tmp_path):
    root = mk(tmp_path)
    res = hk.cmd_prune(root, older_days=0, dry_run=True)
    assert all("houdini" not in c["path"] for c in res["candidates"])


# -- du (Task 4 contract, must still work) ----------------------------------


def test_du_first_level_only(tmp_path):
    root = mk(tmp_path)
    rows = hk.du(os.path.join(root, "projects", "may"))
    by_path = {os.path.basename(r["path"]): r["bytes"] for r in rows}
    assert by_path == {"shotA": 1000, "shotB": 10}


def test_du_missing_path_returns_empty(tmp_path):
    assert hk.du(str(tmp_path / "nope")) == []


# -- ls: zones, index fields, ordering, missing zones ------------------------


def test_ls_missing_zones_are_zero(tmp_path):
    root = mk(tmp_path)
    out = hk.cmd_ls(root)
    assert out["zones"]["apps"] == 0
    assert out["zones"]["ledger"] == 0
    assert out["zones"]["houdini"] == 0  # empty bin/ dir, no files


def test_ls_projects_sorted_and_index_fields(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotB", "cook")
    projects = hk.cmd_ls(root)["projects"]
    assert [p["project"] for p in projects] == ["shotA", "shotB"]
    shotA, shotB = projects
    assert shotA["last_used"] is None and shotA["last_cook"] is None
    assert shotB["last_used"] is not None and shotB["last_cook"] is not None


def _volume_used_from_zones(root):
    return sum(hk._size(os.path.join(root, z)) or 0 for z in hk._ZONES)


def test_ls_volume_without_size_gb_is_null_not_disk_usage(tmp_path):
    # Ruling R27: shutil.disk_usage(root) inside a pod reports the backing
    # storage pool's capacity, not the volume's real size (confirmed live:
    # a real 50GB volume read back as ~2.14 PiB) -- never used for this.
    root = mk(tmp_path)
    volume = hk.cmd_ls(root)["volume"]
    assert volume["used"] == _volume_used_from_zones(root) == 1010
    assert volume["total"] is None
    assert volume["used_pct"] is None
    assert "note" in volume


def test_ls_volume_with_size_gb_computes_totals(tmp_path):
    root = mk(tmp_path)
    gb = 2**30
    volume = hk.cmd_ls(root, volume_size_gb=50)["volume"]
    assert volume["total"] == 50 * gb
    assert volume["used"] == _volume_used_from_zones(root)
    assert volume["used_pct"] == round(100.0 * volume["used"] / (50 * gb), 2)
    assert "note" not in volume


def test_ls_empty_root(tmp_path):
    out = hk.cmd_ls(str(tmp_path))
    assert out["zones"] == {"houdini": 0, "apps": 0, "projects": 0, "ledger": 0}
    assert out["projects"] == []


# -- touch: events, invalid input --------------------------------------------


def test_touch_events_set_distinct_fields(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotA", "cook")
    hk.cmd_touch(root, "may/shotA", "upload")
    entry = hk.cmd_ls(root)["projects"][0]
    assert entry["last_cook"] is not None
    assert entry["last_used"] is not None


def test_touch_default_event_is_cook(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_touch(root, "may/shotA")
    assert result["event"] == "cook"
    assert result["last_cook"] is not None


def test_touch_rejects_bad_event(tmp_path):
    root = mk(tmp_path)
    with pytest.raises(hk.HousekeepingError):
        hk.cmd_touch(root, "may/shotA", "bogus")


def test_touch_rejects_malformed_user_project(tmp_path):
    root = mk(tmp_path)
    for bad in ("shotA", "may/../etc", "../may/shotA", "may/shotA/extra", ""):
        with pytest.raises(hk.HousekeepingError):
            hk.cmd_touch(root, bad)


def test_touch_persists_across_calls(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotA", "upload")
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        data = json.load(f)
    assert "may/shotA" in data
    assert "last_upload" in data["may/shotA"]


# -- rm: not found, protected, traversal -------------------------------------


def test_rm_not_found(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_rm(root, "may/does-not-exist")
    assert result["ok"] is False
    assert result["error"] == "not found"


def test_rm_no_pending_outputs_succeeds_without_force(tmp_path):
    root = mk(tmp_path)
    # shotB has no render/geo/resultdata dir at all -- nothing pending.
    result = hk.cmd_rm(root, "may/shotB", force=False)
    assert result["ok"] is True
    assert not (tmp_path / "projects/may/shotB").exists()


def test_rm_rejects_path_traversal(tmp_path):
    root = mk(tmp_path)
    with pytest.raises(hk.HousekeepingError):
        hk.cmd_rm(root, "../houdini/22.0.393")


def test_rm_clears_index_entry(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotB", "cook")
    hk.cmd_rm(root, "may/shotB", force=True)
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        data = json.load(f)
    assert "may/shotB" not in data


# -- prune: age threshold, dry-run vs real, boot log rotation ---------------


def test_prune_dry_run_deletes_nothing(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotB", "cook")
    res = hk.cmd_prune(root, older_days=0, dry_run=True)
    assert res["deleted"] is False
    assert (tmp_path / "projects/may/shotB").exists()


def test_prune_real_run_deletes_candidates(tmp_path):
    root = mk(tmp_path)
    # shotA is excluded (outputs_pending); shotB has no outputs -> candidate.
    res = hk.cmd_prune(root, older_days=0, dry_run=False)
    assert res["deleted"] is True
    paths = {c["path"] for c in res["candidates"]}
    assert any(p.endswith(os.path.join("may", "shotB")) for p in paths)
    assert not (tmp_path / "projects/may/shotB").exists()
    assert (tmp_path / "projects/may/shotA").exists()  # pending, never touched


def test_prune_skips_recently_used(tmp_path):
    root = mk(tmp_path)
    hk.cmd_touch(root, "may/shotB", "cook")
    res = hk.cmd_prune(root, older_days=30, dry_run=True)
    assert res["candidates"] == []


def test_prune_rotates_old_boot_logs(tmp_path):
    root = mk(tmp_path)
    log_dir = tmp_path / "ledger" / "logs"
    log_dir.mkdir(parents=True)
    old_log = log_dir / "boot-old.log"
    new_log = log_dir / "boot-new.log"
    old_log.write_text("old")
    new_log.write_text("new")
    old_ts = time.time() - (hk._BOOT_LOG_RETENTION_DAYS + 1) * 86400
    os.utime(old_log, (old_ts, old_ts))

    res = hk.cmd_prune(root, older_days=9999, dry_run=True)
    rotated_paths = {r["path"] for r in res["boot_logs_rotated"]}
    assert str(old_log) in rotated_paths
    assert str(new_log) not in rotated_paths
    assert old_log.exists()  # dry-run: nothing actually deleted

    res = hk.cmd_prune(root, older_days=9999, dry_run=False)
    assert not old_log.exists()
    assert new_log.exists()


def test_prune_no_boot_logs_dir_is_fine(tmp_path):
    root = mk(tmp_path)
    res = hk.cmd_prune(root, older_days=9999, dry_run=True)
    assert res["boot_logs_rotated"] == []


# -- houdini ls/rm ------------------------------------------------------------


def test_houdini_ls(tmp_path):
    root = mk(tmp_path)
    (tmp_path / "houdini/22.0.393/bin/hexpand").write_bytes(b"x" * 42)
    out = hk.cmd_houdini_ls(root)
    assert out["versions"] == [{"version": "22.0.393", "bytes": 42}]


def test_houdini_rm(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_houdini_rm(root, "22.0.393")
    assert result["ok"] is True
    assert not (tmp_path / "houdini/22.0.393").exists()


def test_houdini_rm_not_found(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_houdini_rm(root, "99.9.999")
    assert result["ok"] is False


def test_houdini_rm_rejects_traversal(tmp_path):
    root = mk(tmp_path)
    with pytest.raises(hk.HousekeepingError):
        hk.cmd_houdini_rm(root, "../ledger")


# -- houdini ls/rm: v1's flat "legacy" install (spec 4.1) --------------------


def test_houdini_ls_groups_legacy_entries(tmp_path):
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    (tmp_path / "houdini/houdini.env").write_bytes(b"y" * 5)
    out = hk.cmd_houdini_ls(root)
    by_version = {v["version"]: v["bytes"] for v in out["versions"]}
    assert by_version["22.0.393"] == 0  # the real version dir, still separate
    assert by_version["legacy"] == 15  # bin/hexpand + houdini.env, summed


def test_houdini_ls_no_legacy_entry_when_only_proper_versions(tmp_path):
    root = mk(tmp_path)
    out = hk.cmd_houdini_ls(root)
    assert "legacy" not in {v["version"] for v in out["versions"]}


def test_houdini_rm_legacy_removes_only_non_version_entries(tmp_path):
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    (tmp_path / "houdini/houdini.env").write_bytes(b"y" * 5)
    result = hk.cmd_houdini_rm(root, "legacy")
    assert result["ok"] is True
    assert result["bytes_freed"] == 15
    assert not (tmp_path / "houdini/bin").exists()
    assert not (tmp_path / "houdini/houdini.env").exists()
    assert (tmp_path / "houdini/22.0.393").exists()  # real version untouched


def test_houdini_rm_legacy_not_found_when_nothing_to_remove(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_houdini_rm(root, "legacy")
    assert result["ok"] is False


def test_houdini_rm_legacy_invalidates_the_houdini_zone_size(tmp_path):
    """Task 14, seen live: deleting the 10.8GB legacy install pruned only
    the per-version `_houdini` cache, so the next `storage ls` still served
    the pre-deletion `_sizes["houdini"]` (23.4GB = stale legacy + the new
    install) and the volume read as half again as full as it was."""
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    hk.cmd_ls(root)  # populates _sizes["houdini"] with the pre-deletion figure
    assert "houdini" in hk._load_index(root)["_sizes"]

    hk.cmd_houdini_rm(root, "legacy")
    assert "houdini" not in hk._load_index(root).get("_sizes", {})


def test_houdini_rm_version_invalidates_the_houdini_zone_size(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    assert "houdini" in hk._load_index(root)["_sizes"]

    hk.cmd_houdini_rm(root, "22.0.393")
    assert "houdini" not in hk._load_index(root).get("_sizes", {})


def test_houdini_rm_dry_run_leaves_the_zone_size_cache_alone(tmp_path):
    """A dry run deletes nothing, so it must not throw away a good cached
    measurement either."""
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    hk.cmd_ls(root)
    hk.cmd_houdini_rm(root, "legacy", dry_run=True)
    assert "houdini" in hk._load_index(root)["_sizes"]


# -- sync-idle ----------------------------------------------------------------


def test_sync_idle_missing_file(tmp_path):
    root = mk(tmp_path)
    assert hk.cmd_sync_idle(root) == {"idle_seconds": None}


def test_sync_idle_reports_elapsed(tmp_path):
    root = mk(tmp_path)
    stamp_path = tmp_path / ".rpfarm" / "sync_last_used"
    stamp_path.write_text(str(int(time.time()) - 120))
    out = hk.cmd_sync_idle(root)
    assert out["idle_seconds"] >= 119


def test_sync_touch_writes_a_stamp_both_readers_parse(tmp_path):
    """The one writer's output must satisfy both readers.

    Final-review finding 1: the stamp has two readers that both parse the
    file's *content* as a float -- cmd_sync_idle here, and the scheduler
    HDA's _retireStaleSyncPod, which does `float(read_file(path).strip())`
    over the raw bytes. A bare `touch` (what rpfarm/packages.py used to
    run) leaves an empty file that neither can parse.
    """
    root = mk(tmp_path)
    out = hk.cmd_sync_touch(root)

    raw = (tmp_path / ".rpfarm" / "sync_last_used").read_text()
    assert raw.strip(), "a bare `touch` would leave this empty"
    # reader 1: the scheduler HDA, verbatim
    assert float(raw.strip()) == pytest.approx(out["last_used"], abs=0.01)
    # reader 2: housekeeping itself
    assert hk.cmd_sync_idle(root)["idle_seconds"] == pytest.approx(0.0, abs=5.0)


def test_sync_touch_creates_the_rpfarm_dir_if_missing(tmp_path):
    """A brand-new volume has no /workspace/.rpfarm yet."""
    root = str(tmp_path)
    hk.cmd_sync_touch(root)
    assert hk.cmd_sync_idle(root)["idle_seconds"] is not None


def test_sync_touch_overwrites_an_older_stamp(tmp_path):
    """`touch` on an existing file never rewrote its content -- this must."""
    root = mk(tmp_path)
    (tmp_path / ".rpfarm" / "sync_last_used").write_text(str(time.time() - 3600))
    hk.cmd_sync_touch(root)
    assert hk.cmd_sync_idle(root)["idle_seconds"] < 60


def test_sync_touch_is_reachable_from_the_command_line(tmp_path):
    """The command rpfarm.packages.SYNC_TOUCH_COMMAND actually sends."""
    root = str(tmp_path)
    monkeyroot = hk.DEFAULT_ROOT
    hk.DEFAULT_ROOT = root
    try:
        assert hk.main(["housekeeping.py", "sync-touch"]) == 0
        assert hk.cmd_sync_idle(root)["idle_seconds"] is not None
    finally:
        hk.DEFAULT_ROOT = monkeyroot


# -- protection: rm/prune never reach into houdini/ledger/.rpfarm -----------


def test_is_protected(tmp_path):
    root = str(tmp_path)
    assert hk._is_protected(root, os.path.join(root, "houdini"))
    assert hk._is_protected(root, os.path.join(root, "houdini", "22.0.393"))
    assert hk._is_protected(root, os.path.join(root, "ledger", "logs"))
    assert hk._is_protected(root, os.path.join(root, ".rpfarm", "index.json"))
    assert not hk._is_protected(root, os.path.join(root, "projects", "may", "shotA"))


# -- CLI entry point ----------------------------------------------------------


def test_main_du_prints_json(tmp_path, capsys):
    root = mk(tmp_path)
    rc = hk.main(["housekeeping.py", "du", os.path.join(root, "projects", "may")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["bytes"] for r in out} == {1000, 10}


def test_main_ls_with_root_flag(tmp_path, capsys):
    root = mk(tmp_path)
    rc = hk.main(["housekeeping.py", "ls", "--root", root])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["zones"]["projects"] == 1010


def test_main_unknown_touch_event_exits_nonzero(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hk, "DEFAULT_ROOT", str(tmp_path))
    rc = hk.main(["housekeeping.py", "touch", "may/shotA", "--event", "bogus"])
    assert rc == 2  # argparse rejects the bad choice before cmd_touch ever runs


def test_main_bad_command_exits_nonzero(capsys):
    rc = hk.main(["housekeeping.py", "not-a-command"])
    assert rc == 2


# -- Ruling R26: du -sb fast path -----------------------------------------


@pytest.fixture
def reset_du_flag():
    """`_DU_B_UNSUPPORTED` is a sticky module-level flag by design (Ruling
    R26) -- save/restore it so one test's mocked `du` behavior can't leak
    into another."""
    original = hk._DU_B_UNSUPPORTED
    yield
    hk._DU_B_UNSUPPORTED = original


def test_size_uses_du_sb_when_available(tmp_path, monkeypatch, reset_du_flag):
    hk._DU_B_UNSUPPORTED = False
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return subprocess_completed(returncode=0, stdout="424242\t/some/path\n", stderr="")

    monkeypatch.setattr(hk.subprocess, "run", fake_run)
    assert hk._size(str(tmp_path)) == 424242
    assert calls and calls[0][:2] == ["du", "-sb"]


def test_size_falls_back_to_walk_when_du_b_unsupported(tmp_path, monkeypatch, reset_du_flag):
    hk._DU_B_UNSUPPORTED = False
    (tmp_path / "f.txt").write_bytes(b"x" * 77)

    def fake_run(cmd, capture_output, text, timeout):
        return subprocess_completed(returncode=1, stdout="", stderr="du: illegal option -- b\n")

    monkeypatch.setattr(hk.subprocess, "run", fake_run)
    assert hk._size(str(tmp_path)) == 77
    assert hk._DU_B_UNSUPPORTED is True  # sticky: won't retry `du -sb` again this process


def test_size_returns_none_on_timeout_without_falling_back(tmp_path, monkeypatch, reset_du_flag):
    hk._DU_B_UNSUPPORTED = False

    def fake_run(cmd, capture_output, text, timeout):
        raise hk.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(hk.subprocess, "run", fake_run)
    assert hk._size(str(tmp_path), timeout_s=1) is None
    assert hk._DU_B_UNSUPPORTED is False  # a timeout says nothing about -b support


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def subprocess_completed(returncode, stdout, stderr):
    return _FakeCompletedProcess(returncode, stdout, stderr)


# -- Ruling R26: ls/houdini ls size caching --------------------------------


def test_ls_serves_cached_size_without_remeasuring(tmp_path, monkeypatch):
    root = mk(tmp_path)
    calls = {"n": 0}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        return real_size(path, timeout_s=timeout_s)

    monkeypatch.setattr(hk, "_size", counting_size)
    hk.cmd_ls(root)
    first = calls["n"]
    assert first > 0
    hk.cmd_ls(root)  # within max_age_s -- must be served from cache
    assert calls["n"] == first


def test_ls_refresh_forces_remeasure(tmp_path, monkeypatch):
    root = mk(tmp_path)
    calls = {"n": 0}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        return real_size(path, timeout_s=timeout_s)

    monkeypatch.setattr(hk, "_size", counting_size)
    hk.cmd_ls(root)
    first = calls["n"]
    hk.cmd_ls(root, refresh=True)
    assert calls["n"] > first


def test_ls_stale_cache_remeasures(tmp_path, monkeypatch):
    root = mk(tmp_path)
    calls = {"n": 0}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        return real_size(path, timeout_s=timeout_s)

    monkeypatch.setattr(hk, "_size", counting_size)
    hk.cmd_ls(root)
    first = calls["n"]
    hk.cmd_ls(root, max_age_s=0)  # everything counts as stale
    assert calls["n"] > first


def test_ls_partial_when_measurement_times_out(tmp_path, monkeypatch):
    root = mk(tmp_path)
    monkeypatch.setattr(hk, "_size", lambda path, timeout_s=hk._SIZE_TIMEOUT_S: None)
    out = hk.cmd_ls(root)
    assert out["partial"] is True
    assert out["zones"]["projects"] == 0  # no cache yet, timed out -> reports 0


def test_ls_partial_serves_stale_cache_instead_of_zero(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_ls(root)  # populate the cache for real first
    monkeypatch.setattr(hk, "_size", lambda path, timeout_s=hk._SIZE_TIMEOUT_S: None)
    out = hk.cmd_ls(root, refresh=True)  # force a re-measure attempt, which "times out"
    assert out["partial"] is True
    assert out["zones"]["projects"] == 1010  # served the last known-good value, not 0


def test_ls_out_of_budget_reports_partial(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)  # populate cache
    out = hk.cmd_ls(root, refresh=True, budget_s=0)  # no time left for any remeasure
    assert out["partial"] is True
    assert out["zones"]["projects"] == 1010  # stale cache served, not blocked


def test_houdini_ls_partial_and_cache_same_as_ls(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_houdini_ls(root)
    monkeypatch.setattr(hk, "_size", lambda path, timeout_s=hk._SIZE_TIMEOUT_S: None)
    out = hk.cmd_houdini_ls(root, refresh=True)
    assert out["partial"] is True
    by_version = {v["version"]: v["bytes"] for v in out["versions"]}
    assert by_version["22.0.393"] == 0  # stale cache (was 0 the first time too)


# -- houdini rm --dry-run ---------------------------------------------------


def test_houdini_rm_dry_run_does_not_delete_version(tmp_path):
    root = mk(tmp_path)
    result = hk.cmd_houdini_rm(root, "22.0.393", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert (tmp_path / "houdini/22.0.393").exists()


def test_houdini_rm_dry_run_legacy_does_not_delete(tmp_path):
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    result = hk.cmd_houdini_rm(root, "legacy", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["bytes_freed"] == 10
    assert (tmp_path / "houdini/bin").exists()
    assert (tmp_path / "houdini/22.0.393").exists()


# -- disk-usage (Ruling R26 review fix: maybe_grow_volume's fast path) ------


def test_disk_usage_shape_without_size_gb(tmp_path):
    root = mk(tmp_path)
    out = hk.cmd_disk_usage(root)
    assert set(out.keys()) == {"volume", "partial"}
    assert out["volume"]["used"] == _volume_used_from_zones(root)
    assert out["volume"]["total"] is None
    assert out["volume"]["used_pct"] is None
    assert "note" in out["volume"]


def test_disk_usage_with_size_gb(tmp_path):
    root = mk(tmp_path)
    gb = 2**30
    out = hk.cmd_disk_usage(root, volume_size_gb=50)
    assert out["volume"]["total"] == 50 * gb
    assert out["volume"]["used"] == _volume_used_from_zones(root)
    assert "note" not in out["volume"]


def test_disk_usage_reuses_ls_cache(tmp_path, monkeypatch):
    """R27: disk-usage's "used" is the same _sizes cache ls populates --
    a disk-usage call right after ls must not re-measure anything."""
    root = mk(tmp_path)
    calls = {"n": 0}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        return real_size(path, timeout_s=timeout_s)

    hk.cmd_ls(root)  # populates the _sizes cache with the real _size
    monkeypatch.setattr(hk, "_size", counting_size)
    hk.cmd_disk_usage(root, volume_size_gb=50)
    assert calls["n"] == 0


def test_main_disk_usage(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hk, "DEFAULT_ROOT", str(tmp_path))
    rc = hk.main(["housekeeping.py", "disk-usage", "--volume-size-gb", "50"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["volume"]["total"] == 50 * 2**30


def test_main_disk_usage_without_size_gb_is_null(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hk, "DEFAULT_ROOT", str(tmp_path))
    rc = hk.main(["housekeeping.py", "disk-usage"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["volume"]["total"] is None


def test_main_ls_accepts_budget_and_max_age_flags(tmp_path, capsys):
    root = mk(tmp_path)
    rc = hk.main(
        ["housekeeping.py", "ls", "--root", root, "--budget-s", "5", "--max-age-s", "0"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["zones"]["projects"] == 1010


# -- houdini rm reuses houdini ls's cache (review follow-up) ----------------


def test_houdini_rm_legacy_dry_run_reuses_houdini_ls_cache(tmp_path, monkeypatch):
    root = mk(tmp_path)
    (tmp_path / "houdini/bin").mkdir()
    (tmp_path / "houdini/bin/hexpand").write_bytes(b"x" * 10)
    hk.cmd_houdini_ls(root)  # populates the _houdini cache

    calls = {"n": 0}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        return real_size(path, timeout_s=timeout_s)

    monkeypatch.setattr(hk, "_size", counting_size)
    result = hk.cmd_houdini_rm(root, "legacy", dry_run=True)
    assert calls["n"] == 0  # served entirely from the houdini-ls-populated cache
    assert result["bytes_freed"] == 10


def test_houdini_rm_real_deletion_prunes_cache_entry(tmp_path):
    root = mk(tmp_path)
    hk.cmd_houdini_ls(root)  # populates the _houdini cache for 22.0.393
    hk.cmd_houdini_rm(root, "22.0.393")
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        data = json.load(f)
    assert "22.0.393" not in data.get("_houdini", {})


# -- fix round 2 "A" / fix round 3 "1": disk-usage uses the same plain
# staleness-based caching for every zone as ls/houdini ls -- no special
# "static zone" casing (round 2's first attempt at "A" tried that, but it
# made the once-per-cook warm-up unconditionally expensive too, since
# --refresh was the only way those zones were ever remeasured at all;
# round 3 walked it back to the uniform mechanism below). -----------------


def _count_size_calls(monkeypatch):
    calls = {"n": 0, "paths": []}
    real_size = hk._size

    def counting_size(path, timeout_s=hk._SIZE_TIMEOUT_S):
        calls["n"] += 1
        calls["paths"].append(path)
        return real_size(path, timeout_s=timeout_s)

    monkeypatch.setattr(hk, "_size", counting_size)
    return calls


def test_disk_usage_costs_nothing_when_everything_is_fresh(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_ls(root)  # populate every zone's cache once, for real
    calls = _count_size_calls(monkeypatch)
    hk.cmd_disk_usage(root, volume_size_gb=50)
    assert calls["n"] == 0  # nothing was stale -- served entirely from cache


def test_disk_usage_remeasures_stale_zones_including_houdini(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    calls = _count_size_calls(monkeypatch)
    hk.cmd_disk_usage(root, volume_size_gb=50, max_age_s=0)  # force everything stale
    assert any(p.endswith("houdini") for p in calls["paths"])
    assert any(p.endswith("projects") for p in calls["paths"])


def test_disk_usage_no_prior_cache_still_succeeds_without_partial(tmp_path):
    root = mk(tmp_path)
    # No prior ls/disk-usage call -- every zone is measured fresh for the
    # first time; on these tiny fixtures that succeeds well within budget.
    # partial means a *timed-out* remeasure, not merely "wasn't cached yet".
    out = hk.cmd_disk_usage(root, volume_size_gb=50)
    assert out["partial"] is False


def test_disk_usage_refresh_still_walks_everything(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    calls = _count_size_calls(monkeypatch)
    hk.cmd_disk_usage(root, volume_size_gb=50, refresh=True)
    assert any("houdini" in p for p in calls["paths"])


# -- fix round 3, "1": the warm-up call itself (disk-usage --budget-s N,
# deliberately no --refresh) must not force a re-walk of an already-fresh
# zone -- otherwise the warm-up becomes the same "always expensive" call
# --refresh was, just moved from every item to every cook. -----------------


def test_warmup_style_disk_usage_does_not_rewalk_fresh_zones(tmp_path, monkeypatch):
    """The exact shape of onSetupCook's _warmSizeCache() call: a generous
    budget, no --refresh. Run it twice back to back -- the second run
    (everything still fresh from the first) must cost zero _size() calls."""
    root = mk(tmp_path)
    hk.cmd_disk_usage(root, volume_size_gb=50, budget_s=90)  # first "warm-up": cold
    calls = _count_size_calls(monkeypatch)
    hk.cmd_disk_usage(root, volume_size_gb=50, budget_s=90)  # second "warm-up": warm
    assert calls["n"] == 0


# -- fix round 2, "B": touch invalidates the size cache on upload --------------


def test_touch_upload_invalidates_project_and_projects_zone_cache(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)  # populate _sizes for "projects" and "may/shotA"
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        before = json.load(f)
    assert "projects" in before["_sizes"] and "may/shotA" in before["_sizes"]

    hk.cmd_touch(root, "may/shotA", "upload")

    with open(index_path) as f:
        after = json.load(f)
    assert "projects" not in after.get("_sizes", {})
    assert "may/shotA" not in after.get("_sizes", {})


def test_touch_cook_does_not_invalidate_size_cache(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    hk.cmd_touch(root, "may/shotA", "cook")
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        after = json.load(f)
    assert "projects" in after["_sizes"] and "may/shotA" in after["_sizes"]


def test_touch_download_does_not_invalidate_size_cache(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    hk.cmd_touch(root, "may/shotA", "download")
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        after = json.load(f)
    assert "projects" in after["_sizes"] and "may/shotA" in after["_sizes"]


def test_touch_upload_invalidation_forces_ls_to_remeasure(tmp_path, monkeypatch):
    root = mk(tmp_path)
    hk.cmd_ls(root)
    hk.cmd_touch(root, "may/shotA", "upload")
    calls = _count_size_calls(monkeypatch)
    hk.cmd_ls(root)  # within max_age_s, but the cache entries were invalidated
    assert any(p.endswith(os.path.join("projects")) for p in calls["paths"])
    assert any(p.endswith(os.path.join("projects", "may", "shotA")) for p in calls["paths"])


# -- fix round 2, "C": _SizeCache.flush() merges, doesn't clobber -------------


def test_size_cache_concurrent_flush_merges_not_clobbers(tmp_path):
    root = str(tmp_path)
    (tmp_path / ".rpfarm").mkdir()

    start1 = threading.Event()
    start2 = threading.Event()
    proceed = threading.Event()
    errors = []

    def worker(key, value, ready_event):
        try:
            index = hk._load_index(root)
            cache = hk._SizeCache(root, index, "_sizes", False, hk._DEFAULT_MAX_AGE_S, hk._DEFAULT_BUDGET_S)
            # Simulate "this sweep measured one key" without a real du call.
            cache.updated[key] = {"bytes": value, "measured_at": 1.0}
            ready_event.set()
            proceed.wait(timeout=5)
            cache.flush()
        except Exception as e:  # noqa: BLE001 - surfaced via `errors`, not swallowed
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("zoneA", 111, start1))
    t2 = threading.Thread(target=worker, args=("zoneB", 222, start2))
    t1.start()
    t2.start()
    assert start1.wait(timeout=5) and start2.wait(timeout=5)
    proceed.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors
    index = hk._load_index(root)
    assert index["_sizes"]["zoneA"]["bytes"] == 111
    assert index["_sizes"]["zoneB"]["bytes"] == 222


def test_size_cache_flush_does_not_resurrect_deleted_key(tmp_path):
    """A flush must merge into the *current* on-disk state, not the
    read-time snapshot -- if another writer removed a key in between
    (e.g. touch's invalidation), a stale-snapshot flush must not bring it
    back for an unrelated key it never even measured."""
    root = str(tmp_path)
    (tmp_path / ".rpfarm").mkdir()
    index = hk._load_index(root)
    cache = hk._SizeCache(root, index, "_sizes", False, hk._DEFAULT_MAX_AGE_S, hk._DEFAULT_BUDGET_S)
    cache.cache["stale_key"] = {"bytes": 999, "measured_at": 1.0}  # in the read-time view only
    cache.updated["fresh_key"] = {"bytes": 1, "measured_at": 2.0}  # actually measured this sweep
    cache.flush()
    on_disk = hk._load_index(root)["_sizes"]
    assert "fresh_key" in on_disk
    assert "stale_key" not in on_disk


# -- fix round 3, "2": invalidate <zone>, and the install preset using it --


def test_invalidate_removes_zone_from_size_cache(tmp_path):
    root = mk(tmp_path)
    hk.cmd_ls(root)  # populate _sizes for every zone
    index_path = os.path.join(root, ".rpfarm", "index.json")
    with open(index_path) as f:
        before = json.load(f)
    assert "houdini" in before["_sizes"]

    result = hk.cmd_invalidate(root, "houdini")
    assert result == {"ok": True, "zone": "houdini"}

    with open(index_path) as f:
        after = json.load(f)
    assert "houdini" not in after.get("_sizes", {})
    # Untouched zones survive -- invalidate is scoped to the one zone.
    assert "projects" in after.get("_sizes", {})


def test_invalidate_rejects_unknown_zone(tmp_path):
    root = mk(tmp_path)
    with pytest.raises(hk.HousekeepingError):
        hk.cmd_invalidate(root, "not-a-real-zone")


def test_invalidate_houdini_forces_next_disk_usage_to_remeasure_it(tmp_path, monkeypatch):
    """Simulates what the install preset's post-command now does: a
    version lands under houdini/ (bytes change), then `invalidate houdini`
    runs. The very next disk-usage (no --refresh, cache otherwise still
    fresh from an earlier ls) must remeasure houdini specifically, not
    silently keep serving the pre-install figure."""
    root = mk(tmp_path)
    hk.cmd_ls(root)  # cache is fresh for every zone, including houdini

    # Simulate the install: a new version directory lands under houdini/.
    (tmp_path / "houdini/22.0.500/bin").mkdir(parents=True)
    (tmp_path / "houdini/22.0.500/bin/hython").write_bytes(b"x" * 999)
    hk.cmd_invalidate(root, "houdini")

    calls = _count_size_calls(monkeypatch)
    out = hk.cmd_disk_usage(root, volume_size_gb=50)
    assert any(p.endswith("houdini") for p in calls["paths"])
    assert out["volume"]["used"] >= 999  # the newly-installed bytes are counted


def test_main_invalidate(tmp_path, capsys, monkeypatch):
    root = mk(tmp_path)
    monkeypatch.setattr(hk, "DEFAULT_ROOT", root)
    hk.cmd_ls(root)
    rc = hk.main(["housekeeping.py", "invalidate", "houdini"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "zone": "houdini"}


def test_main_invalidate_rejects_bad_zone(capsys):
    rc = hk.main(["housekeeping.py", "invalidate", "not-a-zone"])
    assert rc == 2  # argparse's own `choices=` rejects it before cmd_invalidate runs


# Reviewer-noted, intentionally NOT fixed (documented in the report):
# `houdini rm <version> --dry-run` still writes the `_houdini` cache via
# its own `_SizeCache.flush()` -- the cache write is exactly the same
# size-measurement bookkeeping a real `houdini ls` would do; --dry-run
# only skips the deletion itself, not the (harmless, correct) caching.
