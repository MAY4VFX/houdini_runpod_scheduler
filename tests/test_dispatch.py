"""Tests for rpfarm.dispatch -- the pdg-free half of the scheduler."""

import json

import rpfarm.dispatch as rpdispatch

import pytest

from rpfarm.dispatch import (
    OUTAGE_GRACE_SECONDS,
    POD_DEAD_AFTER_SECONDS,
    TERMINATE_RETRY_SECONDS,
    Dispatcher,
    OutageTracker,
    PodState,
    TaskState,
    TerminateRetries,
    append_record,
    autoscale_decision,
    budget_state,
    pod_timed_out,
    should_defer,
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


# -- fix round 1: backpressure, boot budget, terminal path -------------------


def test_should_defer_accepts_the_first_item_with_no_pods_yet():
    """onSetupCook creates the first pod, but onSchedule can still arrive
    before it registers. Deferring everything then would deadlock the cook."""
    assert should_defer(pending=0, pods=0) is False


def test_should_defer_at_capacity():
    # One pod, depth 3: three queued is full, two is not.
    assert should_defer(pending=2, pods=1) is False
    assert should_defer(pending=3, pods=1) is True
    # More pods, more room.
    assert should_defer(pending=3, pods=2) is False
    assert should_defer(pending=6, pods=2) is True


def test_should_defer_honours_a_custom_depth():
    assert should_defer(pending=1, pods=1, depth_per_pod=1) is True
    assert should_defer(pending=1, pods=1, depth_per_pod=2) is False


def test_deferring_before_enqueue_keeps_the_queue_honest():
    """The bug this replaces: onSchedule enqueued the task and *then* returned
    a busy result, so PDG's re-offer of the same work item enqueued it twice
    and it would have run twice."""
    d = Dispatcher(slots_per_pod=1)
    d.add_pod("a", 1.0)
    for i in range(3):
        d.enqueue(T(i))
    assert should_defer(len(d.pending), len(d.pods)) is True
    # The caller returns Deferred here without enqueueing; PDG re-offers the
    # same item later, and the queue still holds exactly one copy of each.
    assert [t.task_id for t in d.pending] == ["t0", "t1", "t2"]


def test_pod_timed_out_gives_a_booting_pod_its_own_budget():
    """A CREATED pod is not yet expected to answer /health. Judging it by the
    RUNNING heartbeat killed pods mid-boot: the live smoke saw pods answer at
    25-37s and the spec measures 43.5s, against a 60s heartbeat clock that
    started at creation."""
    pod = PodState(pod_id="a", status="CREATED", created_at=1000.0)
    pod.health_fail_since = 1005.0  # stamped by the first poll after creation
    assert pod_timed_out(pod, now=1100.0, boot_seconds=300, dead_seconds=60) is None
    assert pod_timed_out(pod, now=1301.0, boot_seconds=300, dead_seconds=60) == "boot"


def test_pod_timed_out_still_kills_a_silent_running_pod():
    pod = PodState(pod_id="a", status="RUNNING", created_at=1000.0)
    assert pod_timed_out(pod, now=1100.0, boot_seconds=300, dead_seconds=60) is None
    pod.health_fail_since = 1100.0
    assert pod_timed_out(pod, now=1150.0, boot_seconds=300, dead_seconds=60) is None
    assert pod_timed_out(pod, now=1161.0, boot_seconds=300, dead_seconds=60) == "dead"


def test_pod_timed_out_without_a_creation_time():
    pod = PodState(pod_id="a", status="CREATED", created_at=None)
    assert pod_timed_out(pod, now=9999.0, boot_seconds=300, dead_seconds=60) is None


def test_fail_pending_moves_a_queued_task_to_failed():
    """The duplicate-task_id path used to reach into .pending and report the
    item itself, skipping .failed -- so the give-up was invisible to
    failed_since_last_call() and the item was reported by a different code
    path than every other failure."""
    d = Dispatcher(slots_per_pod=1)
    d.enqueue(T(1))
    d.enqueue(T(2))
    task = d.fail_pending("t1")
    assert task is not None and task.work_item_id == 1
    assert [t.task_id for t in d.pending] == ["t2"]
    assert [t.task_id for t in d.failed] == ["t1"]
    assert [t.task_id for t in d.failed_since_last_call()] == ["t1"]


def test_fail_pending_ignores_an_unknown_task():
    d = Dispatcher(slots_per_pod=1)
    d.enqueue(T(1))
    assert d.fail_pending("nope") is None
    assert len(d.pending) == 1 and d.failed == []


# -- outage vs. dead pods (final-review finding 6) ---------------------------


def test_outage_tracker_needs_every_pod_to_fail():
    t = OutageTracker()
    assert t.sweep(now=10.0, polled=3, healthy=1) is False
    assert t.dead_seconds() == POD_DEAD_AFTER_SECONDS
    assert t.sweep(now=15.0, polled=3, healthy=0) is True
    assert t.dead_seconds() == POD_DEAD_AFTER_SECONDS + OUTAGE_GRACE_SECONDS


def test_outage_tracker_clears_as_soon_as_one_pod_answers():
    t = OutageTracker()
    t.sweep(now=10.0, polled=2, healthy=0)
    assert t.since == 10.0
    t.sweep(now=20.0, polled=2, healthy=1)
    assert t.since is None
    assert t.dead_seconds() == POD_DEAD_AFTER_SECONDS


def test_outage_tracker_keeps_the_streak_start_across_sweeps():
    """The grace is measured from when the outage started, not from the
    latest sweep, so it cannot be extended indefinitely one sweep at a
    time -- the caller's per-pod clock is what eventually expires."""
    t = OutageTracker()
    t.sweep(now=10.0, polled=2, healthy=0)
    t.sweep(now=15.0, polled=2, healthy=0)
    t.sweep(now=20.0, polled=2, healthy=0)
    assert t.since == 10.0


def test_a_sweep_that_polled_nobody_is_no_evidence_either_way():
    t = OutageTracker()
    assert t.sweep(now=10.0, polled=0, healthy=0) is False
    assert t.since is None
    t.sweep(now=11.0, polled=1, healthy=0)
    assert t.sweep(now=12.0, polled=0, healthy=0) is True, "an ongoing outage stays ongoing"
    assert t.since == 11.0


def test_pod_timed_out_honours_the_widened_deadline():
    pod = PodState(pod_id="a", status="RUNNING", health_fail_since=0.0)
    widened = POD_DEAD_AFTER_SECONDS + OUTAGE_GRACE_SECONDS
    assert pod_timed_out(pod, now=POD_DEAD_AFTER_SECONDS + 1, dead_seconds=widened) is None
    assert pod_timed_out(pod, now=widened + 1, dead_seconds=widened) == "dead"


# -- terminate retries -------------------------------------------------------


def test_terminate_retries_are_due_only_after_the_backoff():
    r = TerminateRetries()
    r.add("a", now=0.0)
    assert r.pending() == ["a"] and len(r) == 1
    assert r.due(now=TERMINATE_RETRY_SECONDS - 1) == []
    assert r.due(now=TERMINATE_RETRY_SECONDS) == ["a"]


def test_terminate_retries_clear_on_success():
    r = TerminateRetries()
    r.add("a", now=0.0)
    r.clear("a")
    assert r.pending() == [] and not r
    r.clear("a")  # clearing something unknown is not an error


def test_terminate_retries_rearm_when_a_retry_fails_again():
    r = TerminateRetries()
    r.add("a", now=0.0)
    r.add("a", now=100.0)  # the retry at t=100 failed too
    assert r.due(now=100.0) == []
    assert r.due(now=100.0 + TERMINATE_RETRY_SECONDS) == ["a"]


def test_terminate_retries_are_ordered_deterministically():
    r = TerminateRetries()
    r.add("b", now=0.0)
    r.add("a", now=0.0)
    r.add("c", now=-100.0)
    assert r.pending() == ["a", "b", "c"]
    assert r.due(now=TERMINATE_RETRY_SECONDS) == ["c", "a", "b"]


# ---------------------------------------------------------------------------
# capacity_expired
#
# RunPod pods are not spot instances (cloudType SECURE/COMMUNITY,
# `interruptible` never set), so a pod we already hold is never taken back --
# the only question is whether a free one exists yet. That makes a shortage a
# QUEUE, not a failure, and onSetupCook used to kill the whole cook on it.
# These pin the backstop: past a per-item deadline, individual items give up
# so a re-cook retries only them.
# ---------------------------------------------------------------------------


def _task(task_id="t1", work_item_id=1):
    return TaskState(task_id=task_id, work_item_id=work_item_id, command="c", env={})


def test_enqueue_stamps_when_the_task_started_waiting():
    d = Dispatcher()
    t = _task()

    d.enqueue(t, now=1000.0)

    assert t.queued_at == 1000.0


def test_nothing_expires_before_the_deadline():
    d = Dispatcher()
    d.enqueue(_task(), now=1000.0)

    assert d.capacity_expired(1000.0 + 899, 900) == []


def test_a_task_expires_once_it_has_waited_long_enough():
    d = Dispatcher()
    t = _task()
    d.enqueue(t, now=1000.0)

    assert d.capacity_expired(1000.0 + 900, 900) == [t]


def test_only_the_tasks_past_the_deadline_expire():
    """The point of doing this per item: a re-cook must retry only these."""
    d = Dispatcher()
    old = _task("old", 1)
    new = _task("new", 2)
    d.enqueue(old, now=1000.0)
    d.enqueue(new, now=1800.0)

    expired = d.capacity_expired(1900.0, 900)

    assert [t.task_id for t in expired] == ["old"]
    assert [t.task_id for t in d.pending] == ["old", "new"]  # caller fails them


def test_zero_wait_means_wait_forever():
    d = Dispatcher()
    d.enqueue(_task(), now=0.0)

    assert d.capacity_expired(1e9, 0) == []
    assert d.capacity_expired(1e9, -1) == []


def test_expired_tasks_go_through_fail_pending_so_pdg_hears_about_them():
    d = Dispatcher()
    t = _task()
    d.enqueue(t, now=1000.0)

    for expired in d.capacity_expired(2000.0, 900):
        d.fail_pending(expired.task_id)

    assert d.pending == []
    assert [x.task_id for x in d.failed_since_last_call()] == ["t1"]


def test_a_requeued_task_restarts_its_capacity_clock():
    """It had a machine and lost it; the wait it already served says nothing
    about whether RunPod has capacity now."""
    d = Dispatcher()
    d.add_pod("pod1")
    t = _task()
    d.enqueue(t, now=1000.0)
    d.assign(t.task_id, "pod1")

    d.pod_dead("pod1", now=5000.0)

    assert t.queued_at == 5000.0
    assert d.capacity_expired(5100.0, 900) == []


# -- never more machines than there is work (2026-09-05) -----------------------
#
# The owner's RunPod panel: two RTX PRO 4000 at $0.57/hr for a cook with ONE
# task -- one pod at 99%, the other at exactly 0% for the whole render. Min
# Pods was applied without looking at how much work there was.


def test_one_item_raises_one_pod_even_with_min_pods_two():
    assert rpdispatch.initial_pods(min_pods=2, work_items=1) == 1


def test_three_items_raise_min_pods():
    assert rpdispatch.initial_pods(min_pods=2, work_items=3) == 2


def test_min_pods_is_a_floor_not_a_ceiling():
    """Two items and Min Pods 1 still raises one here -- the autoscaler adds
    more when it sees the work is worth it. This decision is only about not
    starting more machines than there are items."""
    assert rpdispatch.initial_pods(min_pods=1, work_items=5) == 1
    assert rpdispatch.initial_pods(min_pods=4, work_items=4) == 4
    assert rpdispatch.initial_pods(min_pods=0, work_items=3) == 1
    assert rpdispatch.initial_pods(min_pods=2, work_items=0) == 1


def test_the_autoscaler_does_not_outnumber_the_work_either():
    """Same defect, milder: the batch is sized by predicted TIME, which says
    nothing about how many items are left to spread across it. One slow item
    on one pod predicted a long cook and asked for four more machines."""
    added = rpdispatch.autoscale_decision(
        pending=1, active_pods=1, max_pods=8,
        render_times=[2400.0], threshold_min=10.0)

    assert added == 1, "one item cannot use more than one more machine"

    many = rpdispatch.autoscale_decision(
        pending=6, active_pods=1, max_pods=8,
        render_times=[2400.0], threshold_min=10.0)
    assert many > 1, "and real work still scales up"
