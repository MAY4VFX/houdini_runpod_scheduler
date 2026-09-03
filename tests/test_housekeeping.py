import json
import os
import sys
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


def test_ls_volume_totals_present(tmp_path):
    root = mk(tmp_path)
    volume = hk.cmd_ls(root)["volume"]
    assert volume["total"] > 0
    assert volume["used"] >= 0


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
