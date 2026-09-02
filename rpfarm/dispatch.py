"""The scheduler's decision-making, with no ``pdg`` and no ``hou`` in it.

Everything the RunPodFarm HDA decides -- which pod a task goes to, what
happens to a dead pod's tasks, when to add pods, when a cook has spent too
much -- lives here so it can be unit-tested without Houdini. The HDA's
``PythonModule`` is the glue: it owns the RunPod API calls, the
:class:`~rpfarm.worker_client.WorkerClient` HTTP calls and the PDG
callbacks, and asks this module what to do.

Stdlib only, like the rest of ``rpfarm`` (it is imported inside Houdini's
bundled Python).

Pod lifecycle as seen from here:

``CREATED``
    Created via the RunPod API; its worker daemon has not answered
    ``/health`` yet. Never picked for work.
``RUNNING``
    The worker answers. Eligible for work up to ``slots`` concurrent tasks.
``DEAD``
    Stopped answering for long enough that the HDA gave up on it.
    :meth:`Dispatcher.pod_dead` requeues its tasks and drops it from the
    pool.

``add_pod`` defaults to ``RUNNING`` because most callers (and every test)
just want a usable pod; the HDA passes ``status="CREATED"`` explicitly for
a pod it has only just asked RunPod for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Rolling window of recent work-item durations used to predict how much
# longer the cook needs (v1 constant, unchanged).
AUTOSCALE_HISTORY_SIZE = 10

# Below this much predicted remaining time, adding pods isn't worth the
# ~45s each one costs to boot.
AUTOSCALE_THRESHOLD_MINUTES = 30

# A scale-up event always adds between 2 and 4 pods (v1 constants).
AUTOSCALE_BATCH_MIN = 2
AUTOSCALE_BATCH_MAX = 4

# Budget thresholds as a fraction of the cook's cost limit.
BUDGET_WARN = 0.8
BUDGET_STOP = 1.0
BUDGET_KILL = 1.2


@dataclass
class PodState:
    pod_id: str
    cost_per_hr: float = 0.0
    status: str = "RUNNING"  # CREATED | RUNNING | DEAD
    created_at: float | None = None
    gpu: str = ""
    slots: int = 1
    running: set[str] = field(default_factory=set)
    idle_since: float | None = None
    health_fail_since: float | None = None
    # Set for the rest of the tick when the worker answered 429 (its slot
    # count and ours disagree -- e.g. a task from an earlier cook is still
    # running on it). Cleared by Dispatcher.release_full() each tick.
    full: bool = False

    def free_slots(self) -> int:
        if self.status != "RUNNING" or self.full:
            return 0
        return max(0, self.slots - len(self.running))


@dataclass
class TaskState:
    task_id: str
    work_item_id: int
    command: str
    env: dict
    pod_id: str | None = None
    attempts: int = 0
    started: float | None = None
    log_path: str = ""


class Dispatcher:
    """Queue of pending tasks plus the pool of pods they run on.

    Not thread-safe: every method is called from PDG's scheduler thread
    (``onSchedule`` / ``onTick``), which is single-threaded.
    """

    def __init__(self, slots_per_pod=1, max_attempts=3):
        self.slots_per_pod = max(1, int(slots_per_pod))
        self.max_attempts = max(1, int(max_attempts))
        self.pods: dict[str, PodState] = {}
        self._pending: list[TaskState] = []
        self._running: dict[str, TaskState] = {}
        self.failed: list[TaskState] = []
        self._failed_cursor = 0

    # -- pods ---------------------------------------------------------------

    def add_pod(self, pod_id, cost_per_hr=0.0, status="RUNNING", created_at=None, gpu=""):
        """Register a pod. A pod_id already in the pool is left alone, so a
        retried create or a duplicate poll never resets a live pod's state."""
        if pod_id in self.pods:
            return self.pods[pod_id]
        pod = PodState(
            pod_id=pod_id,
            cost_per_hr=float(cost_per_hr or 0.0),
            status=status,
            created_at=created_at,
            gpu=gpu,
            slots=self.slots_per_pod,
        )
        self.pods[pod_id] = pod
        return pod

    def remove_pod(self, pod_id) -> list[TaskState]:
        """Drop a pod from the pool, requeueing whatever it was running.

        Same bookkeeping as :meth:`pod_dead`; the two differ only in intent
        (a deliberate idle scale-down vs. a pod that stopped answering).
        """
        return self.pod_dead(pod_id)

    def pod_dead(self, pod_id) -> list[TaskState]:
        """Give up on a pod: requeue its tasks and drop it from the pool.

        Each requeued task's ``attempts`` goes up by one. A task that has
        now been attempted ``max_attempts`` times goes to :attr:`failed`
        instead of back on the queue. Returns only the tasks that were
        requeued -- the caller reads the give-ups from
        :meth:`failed_since_last_call`.
        """
        pod = self.pods.pop(pod_id, None)
        if pod is None:
            return []
        pod.status = "DEAD"

        retry = []
        for task_id in sorted(pod.running):
            task = self._running.pop(task_id, None)
            if task is None:
                continue
            task.pod_id = None
            task.started = None
            task.attempts += 1
            if task.attempts >= self.max_attempts:
                self.failed.append(task)
            else:
                self._pending.append(task)
                retry.append(task)
        pod.running.clear()
        return retry

    def pick_pod(self) -> PodState | None:
        """Least-loaded RUNNING pod that still has a free slot, or None.

        Ties break on pod_id so the choice is deterministic.
        """
        candidates = [p for p in self.pods.values() if p.free_slots() > 0]
        if not candidates:
            return None
        return min(candidates, key=lambda p: (len(p.running), p.pod_id))

    def mark_full(self, pod_id) -> None:
        """The worker said 429: treat the pod as having no free slots until
        the next tick calls :meth:`release_full`."""
        pod = self.pods.get(pod_id)
        if pod is not None:
            pod.full = True

    def release_full(self) -> None:
        for pod in self.pods.values():
            pod.full = False

    def active_pods(self) -> int:
        return sum(1 for p in self.pods.values() if p.status == "RUNNING")

    # -- tasks --------------------------------------------------------------

    @property
    def pending(self) -> list[TaskState]:
        return self._pending

    def enqueue(self, task: TaskState) -> TaskState:
        self._pending.append(task)
        return task

    def assign(self, task_id, pod_id) -> TaskState | None:
        """Move a pending task onto a pod. No-op for an unknown task id."""
        pod = self.pods.get(pod_id)
        for i, t in enumerate(self._pending):
            if t.task_id == task_id:
                task = self._pending.pop(i)
                break
        else:
            return None
        task.pod_id = pod_id
        self._running[task_id] = task
        if pod is not None:
            pod.running.add(task_id)
            pod.idle_since = None
        return task

    def complete(self, task_id, ok: bool) -> TaskState | None:
        """Retire a running task. Returns it, or None if it isn't running."""
        task = self._running.pop(task_id, None)
        if task is None:
            return None
        pod = self.pods.get(task.pod_id)
        if pod is not None:
            pod.running.discard(task_id)
        if not ok:
            self.failed.append(task)
        return task

    def retask(self, task_id, new_task_id) -> str | None:
        """Rename a pending task's id.

        ``POST /tasks`` on the worker rejects a task_id it has seen before
        (409), so a task that comes back "duplicate" needs a fresh id
        before it can be submitted again.
        """
        for t in self._pending:
            if t.task_id == task_id:
                t.task_id = new_task_id
                return new_task_id
        return None

    def running_tasks(self) -> list[TaskState]:
        return list(self._running.values())

    def failed_since_last_call(self) -> list[TaskState]:
        """Tasks added to :attr:`failed` since the previous call.

        The HDA uses this to report each give-up to PDG exactly once;
        :attr:`failed` itself keeps the whole cook's history.
        """
        new = self.failed[self._failed_cursor:]
        self._failed_cursor = len(self.failed)
        return new

    def task_for_work_item(self, work_item_id) -> TaskState | None:
        for t in list(self._running.values()) + self._pending:
            if t.work_item_id == work_item_id:
                return t
        return None

    # -- idle ---------------------------------------------------------------

    def touch_idle(self, now: float) -> None:
        """Start (or clear) each RUNNING pod's idle clock.

        A pod with nothing running starts its clock at ``now`` if it isn't
        already ticking; a pod that has work clears it. v1's bug was doing
        this only when the *whole queue* was empty, so a pod that finished
        early while others still had work never counted as idle.
        """
        for pod in self.pods.values():
            if pod.status != "RUNNING":
                continue
            if pod.running:
                pod.idle_since = None
            elif pod.idle_since is None:
                pod.idle_since = now

    def idle_pods(self, now: float, idle_seconds: float) -> list[PodState]:
        return [
            p
            for p in self.pods.values()
            if p.status == "RUNNING"
            and p.idle_since is not None
            and now - p.idle_since > idle_seconds
        ]


# -- pure decisions ----------------------------------------------------------


def autoscale_decision(
    pending: int,
    active_pods: int,
    max_pods: int,
    render_times,
    threshold_min: float = AUTOSCALE_THRESHOLD_MINUTES,
    over_budget: bool = False,
) -> int:
    """How many pods to add right now (0 = none).

    The v1 ``_autoscale`` rule with the side effects removed: predict the
    remaining wall-clock time as ``avg(render_times) * pending /
    active_pods``; if that is more than ``threshold_min`` minutes, add a
    batch of ``clamp(int(remaining / threshold), 2, 4)`` pods. The special
    case is a cold start -- no pods at all but work waiting -- which adds
    exactly one, since there is no render history to predict from yet.

    The result is always clamped to the ``max_pods - active_pods``
    headroom, and is 0 whenever the cook is over budget.
    """
    if over_budget or pending <= 0:
        return 0
    headroom = max(0, int(max_pods) - int(active_pods))
    if headroom == 0:
        return 0

    if active_pods == 0:
        return min(1, headroom)
    if not render_times:
        return 0

    avg = sum(render_times) / len(render_times)
    remaining_min = (avg * pending) / active_pods / 60.0
    if remaining_min <= threshold_min:
        return 0

    ratio = remaining_min / threshold_min
    batch = min(AUTOSCALE_BATCH_MAX, max(AUTOSCALE_BATCH_MIN, int(ratio)))
    return min(batch, headroom)


def budget_state(total_cost: float, max_cost: float) -> str:
    """``"ok"`` | ``"warn"`` (80%) | ``"stop"`` (100%) | ``"kill"`` (120%).

    ``max_cost`` of 0 (or infinity) means "no limit" and is always ``ok``.
    """
    if not max_cost or max_cost == float("inf"):
        return "ok"
    ratio = total_cost / max_cost
    if ratio >= BUDGET_KILL:
        return "kill"
    if ratio >= BUDGET_STOP:
        return "stop"
    if ratio >= BUDGET_WARN:
        return "warn"
    return "ok"


# -- ledger stand-in ---------------------------------------------------------


def append_record(path, **record) -> None:
    """Append one JSON object as a line to ``path``, creating parents.

    A minimal stand-in for ``rpfarm.ledger.append`` (Task 11). Writing the
    ledger is bookkeeping alongside a cook, never the cook itself, so a
    failure here is swallowed rather than allowed to fail a work item.
    """
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass
