"""Tests for rpfarm.dispatch -- the pdg-free half of the scheduler."""

import json

import pytest

from rpfarm.dispatch import (
    Dispatcher,
    PodState,
    TaskState,
    append_record,
    autoscale_decision,
    budget_state,
)


def T(i):
    return TaskState(
        task_id=f"t{i}",
        work_item_id=i,
        command="x",
        env={},
        pod_id=None,
        attempts=0,
        started=None,
        log_path="",
    )


# -- the brief's four tests, verbatim ---------------------------------------


def test_least_loaded_and_slots():
    d = Dispatcher(slots_per_pod=2)
    d.add_pod("a", 0.5)
    d.add_pod("b", 0.5)
    for i in range(3):
        d.enqueue(T(i))
    picks = []
    while (p := d.pick_pod()) and d.pending:
        t = d.pending[0]
        d.assign(t.task_id, p.pod_id)
        picks.append(p.pod_id)
    assert sorted(picks) == ["a", "a", "b"] or sorted(picks) == ["a", "b", "b"]
    d.add_pod("c", 0.5)
    d.enqueue(T(9))
    assert d.pick_pod().pod_id == "c"


def test_pod_dead_requeues_until_max():
    d = Dispatcher(slots_per_pod=1, max_attempts=2)
    d.add_pod("a", 1.0)
    d.enqueue(T(1))
    d.assign("t1", "a")
    retry = d.pod_dead("a")
    assert [t.task_id for t in retry] == ["t1"]
    assert retry[0].attempts == 1
    assert d.pending[0].task_id == "t1"
    d.add_pod("b", 1.0)
    d.assign("t1", "b")
    retry = d.pod_dead("b")
    assert retry == []
    assert [t.task_id for t in d.failed] == ["t1"]


def test_autoscale_decision():
    assert autoscale_decision(pending=100, active_pods=1, max_pods=4, render_times=[60.0]) == 3
    assert (
        autoscale_decision(
            pending=100, active_pods=1, max_pods=4, render_times=[60.0], over_budget=True
        )
        == 0
    )
    assert autoscale_decision(pending=5, active_pods=0, max_pods=4, render_times=[]) == 1
    assert autoscale_decision(pending=0, active_pods=2, max_pods=4, render_times=[60.0]) == 0


def test_budget_state():
    assert budget_state(0, 0) == "ok"
    assert budget_state(79, 100) == "ok"
    assert budget_state(85, 100) == "warn"
    assert budget_state(100, 100) == "stop"
    assert budget_state(121, 100) == "kill"


# -- pods --------------------------------------------------------------------


def test_a_created_pod_is_not_pickable():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 0.0, status="CREATED")
    assert d.pods["a"].status == "CREATED"
    d.enqueue(T(1))
    # a CREATED pod has no worker answering yet -- only RUNNING pods are picked
    assert d.pick_pod() is None
    d.pods["a"].status = "RUNNING"
    assert d.pick_pod().pod_id == "a"


def test_add_pod_is_idempotent():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 0.5)
    d.add_pod("a", 0.9, status="CREATED")
    assert len(d.pods) == 1
    assert d.pods["a"].status == "RUNNING"
    assert d.pods["a"].cost_per_hr == 0.5


def test_remove_pod_returns_tasks_like_pod_dead():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 1.0)
    d.enqueue(T(1))
    d.assign("t1", "a")
    d.remove_pod("a")
    assert "a" not in d.pods
    assert [t.task_id for t in d.pending] == ["t1"]


def test_pod_dead_drops_the_pod_from_the_pool():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 1.0)
    d.pod_dead("a")
    assert "a" not in d.pods
    assert d.pod_dead("a") == []


def test_pick_pod_skips_dead_and_full_pods():
    d = Dispatcher(slots_per_pod=1)
    for name in ("a", "b"):
        d.add_pod(name, 1.0)
    d.pods["a"].status = "DEAD"
    d.enqueue(T(1))
    assert d.pick_pod().pod_id == "b"
    d.assign("t1", "b")
    d.enqueue(T(2))
    assert d.pick_pod() is None


def test_mark_full_holds_a_pod_until_released():
    d = Dispatcher(slots_per_pod=2)
    d.add_pod("a", 1.0)
    d.mark_full("a")
    assert d.pick_pod() is None
    d.release_full()
    assert d.pick_pod().pod_id == "a"


# -- tasks -------------------------------------------------------------------


def test_running_tasks_and_complete():
    d = Dispatcher(slots_per_pod=2)
    d.add_pod("a", 1.0)
    d.enqueue(T(1))
    d.enqueue(T(2))
    d.assign("t1", "a")
    d.assign("t2", "a")
    assert sorted(t.task_id for t in d.running_tasks()) == ["t1", "t2"]
    d.complete("t1", True)
    assert [t.task_id for t in d.running_tasks()] == ["t2"]
    assert d.pods["a"].running == {"t2"}
    d.complete("t2", False)
    assert d.running_tasks() == []
    assert [t.task_id for t in d.failed] == ["t2"]


def test_complete_is_idempotent_for_unknown_task():
    d = Dispatcher(slots_per_pod=1)
    assert d.complete("nope", True) is None


def test_failed_since_last_call_drains():
    d = Dispatcher(slots_per_pod=1, max_attempts=1)
    d.add_pod("a", 1.0)
    d.enqueue(T(1))
    d.assign("t1", "a")
    d.pod_dead("a")
    assert [t.task_id for t in d.failed_since_last_call()] == ["t1"]
    assert d.failed_since_last_call() == []
    # .failed keeps the full history
    assert [t.task_id for t in d.failed] == ["t1"]


def test_reassign_mints_a_new_task_id():
    d = Dispatcher(slots_per_pod=1)
    d.enqueue(T(1))
    new_id = d.retask("t1", "t1-retry")
    assert new_id == "t1-retry"
    assert [t.task_id for t in d.pending] == ["t1-retry"]
    assert d.pending[0].work_item_id == 1


def test_task_for_work_item():
    d = Dispatcher(slots_per_pod=1)
    d.enqueue(T(7))
    assert d.task_for_work_item(7).task_id == "t7"
    assert d.task_for_work_item(99) is None


# -- idle --------------------------------------------------------------------


def test_idle_pods_only_after_the_timeout():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 1.0)
    d.enqueue(T(1))
    d.assign("t1", "a")
    d.touch_idle(now=1000.0)
    assert d.pods["a"].idle_since is None  # busy
    d.complete("t1", True)
    d.touch_idle(now=1000.0)
    assert d.pods["a"].idle_since == 1000.0
    assert d.idle_pods(now=1100.0, idle_seconds=120) == []
    assert [p.pod_id for p in d.idle_pods(now=1200.0, idle_seconds=120)] == ["a"]
    # picking work back up clears the idle clock
    d.enqueue(T(2))
    d.assign("t2", "a")
    d.touch_idle(now=1250.0)
    assert d.pods["a"].idle_since is None


def test_idle_pods_ignores_created_pods():
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 1.0, status="CREATED")
    d.touch_idle(now=0.0)
    assert d.idle_pods(now=10_000.0, idle_seconds=1) == []


# -- autoscale / budget edges ------------------------------------------------


def test_autoscale_respects_max_pods_headroom():
    # ratio would be 3, but only 1 slot left under max_pods
    assert autoscale_decision(pending=100, active_pods=3, max_pods=4, render_times=[60.0]) == 1
    assert autoscale_decision(pending=100, active_pods=4, max_pods=4, render_times=[60.0]) == 0


def test_autoscale_batch_is_clamped_to_two_and_four():
    # 31 min remaining -> ratio 1.03 -> clamped up to the batch minimum of 2
    assert autoscale_decision(pending=31, active_pods=1, max_pods=8, render_times=[60.0]) == 2
    # a huge backlog is still capped at the batch maximum of 4
    assert autoscale_decision(pending=10_000, active_pods=1, max_pods=8, render_times=[60.0]) == 4


def test_autoscale_below_threshold_adds_nothing():
    assert autoscale_decision(pending=10, active_pods=1, max_pods=4, render_times=[60.0]) == 0


def test_autoscale_with_no_history_only_covers_the_cold_start():
    assert autoscale_decision(pending=50, active_pods=1, max_pods=4, render_times=[]) == 0
    assert autoscale_decision(pending=1, active_pods=0, max_pods=4, render_times=[]) == 1
    assert autoscale_decision(pending=1, active_pods=0, max_pods=0, render_times=[]) == 0


def test_budget_state_without_a_limit_is_always_ok():
    assert budget_state(1e9, 0) == "ok"
    assert budget_state(1e9, float("inf")) == "ok"


def test_budget_state_boundaries():
    assert budget_state(80, 100) == "warn"
    assert budget_state(99.99, 100) == "warn"
    assert budget_state(119, 100) == "stop"
    assert budget_state(120, 100) == "kill"


# -- ledger stand-in ---------------------------------------------------------


def test_append_record_writes_one_json_line_per_call(tmp_path):
    path = tmp_path / "ledger" / "abc.jsonl"
    append_record(str(path), cook_id="abc", work_item=1, exit_code=0)
    append_record(str(path), cook_id="abc", work_item=2, exit_code=1)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"cook_id": "abc", "work_item": 1, "exit_code": 0}
    assert json.loads(lines[1])["work_item"] == 2


def test_append_record_never_raises_on_a_bad_path(tmp_path):
    # the ledger is bookkeeping, not the cook -- a failure to write must not
    # take a work item down with it
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    append_record(str(blocker / "sub" / "x.jsonl"), cook_id="abc")


# -- PodState ---------------------------------------------------------------


def test_pod_state_free_slots():
    p = PodState(pod_id="a", cost_per_hr=1.0, slots=2)
    assert p.free_slots() == 2
    p.running.add("t1")
    assert p.free_slots() == 1
    p.running.add("t2")
    assert p.free_slots() == 0


@pytest.mark.parametrize("status", ["CREATED", "DEAD"])
def test_pod_state_non_running_has_no_free_slots(status):
    p = PodState(pod_id="a", cost_per_hr=1.0, slots=4, status=status)
    assert p.free_slots() == 0
