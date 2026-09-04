"""Tests for the scheduler HDA's own Python, lifted out of the asset.

`hda/runpodfarm_scheduler.hda/.../PythonModule` cannot be imported: it
subclasses PDG's `PyScheduler` and imports `hou` at module scope. Everything
decision-shaped was therefore moved into `rpfarm.dispatch`, which is unit
tested -- but the *glue* that calls it never was, and finding 6 of the final
whole-branch review lives exactly there:

* a >60s blip on this machine's uplink fails every pod's `/health` in the
  same sweep, so judging each pod on its own terminated the whole farm; and
* the terminate then failed for the same reason, and the pod was dropped
  from `_dispatcher.pods` with no list to retry it from -- still running and
  still billing, with `onStopCook` never trying again.

So these tests compile the individual methods out of the shipped asset and
run them against fakes. That is the code that ships, not a copy of it kept
here: rename a collaborator and this fails.
"""

import ast
import pathlib
import types

import pytest

from rpfarm import dispatch as rpdispatch
from rpfarm.runpod_api import RunPodError

MODULE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "hda" / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler" / "PythonModule"
)


def _tree():
    return ast.parse(MODULE.read_text())


def _module_constant(name):
    """A module-level ``NAME = <literal>`` from the asset, so the test never
    keeps its own drifting copy of a shipped constant."""
    for node in _tree().body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level constant of the scheduler asset")


def load_methods(names, extra_globals=None):
    """Compile the named methods out of the asset into plain functions."""
    found = {}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = node
    missing = set(names) - set(found)
    assert not missing, f"the scheduler asset no longer defines {sorted(missing)}"

    ns = {
        "time": types.SimpleNamespace(time=lambda: 0.0),
        "rpdispatch": rpdispatch,
        "RunPodError": RunPodError,
        "_HEALTH_POLL_INTERVAL": _module_constant("_HEALTH_POLL_INTERVAL"),
    }
    ns.update(extra_globals or {})
    mod = ast.Module(body=[found[n] for n in names], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, str(MODULE), "exec"), ns)  # noqa: S102 - the asset's own code
    return ns


class FakeHealthClient:
    def __init__(self, answers):
        self.answers = list(answers)

    def health(self):
        return self.answers.pop(0) if self.answers else None


class FakeScheduler:
    """Just enough of the scheduler object for the lifted methods."""

    def __init__(self, api=None):
        self._dispatcher = rpdispatch.Dispatcher(1)
        self._clients = {}
        self._last_health_poll = {}
        self._outage = rpdispatch.OutageTracker()
        self._terminate_retries = rpdispatch.TerminateRetries()
        self._api = api
        self.logs = []
        self.given_up = []
        self.promoted = []
        self.retired = []

    # collaborators the lifted methods call
    def _log(self, msg):
        self.logs.append(msg)

    def _verboseLog(self, msg):
        self.logs.append(msg)

    def _promote_pod(self, pod, health):
        pod.status = "RUNNING"
        self.promoted.append(pod.pod_id)

    def _giveUpOnPod(self, pod, reason):
        # The real one terminates the pod and drops it from the pool; only
        # the drop matters to the caller under test (it must not be judged
        # a second time on the next sweep).
        self.given_up.append((pod.pod_id, reason))
        self._dispatcher.pod_dead(pod.pod_id)
        self._clients.pop(pod.pod_id, None)
        self._last_health_poll.pop(pod.pod_id, None)

    def _retire_pod_cost(self, pod):
        self.retired.append(pod.pod_id)


class FakeApi:
    def __init__(self, fail_ids=(), fail_times=None):
        self.fail_ids = set(fail_ids)
        self.fail_times = fail_times  # None = fail forever
        self.terminated = []

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)
        if pod_id in self.fail_ids:
            if self.fail_times is not None:
                self.fail_times -= 1
                if self.fail_times <= 0:
                    self.fail_ids.discard(pod_id)
            raise RunPodError(0, "network down")


def _clocked(names, clock, extra_globals=None):
    g = {"time": types.SimpleNamespace(time=lambda: clock[0])}
    g.update(extra_globals or {})
    return load_methods(names, g)


# -- the all-pods-fail backoff ------------------------------------------------


def _running_farm(sched, pod_ids, clients):
    for pid in pod_ids:
        sched._dispatcher.add_pod(pid, 1.0, status="RUNNING", created_at=0.0)
        sched._clients[pid] = clients[pid]


def test_a_partition_that_fails_every_pod_does_not_kill_the_farm():
    """Every pod talks through the same proxy over the same uplink, so a
    blip on this side fails all of them at once. Judging each pod on its own
    terminated the lot within POD_DEAD_AFTER_SECONDS."""
    clock = [0.0]
    ns = _clocked(["_poll_pods"], clock)
    sched = FakeScheduler()
    _running_farm(sched, ["a", "b", "c"], {p: FakeHealthClient([]) for p in "abc"})

    for _ in range(60):  # 5 minutes of nobody answering
        clock[0] += 5.0
        ns["_poll_pods"](sched)

    assert sched.given_up == [], "a local outage must not terminate the farm"
    assert any("local outage" in ln for ln in sched.logs)


def test_the_backoff_is_bounded_so_a_farm_that_really_is_gone_is_cleaned_up():
    clock = [0.0]
    ns = _clocked(["_poll_pods"], clock)
    sched = FakeScheduler()
    _running_farm(sched, ["a", "b"], {p: FakeHealthClient([]) for p in "ab"})

    limit = rpdispatch.POD_DEAD_AFTER_SECONDS + rpdispatch.OUTAGE_GRACE_SECONDS
    while clock[0] < limit + 30:
        clock[0] += 5.0
        ns["_poll_pods"](sched)

    assert sorted(p for p, _ in sched.given_up) == ["a", "b"]


def test_one_pod_dying_while_the_others_answer_is_still_a_dead_pod():
    """The ordinary case must not get the outage grace: the pods that
    answered prove the uplink is fine."""
    clock = [0.0]
    ns = _clocked(["_poll_pods"], clock)
    sched = FakeScheduler()
    _running_farm(sched, ["a", "b"], {
        "a": FakeHealthClient([]),                      # never answers again
        "b": FakeHealthClient([{"role": "gpu"}] * 500),  # keeps answering
    })

    while clock[0] < rpdispatch.POD_DEAD_AFTER_SECONDS + 20:
        clock[0] += 5.0
        ns["_poll_pods"](sched)

    assert [p for p, _ in sched.given_up] == ["a"]


def test_the_outage_streak_ends_as_soon_as_one_pod_answers_again():
    clock = [0.0]
    ns = _clocked(["_poll_pods"], clock)
    sched = FakeScheduler()
    # Nobody answers for a while, then everyone comes back.
    _running_farm(sched, ["a", "b"], {
        "a": FakeHealthClient([None] * 20 + [{"role": "gpu"}] * 500),
        "b": FakeHealthClient([None] * 20 + [{"role": "gpu"}] * 500),
    })

    for _ in range(40):
        clock[0] += 5.0
        ns["_poll_pods"](sched)

    assert sched.given_up == []
    assert sched._outage.since is None
    assert sched._dispatcher.pods["a"].health_fail_since is None


def test_a_booting_farm_is_not_an_outage():
    """A CREATED pod has not failed a heartbeat, it has not started one --
    it must not extend anyone's deadline, and its own boot clock stands."""
    clock = [0.0]
    ns = _clocked(["_poll_pods"], clock)
    sched = FakeScheduler()
    for pid in ("a", "b"):
        sched._dispatcher.add_pod(pid, 1.0, status="CREATED", created_at=0.0)
        sched._clients[pid] = FakeHealthClient([])

    while clock[0] < rpdispatch.POD_BOOT_TIMEOUT_SECONDS + 20:
        clock[0] += 5.0
        ns["_poll_pods"](sched)

    assert sorted(p for p, _ in sched.given_up) == ["a", "b"]
    assert all(reason == "boot" for _p, reason in sched.given_up)
    assert sched._outage.since is None


# -- the terminate retry list -------------------------------------------------


def test_a_failed_terminate_is_kept_and_retried_by_the_tick():
    """It used to be logged and forgotten, with the pod dropped from the
    pool -- still running, still billing, and nothing left that knew its id."""
    clock = [0.0]
    api = FakeApi(fail_ids=["a"], fail_times=1)
    ns = _clocked(["_terminate_pod", "_retryTerminations"], clock)
    sched = FakeScheduler(api=api)
    sched._dispatcher.add_pod("a", 1.0)

    ns["_terminate_pod"](sched, "a")
    assert sched._terminate_retries.pending() == ["a"]
    assert any("will retry" in ln for ln in sched.logs)

    # Too soon: the backoff has not elapsed.
    ns["_retryTerminations"](sched)
    assert api.terminated == ["a"]

    clock[0] += rpdispatch.TERMINATE_RETRY_SECONDS + 1
    ns["_retryTerminations"](sched)
    assert api.terminated == ["a", "a"]
    assert sched._terminate_retries.pending() == []
    assert any("terminated on retry" in ln for ln in sched.logs)


def test_a_retry_that_fails_again_stays_on_the_list():
    clock = [0.0]
    api = FakeApi(fail_ids=["a"])  # fails forever
    ns = _clocked(["_terminate_pod", "_retryTerminations"], clock)
    sched = FakeScheduler(api=api)
    sched._dispatcher.add_pod("a", 1.0)

    ns["_terminate_pod"](sched, "a")
    for _ in range(3):
        clock[0] += rpdispatch.TERMINATE_RETRY_SECONDS + 1
        ns["_retryTerminations"](sched)
    assert sched._terminate_retries.pending() == ["a"]
    assert len(api.terminated) == 4


def test_a_retry_never_re_banks_the_pods_cost():
    """The first attempt already banked the pod's spend into the cook total;
    doing it again would inflate the budget decision and the ledger."""
    clock = [0.0]
    api = FakeApi(fail_ids=["a"], fail_times=1)
    ns = _clocked(["_terminate_pod", "_retryTerminations"], clock)
    sched = FakeScheduler(api=api)
    sched._dispatcher.add_pod("a", 1.0)

    ns["_terminate_pod"](sched, "a")
    clock[0] += rpdispatch.TERMINATE_RETRY_SECONDS + 1
    ns["_retryTerminations"](sched)
    assert sched.retired == ["a"]


def test_stop_cook_forces_a_last_attempt_regardless_of_the_backoff():
    clock = [0.0]
    api = FakeApi(fail_ids=["a"], fail_times=1)
    ns = _clocked(["_terminate_pod", "_retryTerminations"], clock)
    sched = FakeScheduler(api=api)
    sched._dispatcher.add_pod("a", 1.0)

    ns["_terminate_pod"](sched, "a")
    ns["_retryTerminations"](sched, force=True)  # no time has passed at all
    assert sched._terminate_retries.pending() == []


def test_a_successful_terminate_leaves_nothing_on_the_list():
    clock = [0.0]
    api = FakeApi()
    ns = _clocked(["_terminate_pod", "_retryTerminations"], clock)
    sched = FakeScheduler(api=api)
    sched._dispatcher.add_pod("a", 1.0)

    ns["_terminate_pod"](sched, "a")
    assert sched._terminate_retries.pending() == []
    assert not any("TERMINATE FAILED" in ln for ln in sched.logs)


def test_stop_cook_drains_and_then_names_what_is_still_billing():
    """The wiring in onStopCook, checked as source: after _reset_cook_state
    nothing holds the pod ids any more, so this really is the last chance."""
    src = MODULE.read_text()
    stop = src[src.index("def onStopCook("):]
    stop = stop[:stop.index("\n    def ", 1)]
    assert "self._retryTerminations(force=True)" in stop
    assert "STILL RUNNING AND BILLING" in stop
    assert stop.index("self._retryTerminations(force=True)") < stop.index("self._reset_cook_state()")


def test_the_tick_drains_the_retry_list():
    src = MODULE.read_text()
    tick = src[src.index("def onTick("):]
    tick = tick[:tick.index("\n    def ", 1)]
    assert "self._retryTerminations()" in tick
