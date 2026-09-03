"""Tests for rpfarm.ledger -- journal, billing merge, and cost summaries.

Record shapes mirror what hda/runpodfarm_scheduler.hda/.../PythonModule
actually writes (``_poll_tasks`` for a task record, ``onStopCook`` for a
cook_summary record) -- see rpfarm/ledger.py's module docstring.
"""

import json
import logging
import os

import pytest

from rpfarm import ledger
from rpfarm.sync import SftpTarget, SyncError


# -- append / load_all -------------------------------------------------------


def test_append_and_load(tmp_path):
    p = tmp_path / "c1.jsonl"
    ledger.append(
        p, cook_id="c1", user="may", project="shot", work_item=1, pod="p1",
        gpu="RTX 4090", started=0, ended=60, duration_s=60, exit_code=0, cost_est=0.0123,
    )
    ledger.append(
        p, cook_id="c1", user="may", project="shot", work_item=2, pod="p1",
        gpu="RTX 4090", started=60, ended=180, duration_s=120, exit_code=0, cost_est=0.0246,
    )
    recs = ledger.load_all(tmp_path)
    assert len(recs) == 2 and recs[1]["duration_s"] == 120


def test_append_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "dir" / "c2.jsonl"
    ledger.append(p, cook_id="c2", pod="p1", duration_s=1)
    assert p.exists()


def test_append_swallows_oserror_and_logs(tmp_path, caplog):
    # A path that IS a directory can never be open()'d for writing -- a
    # reliable way to trigger a real OSError without permission-bit
    # gymnastics. Matches rpfarm.dispatch.append_record's own behavior
    # (the stand-in this module replaces): a write failure is bookkeeping
    # gone wrong, never a reason to raise into onStopCook/_poll_tasks.
    bad_path = tmp_path / "not_a_file"
    bad_path.mkdir()
    with caplog.at_level(logging.WARNING):
        ledger.append(bad_path, cook_id="x")  # must not raise
    assert bad_path.is_dir()  # nothing wrote to it; still just a directory
    assert any("ledger append" in r.message for r in caplog.records)


def test_append_cook_summary_swallows_oserror(tmp_path):
    bad_path = tmp_path / "not_a_file"
    bad_path.mkdir()
    ledger.append_cook_summary(bad_path, cook_id="x", user="u", project="p")  # must not raise


def test_load_all_multiple_files_and_missing_dir(tmp_path):
    ledger.append(tmp_path / "a.jsonl", cook_id="a", pod="p1", duration_s=1)
    ledger.append(tmp_path / "b.jsonl", cook_id="b", pod="p1", duration_s=1)
    recs = ledger.load_all(tmp_path)
    assert {r["cook_id"] for r in recs} == {"a", "b"}
    assert ledger.load_all(tmp_path / "does-not-exist") == []


def test_load_all_skips_blank_lines(tmp_path):
    p = tmp_path / "c3.jsonl"
    p.write_text('{"cook_id": "c3", "pod": "p1"}\n\n')
    recs = ledger.load_all(tmp_path)
    assert len(recs) == 1


def test_append_cook_summary_matches_scheduler_shape(tmp_path):
    p = tmp_path / "c4.jsonl"
    ledger.append_cook_summary(
        p, cook_id="c4", user="may", project="shot",
        started=0, ended=100, canceled=False, items_failed=0, cost_est=0.5,
    )
    recs = ledger.load_all(tmp_path)
    assert recs[0]["record"] == "cook_summary"
    assert recs[0]["cost_est"] == 0.5


def test_append_cook_summary_derives_cost_from_pods(tmp_path):
    p = tmp_path / "c5.jsonl"
    ledger.append_cook_summary(
        p, cook_id="c5", user="may", project="shot",
        pods=[{"pod_id": "p1", "gpu": "RTX 4090", "cost_per_hr": 0.5, "created": 0, "terminated": 3600}],
    )
    recs = ledger.load_all(tmp_path)
    assert abs(recs[0]["cost_est"] - 0.5) < 1e-9


# -- merge_billing ------------------------------------------------------------


def test_merge_billing_prorates():
    # GET /billing/pods returns one row PER POD PER DAY (verified live,
    # 2026-09-03: {podId, amount, timeBilledMs, diskSpaceBilledGB, time} --
    # no podName at all). Two daily rows for the same podId aggregate to
    # amount=0.90, timeBilledMs=360000 (360s), matching the brief's single-row
    # example numbers.
    recs = [
        {"cook_id": "abcd1234", "user": "may", "project": "shot", "pod": "p1", "duration_s": 60, "cost_est": 0.01},
        {"cook_id": "abcd1234", "user": "may", "project": "shot", "pod": "p1", "duration_s": 120, "cost_est": 0.02},
    ]
    billing = [
        {"podId": "p1", "amount": 0.50, "timeBilledMs": 200000, "diskSpaceBilledGB": 10, "time": "2026-09-01 00:00:00"},
        {"podId": "p1", "amount": 0.40, "timeBilledMs": 160000, "diskSpaceBilledGB": 10, "time": "2026-09-02 00:00:00"},
    ]
    out = ledger.merge_billing(recs, billing)
    task = [r for r in out if r.get("kind") == "task"]
    idle = [r for r in out if r.get("kind") == "idle"]
    assert abs(task[0]["cost"] - 0.15) < 1e-6
    assert abs(task[1]["cost"] - 0.30) < 1e-6
    assert abs(idle[0]["cost"] - 0.45) < 1e-6


def test_merge_billing_unattributed_pod_with_no_local_records():
    # No podName in the real API -- a billed pod with no matching local
    # record can't be attributed to a user/project, only surfaced honestly.
    billing = [{"podId": "p9", "amount": 1.2, "timeBilledMs": 600000, "time": "2026-09-01 00:00:00"}]
    out = ledger.merge_billing([], billing)
    assert len(out) == 1
    assert out[0]["kind"] == "unattributed"
    assert out[0]["user"] == "(unattributed)" and out[0]["project"] == "(unattributed)"
    assert abs(out[0]["cost"] - 1.2) < 1e-9
    assert abs(out[0]["duration_s"] - 600.0) < 1e-9


def test_merge_billing_no_matching_billing_passes_through_untouched():
    recs = [{"cook_id": "c", "user": "u", "project": "p", "pod": "p1", "duration_s": 60, "cost_est": 0.01}]
    out = ledger.merge_billing(recs, [])
    assert len(out) == 1 and out[0]["cost_est"] == 0.01 and "cost" not in out[0]


def test_merge_billing_passes_through_cook_summary():
    recs = [{"record": "cook_summary", "cook_id": "c", "cost_est": 1.0}]
    out = ledger.merge_billing(recs, [])
    assert out == recs


def test_merge_billing_passes_through_records_without_pod_field():
    # Not expected from the scheduler (every task record carries "pod"),
    # but merge_billing must not silently drop a record just because it
    # doesn't -- same treatment as "no matching billing entry".
    recs = [{"cook_id": "c", "user": "u", "project": "p", "duration_s": 10, "cost_est": 0.001}]
    out = ledger.merge_billing(recs, [])
    assert out == recs


def test_merge_billing_caps_task_costs_when_duration_exceeds_billed_seconds():
    # A task straddling a billing-period boundary: local duration_s sums
    # to more (600s) than this period's billing rows show as billed
    # (400s) -- e.g. only part of a still-running pod's time fell inside
    # the queried window.
    recs = [
        {"cook_id": "c", "user": "u", "project": "p", "pod": "p1", "duration_s": 300},
        {"cook_id": "c", "user": "u", "project": "p", "pod": "p1", "duration_s": 300},
    ]
    billing = [{"podId": "p1", "amount": 1.0, "timeBilledMs": 400000}]
    out = ledger.merge_billing(recs, billing)
    tasks = [r for r in out if r.get("kind") == "task"]
    idle = [r for r in out if r.get("kind") == "idle"]
    assert abs(sum(t["cost"] for t in tasks) - 1.0) < 1e-9  # never exceeds the actual bill
    assert abs(tasks[0]["cost"] - 0.5) < 1e-9 and abs(tasks[1]["cost"] - 0.5) < 1e-9  # split by duration share
    assert len(idle) == 1 and idle[0]["cost"] == 0.0 and idle[0]["duration_s"] == 0.0


# -- summarize ------------------------------------------------------------


def test_summarize_by_project():
    out = ledger.summarize(
        [
            {"project": "a", "user": "u", "cost": 1.0, "duration_s": 3600, "kind": "task"},
            {"project": "a", "user": "u", "cost": 0.5, "duration_s": 0, "kind": "idle"},
        ],
        by="project",
    )
    assert out[0]["key"] == "a"
    assert out[0]["cost"] == 1.5
    assert out[0]["tasks"] == 1
    assert abs(out[0]["gpu_hours"] - 1.0) < 1e-9
    assert abs(out[0]["cost_per_task"] - 1.5) < 1e-9


def test_summarize_by_user_multiple_groups_sorted_by_cost_desc():
    out = ledger.summarize(
        [
            {"user": "a", "project": "p", "cost": 1.0, "duration_s": 60, "kind": "task"},
            {"user": "b", "project": "p", "cost": 5.0, "duration_s": 60, "kind": "task"},
        ],
        by="user",
    )
    assert [g["key"] for g in out] == ["b", "a"]


def test_summarize_excludes_cook_summary_rows():
    out = ledger.summarize(
        [
            {"record": "cook_summary", "project": "a", "cost_est": 99.0},
            {"project": "a", "user": "u", "cost": 1.0, "duration_s": 60, "kind": "task"},
        ],
        by="project",
    )
    assert out[0]["cost"] == 1.0


def test_summarize_filters_by_since_until():
    recs = [
        {"project": "a", "cost": 1.0, "duration_s": 60, "kind": "task", "started": 100},
        {"project": "a", "cost": 2.0, "duration_s": 60, "kind": "task", "started": 500},
    ]
    out = ledger.summarize(recs, by="project", since=200, until=600)
    assert out[0]["cost"] == 2.0 and out[0]["tasks"] == 1


def test_summarize_no_tasks_zero_cost_per_task():
    out = ledger.summarize([{"project": "a", "cost": 0.5, "duration_s": 60, "kind": "idle"}], by="project")
    assert out[0]["tasks"] == 0 and out[0]["cost_per_task"] == 0.0


# -- to_csv -------------------------------------------------------------------


def test_to_csv_roundtrip(tmp_path):
    recs = [
        {"cook_id": "c1", "cost": 1.5, "project": "a"},
        {"cook_id": "c2", "cost": 2.5, "kind": "idle"},
    ]
    out = tmp_path / "out.csv"
    ledger.to_csv(recs, out)
    text = out.read_text()
    lines = text.strip().splitlines()
    assert lines[0].split(",") == ["cook_id", "cost", "project", "kind"]
    assert len(lines) == 3


def test_to_csv_creates_parent_dirs(tmp_path):
    out = tmp_path / "nested" / "out.csv"
    ledger.to_csv([{"a": 1}], out)
    assert out.exists()


# -- sync_from_volume ---------------------------------------------------------


class FakeClient:
    """Stands in for rpfarm.worker_client.WorkerClient -- exec()/read_file()
    only, matching the real methods sync_from_volume calls."""

    def __init__(self, files):
        # files: {remote_path: content}
        self.files = files

    def exec(self, command, timeout_s=600):
        assert "find" in command and "/workspace/ledger" in command
        return {"exit_code": 0, "stdout": "\n".join(sorted(self.files)), "stderr": ""}

    def read_file(self, path):
        return self.files.get(path)


def test_sync_from_volume_pulls_missing_files(tmp_path):
    client = FakeClient({
        "/workspace/ledger/may/aaaa1111.jsonl": '{"cook_id": "aaaa1111"}\n',
        "/workspace/ledger/bob/bbbb2222.jsonl": '{"cook_id": "bbbb2222"}\n',
    })
    local = tmp_path / "ledger"
    n = ledger.sync_from_volume(client, local, "may")
    assert n == 2
    assert (local / "may" / "aaaa1111.jsonl").read_text() == '{"cook_id": "aaaa1111"}\n'
    assert (local / "bob" / "bbbb2222.jsonl").read_text() == '{"cook_id": "bbbb2222"}\n'


def test_sync_from_volume_pulls_other_users_records_too(tmp_path):
    # Ruling R25: cross-user accounting is the whole point of this task --
    # sync_from_volume must not scope its own `find` or its pull to just
    # the calling user's own subtree.
    client = FakeClient({
        "/workspace/ledger/alice/1111aaaa.jsonl": '{"cook_id": "1111aaaa", "user": "alice"}\n',
        "/workspace/ledger/bob/2222bbbb.jsonl": '{"cook_id": "2222bbbb", "user": "bob"}\n',
        "/workspace/ledger/carol/3333cccc.jsonl": '{"cook_id": "3333cccc", "user": "carol"}\n',
    })
    local = tmp_path / "ledger"
    n = ledger.sync_from_volume(client, local, "may")  # caller "may" owns none of these
    assert n == 3
    pulled_users = {p.name for p in local.iterdir()}
    assert pulled_users == {"alice", "bob", "carol"}


def test_sync_from_volume_skips_files_already_local(tmp_path):
    local = tmp_path / "ledger"
    (local / "may").mkdir(parents=True)
    (local / "may" / "aaaa1111.jsonl").write_text("existing\n")
    client = FakeClient({"/workspace/ledger/may/aaaa1111.jsonl": "new content\n"})
    n = ledger.sync_from_volume(client, local, "may")
    assert n == 0
    assert (local / "may" / "aaaa1111.jsonl").read_text() == "existing\n"


def test_sync_from_volume_flat_remote_path_bucketed_under_user(tmp_path):
    # Legacy / not-yet-nested remote layout (Task 8 Step 14 not landed yet):
    # a flat *.jsonl directly under /workspace/ledger falls back to the
    # calling user's own bucket rather than being dropped.
    client = FakeClient({"/workspace/ledger/cccc3333.jsonl": '{"cook_id": "cccc3333"}\n'})
    local = tmp_path / "ledger"
    n = ledger.sync_from_volume(client, local, "may")
    assert n == 1
    assert (local / "may" / "cccc3333.jsonl").exists()


def test_sync_from_volume_returns_zero_on_find_failure(tmp_path):
    class FailingClient:
        def exec(self, command, timeout_s=600):
            return {"exit_code": 1, "stdout": "", "stderr": "no such dir"}

    n = ledger.sync_from_volume(FailingClient(), tmp_path / "ledger", "may")
    assert n == 0


# -- mirror_to_volume (Ruling R25) --------------------------------------------

_FAKE_RCLONE_OK = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"stats": {"bytes": 5, "totalBytes": 5, "speed": 1.0, "transfers": 1}}), file=sys.stderr, flush=True)
sys.exit(0)
"""

_FAKE_RCLONE_FAIL = """#!/usr/bin/env python3
import sys
sys.exit(7)
"""


def _write_fake_rclone(tmp_path, body):
    p = tmp_path / "fake_rclone.py"
    p.write_text(body)
    os.chmod(p, 0o755)
    return str(p)


def test_mirror_to_volume_pushes_single_file_package(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    p = ledger_dir / "aaaa1111.jsonl"
    p.write_text('{"cook_id": "aaaa1111"}\n')

    rclone_bin = _write_fake_rclone(tmp_path, _FAKE_RCLONE_OK)
    target = SftpTarget(host="h", port=22, key_path="/k")
    # Would raise SyncError (ruling R8's invariant check inside
    # build_rclone_args) if local_root/remote_root/remote didn't line up --
    # succeeding here IS the proof they do.
    stats = ledger.mirror_to_volume(p, "may", target, rclone_bin)
    assert stats.files == 1


def test_mirror_to_volume_raises_on_rclone_failure(tmp_path):
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    p = ledger_dir / "aaaa1111.jsonl"
    p.write_text('{"cook_id": "aaaa1111"}\n')

    rclone_bin = _write_fake_rclone(tmp_path, _FAKE_RCLONE_FAIL)
    target = SftpTarget(host="h", port=22, key_path="/k")
    with pytest.raises(SyncError):
        ledger.mirror_to_volume(p, "may", target, rclone_bin)
