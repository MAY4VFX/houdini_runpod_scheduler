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
import json
import pathlib
import sys
import time
import types

import pytest

from rpfarm import dispatch as rpdispatch
from rpfarm import packages as rppkg
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


# -- the sync-touch the scheduler sends (Task 17, residual A) -----------------


class _StaleImageSyncClient:
    """A sync pod whose /opt/rpfarm/housekeeping.py predates ``sync-touch``."""

    def exec(self, command, timeout_s=600):
        return {
            "exit_code": 2,
            "stdout": "",
            "stderr": "housekeeping.py: error: argument command: invalid choice: 'sync-touch'",
        }


def test_the_scheduler_says_so_when_its_sync_touch_fails():
    """`WorkerClient.exec` never raises on a non-zero exit, and this method
    used to throw the result away -- so a stale pod image left the idle
    stamp unwritten (the pod then never auto-retires and keeps billing)
    with no line anywhere. The real rpfarm.packages is used here, not a
    stand-in: rename touch_sync_pod and this fails."""
    from rpfarm import packages as rppkg

    ns = load_methods(["_touchSyncPod"], {"rppkg": rppkg})
    sched = FakeScheduler()
    sched._sync_client = _StaleImageSyncClient()

    ns["_touchSyncPod"](sched)

    assert len(sched.logs) == 1
    assert "sync pod idle stamp NOT written" in sched.logs[0]
    assert "invalid choice" in sched.logs[0]


def test_a_working_sync_touch_stays_quiet():
    from rpfarm import packages as rppkg

    class Ok:
        def exec(self, command, timeout_s=600):
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    ns = load_methods(["_touchSyncPod"], {"rppkg": rppkg})
    sched = FakeScheduler()
    sched._sync_client = Ok()
    ns["_touchSyncPod"](sched)
    assert sched.logs == []


# -- the Datacenter parm baked into an old scene (Task 17, residual B) --------


class _CookError(Exception):
    """Stands in for pdg.CookError, which needs PDG to import."""


class _Parm:
    def __init__(self, value):
        self.value = value

    def evaluateString(self):
        return self.value


class _DatacenterScheduler(FakeScheduler):
    """A scheduler whose parms and config can disagree, as they do in a
    .hip saved before the Datacenter default became empty."""

    def __init__(self, node_dc, cfg_dc="US-KS-2", node_volume="", cfg_volume="vol-real"):
        super().__init__()
        self._cfg = types.SimpleNamespace(datacenter=cfg_dc, volume_id=cfg_volume)
        self._parms = {
            "rpfarm_datacenter": _Parm(node_dc),
            "rpfarm_networkvolumeid": _Parm(node_volume),
        }

    def __getitem__(self, name):
        return self._parms[name]

    def _volumeId(self):
        return self._parms["rpfarm_networkvolumeid"].evaluateString() or self._cfg.volume_id


def _datacenter_check():
    return load_methods(["_checkDatacenter"], {"CookError": _CookError})["_checkDatacenter"]


def test_a_stale_baked_in_datacenter_fails_the_cook_with_the_fix():
    """The trap: the parm shipped defaulting to EU-RO-1, a scene saved then
    keeps that literal, and _datacenterId's `parm or config` hands it to
    every create_gpu_pod -- so pods land where the volume is not and cannot
    mount /workspace, while `doctor` (config only) says everything is
    fine."""
    with pytest.raises(_CookError) as e:
        _datacenter_check()(_DatacenterScheduler("EU-RO-1", cfg_dc="US-KS-2"))

    message = str(e.value)
    assert "EU-RO-1" in message and "US-KS-2" in message
    assert "Revert to Defaults" in message, "the fix has to name the actual gesture"
    assert "cannot mount" in message


def test_the_empty_parm_means_the_config_and_is_never_a_mismatch():
    _datacenter_check()(_DatacenterScheduler("", cfg_dc="US-KS-2"))  # no raise


def test_a_matching_parm_is_fine():
    _datacenter_check()(_DatacenterScheduler("US-KS-2", cfg_dc="US-KS-2"))  # no raise


def test_whitespace_is_not_a_mismatch():
    _datacenter_check()(_DatacenterScheduler("  US-KS-2 ", cfg_dc="US-KS-2"))  # no raise


def test_an_overridden_volume_is_taken_as_deliberate_and_only_warned_about():
    """The config's region is the *configured* volume's region. Point the
    node at another volume and we have no way to know where that one lives,
    so this must not be the thing that fails the cook."""
    sched = _DatacenterScheduler("EU-RO-1", cfg_dc="US-KS-2", node_volume="vol-other")
    _datacenter_check()(sched)  # no raise
    assert any("deliberate" in m for m in sched.logs)


def test_no_configured_region_is_nothing_to_compare_against():
    _datacenter_check()(_DatacenterScheduler("EU-RO-1", cfg_dc=""))  # no raise


def test_the_cook_runs_the_datacenter_check_before_creating_anything():
    """A check nobody calls is not a check. It has to happen in
    onStartCook, which is the only callback that sees the node."""
    src = MODULE.read_text()
    assert "self._checkDatacenter()" in src
    start = src.index("def onStartCook")
    assert src.index("self._checkDatacenter()", start) < src.index("def onSetupCook")


# ---------------------------------------------------------------------------
# _expireWaitingItems / _cloudType -- Task 19
#
# A shortage of machines is a queue, not a dead cook: our pods are not spot
# instances, so one we hold is never taken back and the only question is
# whether a free one exists yet. onSetupCook used to raise CookError when the
# first scale-up got nothing, killing the cook at second zero over a condition
# that clears in a couple of minutes. These cover the backstop that replaced
# it -- a per-item deadline, so a re-cook retries only the items that never
# got a machine.
# ---------------------------------------------------------------------------


class _CapacityScheduler(FakeScheduler):
    def __init__(self, wait_min=15, cloud="SECURE", now=0.0):
        super().__init__()
        self._parms = {"rpfarm_capacitywait": wait_min, "rpfarm_cloudtype": cloud}
        self._now = now
        self._last_pod_error = "RunPod 500: no longer any instances available"
        self._capacity_give_up_reason = ""
        self.reported = []

    def __getitem__(self, name):
        value = self._parms[name]
        return types.SimpleNamespace(
            evaluateInt=lambda: int(value),
            evaluateString=lambda: str(value),
        )

    def _datacenterId(self):
        return "EU-RO-1"

    def _cloudType(self):
        return self._parms["rpfarm_cloudtype"]

    def _datacenterId(self):
        return "EU-RO-1"

    def _capacityWaitSeconds(self):
        return max(0, int(self._parms["rpfarm_capacitywait"])) * 60

    def _reportFailures(self):
        self.reported.append(self._capacity_give_up_reason)


class _NoOtherCloud:
    """Community has nothing in EU-RO-1, so there is no hint to give."""

    @staticmethod
    def other_cloud_hint(*_a, **_kw):
        return ""


def _capacity_ns(now):
    return load_methods(
        ["_expireWaitingItems"],
        extra_globals={"time": types.SimpleNamespace(time=lambda: now),
                       "rpgpus": _NoOtherCloud,
                       "CLOUD_TYPE_COMMUNITY": "COMMUNITY"},
    )


def _queue(sched, task_id, work_item_id, queued_at):
    task = rpdispatch.TaskState(task_id=task_id, work_item_id=work_item_id,
                                command="c", env={})
    sched._dispatcher.enqueue(task, now=queued_at)
    return task


def test_items_still_within_the_deadline_are_left_waiting():
    sched = _CapacityScheduler(wait_min=15)
    _queue(sched, "t1", 1, queued_at=0.0)

    _capacity_ns(now=600.0)["_expireWaitingItems"](sched)

    assert [t.task_id for t in sched._dispatcher.pending] == ["t1"]
    assert sched.reported == []


def test_an_item_past_the_deadline_fails_and_says_why():
    sched = _CapacityScheduler(wait_min=15, cloud="SECURE")
    _queue(sched, "t1", 1, queued_at=0.0)

    _capacity_ns(now=901.0)["_expireWaitingItems"](sched)

    assert sched._dispatcher.pending == []
    assert [t.task_id for t in sched._dispatcher.failed] == ["t1"]
    assert len(sched.reported) == 1
    reason = sched.reported[0]
    assert "15 min" in reason and "EU-RO-1" in reason
    assert "GPUs To Use" in reason                   # the actionable way out
    # The other cloud is named only when it actually has capacity; here it
    # does not, so the message must not send anyone there.
    assert "Community has" not in reason
    assert "no longer any instances" in reason       # RunPod's own last word


def test_only_the_expired_items_fail():
    """The reason it is per item: a re-cook must retry only these."""
    sched = _CapacityScheduler(wait_min=15)
    _queue(sched, "old", 1, queued_at=0.0)
    _queue(sched, "new", 2, queued_at=800.0)

    _capacity_ns(now=901.0)["_expireWaitingItems"](sched)

    assert [t.task_id for t in sched._dispatcher.pending] == ["new"]
    assert [t.task_id for t in sched._dispatcher.failed] == ["old"]


def test_a_zero_deadline_waits_forever_and_never_reports():
    sched = _CapacityScheduler(wait_min=0)
    _queue(sched, "t1", 1, queued_at=0.0)

    _capacity_ns(now=1e9)["_expireWaitingItems"](sched)

    assert [t.task_id for t in sched._dispatcher.pending] == ["t1"]
    assert sched.reported == []


def test_setup_cook_raises_no_machines_at_all():
    """Two defects met here. A shortage at second zero used to kill the cook
    (R32), and scaling up at cook start paid for machines a fully cached cook
    never used. Both are answered by not creating pods until work exists."""
    src = MODULE.read_text()
    setup = src[src.index("def onSetupCook"):]
    setup = setup[:setup.index("def _uploadPdgTemp")]

    # no CALL to _scale_up (the words still appear in the comment explaining why)
    assert "self._scale_up(" not in setup
    assert "self._raised_for_work = False" in setup
    assert "pods will be raised when the first work item needs one" in setup
    # the one thing that IS still fatal here: no sync pod means nothing can run
    assert "except rppods.SyncPodCapacityError as e:" in setup


def test_the_transient_flag_comes_from_is_capacity_error():
    src = MODULE.read_text()

    assert "self._last_pod_error_transient = is_capacity_error(e)" in src
    assert "cloud_type=self._cloudType()" in src


def test_setup_cook_turns_a_sync_pod_shortage_into_a_cook_error():
    """The GPU side fails items; the sync pod has nothing to fail item by item.

    Without it the cook never starts, so the whole cook failing IS the right
    answer -- but with the human text the exception already carries, not a raw
    RunPod 500 in the artist's face.
    """
    src = MODULE.read_text()

    assert "except rppods.SyncPodCapacityError as e:" in src
    assert "raise CookError(str(e))" in src
    # and it is given the same two parms, not a second set of knobs
    assert "capacity_wait_s=self._capacityWaitSeconds()," in src
    assert "cloud_type=self._cloudType()," in src


# ---------------------------------------------------------------------------
# stale sys.modules guard
#
# The asset and the package are updated together, but Python caches modules for
# the life of the process: a Houdini already open when the checkout updated
# loads the NEW asset against the OLD package. In the field that surfaced on
# scene open as
#     ImportError: cannot import name 'CLOUD_TYPE_SECURE' from 'rpfarm.runpod_api'
# which names a symbol the artist has never heard of and does not say that the
# fix is to restart Houdini.
# ---------------------------------------------------------------------------


def _guard():
    import hashlib
    import os

    return load_methods(
        ["_version_tuple", "_ondisk_rpfarm_version", "_stale_module_message",
         "_ondisk_fingerprint", "_asset_mismatch"],
        extra_globals={"ast": ast, "pathlib": pathlib, "hashlib": hashlib, "os": os},
    )


def test_matching_versions_say_nothing():
    ns = _guard()

    assert ns["_stale_module_message"]("2.1.0", "2.1.0", "2.1.0", "/root") is None


def test_a_newer_package_than_the_asset_needs_is_fine():
    """The declared version is a floor. Bumping rpfarm.VERSION on its own must
    not make every scene shout, and a new package with an old asset never
    broke anything -- only the other direction does."""
    ns = _guard()

    assert ns["_stale_module_message"]("2.1.0", "2.4.7", "2.4.7", "/root") is None


def test_version_tuple_never_raises_on_junk():
    ns = _guard()

    assert ns["_version_tuple"]("2.1.0") == (2, 1, 0)
    assert ns["_version_tuple"]("2.1.0rc1") == (2, 1, 1)
    assert ns["_version_tuple"](None) == (0,)
    assert ns["_version_tuple"]("") == (0,)
    assert ns["_version_tuple"]("junk") == (0,)
    assert ns["_version_tuple"]("2.1.0") > ns["_version_tuple"]("2.0.9")


def test_the_on_disk_version_is_read_without_importing(tmp_path):
    """Importing is what returns the cached module, so it cannot answer this."""
    ns = _guard()
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""doc"""\n\nVERSION = "2.1.0"\n')

    assert ns["_ondisk_rpfarm_version"](tmp_path) == "2.1.0"
    assert "rpfarm" not in sys.modules or sys.modules["rpfarm"].VERSION != "2.1.0-marker"


def test_an_unreadable_or_odd_init_returns_none_instead_of_exploding(tmp_path):
    ns = _guard()
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()

    assert ns["_ondisk_rpfarm_version"](tmp_path) is None      # no __init__ at all

    (pkg / "__init__.py").write_text("this is not python (((")
    assert ns["_ondisk_rpfarm_version"](tmp_path) is None      # unparseable

    (pkg / "__init__.py").write_text("OTHER = 1\n")
    assert ns["_ondisk_rpfarm_version"](tmp_path) is None      # no VERSION


def test_the_asset_declares_a_minimum_and_checks_it_before_importing_symbols():
    """Order matters: the guard is useless after the import it protects."""
    src = MODULE.read_text()

    guard = src.index("_stale = _stale_module_message(")
    first_symbol_import = src.index("from rpfarm.runpod_api import")
    assert guard < first_symbol_import

    assert '_MIN_RPFARM_VERSION = "' in src
    assert "raise ImportError(_stale)" in src


def test_the_demo_scene_never_enables_both_download_paths():
    """Guarding the builder, not just the built scene: the trap is that both
    settings are individually reasonable and only their combination is wrong."""
    builder = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "build_demo_scene.py").read_text(encoding="utf-8")

    # greedy download stays on -- frames appear while the farm still renders
    assert 'sched.parm("rpfarm_downloadoutputs").set(1)' in builder
    # ...so no download node may be created in the chain
    assert 'createNode("runpodfarmdownload"' not in builder
    # and _verify refuses a scene where one came back
    assert 'hou.node("/obj/topnet1/download") is None' in builder


# ---------------------------------------------------------------------------
# XPU preflight -- one clear refusal instead of eight identical crashes
# ---------------------------------------------------------------------------


class _XpuScheduler(FakeScheduler):
    def __init__(self, supported, rops=("/out/karma_demo",)):
        super().__init__()
        self._cfg = types.SimpleNamespace(xpu_supported=supported)
        self._rops = list(rops)

    def _xpuRops(self):
        return self._rops


class _CookError(Exception):
    pass


def _preflight():
    """The asset raises CookError, which is PDG's; stand one in for it."""
    return load_methods(["_preflightXpu"],
                        extra_globals={"CookError": _CookError})["_preflightXpu"]


def test_no_xpu_rops_means_nothing_to_say():
    sched = _XpuScheduler(supported=False, rops=[])

    _preflight()(sched)          # must not raise

    assert sched.logs == []


def test_a_known_bad_farm_refuses_the_cook_before_paying_for_it():
    sched = _XpuScheduler(supported=False)

    with pytest.raises(_CookError) as excinfo:
        _preflight()(sched)

    message = str(excinfo.value)
    assert "/out/karma_demo" in message
    assert "cannot run it" in message
    assert "rpfarm farm xpu" in message          # how to re-check
    assert "CPU" in message                      # and the other way out


def test_a_known_good_farm_just_says_so():
    sched = _XpuScheduler(supported=True)

    _preflight()(sched)

    assert any("checked available" in m for m in sched.logs)


def test_an_unchecked_farm_is_not_treated_as_broken():
    """Guessing 'no' here would block a farm that works."""
    sched = _XpuScheduler(supported=None)

    _preflight()(sched)          # must not raise

    assert any("never been checked" in m for m in sched.logs)


# ---------------------------------------------------------------------------
# machines are raised when there is work, not when a cook starts
#
# onSetupCook used to scale up to Min Pods immediately. PDG only decides
# whether an item needs running later, per item, in onSchedule -- a cached one
# never reaches a scheduler at all -- so a cook whose outputs were already on
# disk created two GPU pods, found all 8 items CookedCache, and terminated them
# having rendered nothing (cook 42600deb: 0 tasks, ~$0.02).
# ---------------------------------------------------------------------------


class _StubPdgNode:
    def __init__(self, name, items):
        self.name = name
        self.workItems = list(range(items))


class _ScaleScheduler(FakeScheduler):
    def __init__(self, minpods=2, pending=0, scale_ok=True,
                 last_error=None, transient=True):
        super().__init__()
        self._parms = {"rpfarm_minpods": minpods, "rpfarm_maxpods": 2}
        self._raised_for_work = False
        self._last_scale_failure = 0.0
        self._render_times = []
        self._cost_tracker = {"over_budget": False}
        self._last_pod_error = last_error
        self._last_pod_error_transient = transient
        self._scale_ok = scale_ok
        self.scaled = []
        self.cook_errors = []
        # No PDG here, so the ceiling comes from the queue alone -- which is
        # the real code path when nothing has been scheduled from a node yet.
        self._work_items = {}
        for i in range(pending):
            self._dispatcher.enqueue(
                rpdispatch.TaskState(task_id="t%d" % i, work_item_id=i,
                                     command="c", env={}), now=0.0)

    def __getitem__(self, name):
        value = self._parms[name]
        return types.SimpleNamespace(evaluateInt=lambda: int(value))

    def _workCeiling(self):
        # the REAL one, so these tests exercise the counting they depend on
        return load_methods(["_workCeiling"], {})["_workCeiling"](self)

    def _scale_up(self, count):
        self.scaled.append(count)
        if not self._scale_ok:
            return 0
        # The real one registers each pod as CREATED before returning, and
        # _autoscale subtracts pods that exist but have not booted. Without
        # that here the fake would invite a second raise the real code cannot.
        for i in range(count):
            self._dispatcher.add_pod("pod%d%d" % (len(self.scaled), i), status="CREATED")
        return count

    def cookError(self, message):
        self.cook_errors.append(message)


def _autoscale_fn():
    return load_methods(
        ["_autoscale"],
        extra_globals={"time": types.SimpleNamespace(time=lambda: 1e6),
                       "_SCALE_RETRY_BACKOFF": 15.0},
    )["_autoscale"]


def test_no_work_means_no_machines():
    """The whole defect: a fully cached cook must not create a single pod."""
    sched = _ScaleScheduler(minpods=2, pending=0)

    _autoscale_fn()(sched)

    assert sched.scaled == []
    assert sched._raised_for_work is False


def test_one_item_raises_one_machine_whatever_min_pods_says():
    """Min Pods is a floor, not a quantity. Applied blind it put two RTX PRO
    4000s on a one-item cook, one at 99% and one at exactly 0%, both billed
    for the whole render."""
    sched = _ScaleScheduler(minpods=2, pending=1)

    _autoscale_fn()(sched)

    assert sched.scaled == [1]
    assert sched._raised_for_work is True


def test_min_pods_is_still_honoured_when_the_work_is_there():
    """The guarantee the previous version of this test was protecting, kept:
    cold-start autoscale would add a single pod, and asking for two and
    getting one does remove parallelism the artist is paying for -- WHEN
    there are two things to run. PDG has generated six items here while only
    one has reached the queue."""
    sched = _ScaleScheduler(minpods=2, pending=1)
    sched._work_items = {0: types.SimpleNamespace(node=_StubPdgNode("fetch", 6))}

    _autoscale_fn()(sched)

    assert sched.scaled == [2]


def test_machines_are_raised_once_not_on_every_tick():
    sched = _ScaleScheduler(minpods=2, pending=3)
    autoscale = _autoscale_fn()

    autoscale(sched)
    autoscale(sched)

    assert sched.scaled == [2]


def test_a_shortage_at_first_work_is_not_a_cook_error():
    """No machines yet is a wait (R32), and the tick keeps trying."""
    sched = _ScaleScheduler(pending=1, scale_ok=False,
                            last_error="RunPod 500: no instances", transient=True)

    _autoscale_fn()(sched)

    assert sched.cook_errors == []


def test_a_bad_key_at_first_work_fails_the_cook_there_and_then():
    """Waiting never fixes a 401, and this is the first moment we can know."""
    sched = _ScaleScheduler(pending=1, scale_ok=False,
                            last_error="RunPod 401: unauthorized", transient=False)

    _autoscale_fn()(sched)

    assert len(sched.cook_errors) == 1
    assert "not a temporary shortage" in sched.cook_errors[0]


# ---------------------------------------------------------------------------
# sync pod: stop first, delete later -- and Houdini is the only executor
# ---------------------------------------------------------------------------


class _SyncAPI:
    def __init__(self, pods):
        self.pods = pods
        self.stopped, self.terminated = [], []

    def list_pods(self, prefix=""):
        return [p for p in self.pods if p.get("name", "").startswith(prefix)]

    def stop_pod(self, pod_id):
        self.stopped.append(pod_id)

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)


def _sync_step(now, stamp="0"):
    ns = load_methods(
        ["_sync_pod_step", "_seconds_since"],
        extra_globals={
            "time": types.SimpleNamespace(time=lambda: now),
            "rppods": rpdispatch and __import__("rpfarm.pods", fromlist=["pods"]),
            "WorkerClient": lambda pid, tok: types.SimpleNamespace(
                read_file=lambda path: stamp),
            "_SYNC_LAST_USED": "/workspace/.rpfarm/sync_last_used",
        },
    )
    ns["_seconds_since"] = ns["_seconds_since"]
    return ns


def test_an_idle_running_sync_pod_is_stopped_not_deleted():
    api = _SyncAPI([{"id": "s1", "name": "rpfarm-sync-u", "desiredStatus": "RUNNING"}])
    cfg = types.SimpleNamespace(user="u")
    ns = _sync_step(now=3600.0, stamp="0")          # last used an hour ago

    ns["_sync_pod_step"](api, cfg, "tok", 15 * 60, 120 * 60, lambda m: None)

    assert api.stopped == ["s1"]
    assert api.terminated == []


def test_a_busy_running_sync_pod_is_left_alone():
    api = _SyncAPI([{"id": "s1", "name": "rpfarm-sync-u", "desiredStatus": "RUNNING"}])
    cfg = types.SimpleNamespace(user="u")
    ns = _sync_step(now=60.0, stamp="0")            # used a minute ago

    ns["_sync_pod_step"](api, cfg, "tok", 15 * 60, 120 * 60, lambda m: None)

    assert api.stopped == [] and api.terminated == []


def test_a_long_stopped_sync_pod_is_deleted():
    """Stopped is ~20x cheaper than running but not free -- RunPod bills the
    container disk at double rate while stopped."""
    api = _SyncAPI([{"id": "s1", "name": "rpfarm-sync-u", "desiredStatus": "EXITED",
                     "lastStatusChange": "2020-01-01T00:00:00Z"}])
    cfg = types.SimpleNamespace(user="u")
    ns = _sync_step(now=0.0)

    ns["_sync_pod_step"](api, cfg, "tok", 15 * 60, 120 * 60, lambda m: None)

    assert api.terminated == ["s1"]
    assert api.stopped == []


def test_a_stopped_pod_with_an_unreadable_timestamp_is_left_alone():
    """Unknown never triggers an action: doing nothing costs a little money."""
    api = _SyncAPI([{"id": "s1", "name": "rpfarm-sync-u", "desiredStatus": "EXITED",
                     "lastStatusChange": "who knows"}])
    cfg = types.SimpleNamespace(user="u")
    ns = _sync_step(now=0.0)

    ns["_sync_pod_step"](api, cfg, "tok", 15 * 60, 120 * 60, lambda m: None)

    assert api.terminated == [] and api.stopped == []


def test_another_users_sync_pod_is_never_touched():
    """rpfarm-sync-u is a prefix of rpfarm-sync-usha; the account is shared."""
    api = _SyncAPI([{"id": "mine", "name": "rpfarm-sync-u", "desiredStatus": "RUNNING"},
                    {"id": "theirs", "name": "rpfarm-sync-usha", "desiredStatus": "RUNNING"}])
    cfg = types.SimpleNamespace(user="u")
    ns = _sync_step(now=3600.0, stamp="0")

    ns["_sync_pod_step"](api, cfg, "tok", 15 * 60, 120 * 60, lambda m: None)

    assert api.stopped == ["mine"]


def test_the_watch_runs_between_cooks_not_only_at_cook_start():
    """The check used to live only in onSetupCook, which is why a pod once sat
    for hours: finish a cook, start no other, and nothing looked again."""
    src = MODULE.read_text()

    assert "def startSyncPodWatch" in src
    assert "_SYNC_WATCH_INTERVAL_S" in src
    assert "hou.ui.addEventLoopCallback" in src
    # ...and the same policy function decides in both places
    assert src.count("rppods.sync_pod_action") >= 1
    assert "self._manageSyncPod()" in src


def test_the_houdini_only_limit_is_stated_where_it_is_configured():
    """The owner chose Houdini-as-executor knowingly; the choice must not be
    silent to whoever reads the parameter later."""
    dialog = (pathlib.Path(__file__).resolve().parent.parent / "hda"
              / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler"
              / "DialogScript").read_text(encoding="utf-8")

    assert "WORKS ONLY WHILE HOUDINI IS OPEN" in dialog
    assert "only while Houdini is open" in dialog
    # and there is finally a way to finish for the day from the interface
    assert 'name    "rpfarm_killsync"' in dialog


# ---------------------------------------------------------------------------
# What the guard is allowed to decide on (2026-09-05, corrected the same day)
#
# First version: a version floor. It slept through seven commits because
# rpfarm.VERSION was never bumped -- a guard that depends on someone
# remembering a number is off whenever they forget.
#
# Second version: the package's files against the DISK. That answered the
# wrong question. Every push while an artist had Houdini open blocked their
# next cook, and a genuinely broken session -- an asset reinstalled under a
# running Houdini, which reloads definitions without reopening the scene --
# could still look fine.
#
# This version: what the asset was BUILT AGAINST against what this process
# LOADED. Both live inside the artist's Houdini, so nothing anyone does in a
# checkout can move either of them, and a mismatch means exactly one thing:
# this node was built against different code than the one in memory.
# ---------------------------------------------------------------------------


class _FakePackage:
    def __init__(self, fingerprint, version="2.3.0"):
        self.FINGERPRINT = fingerprint
        self.VERSION = version


def test_an_asset_built_against_the_loaded_code_says_nothing():
    ns = _guard()
    baked = {"deps.py": (100, "aaaa"), "preflight.py": (50, "bbbb")}

    assert ns["_asset_mismatch"](_FakePackage(dict(baked)), baked) == []
    assert ns["_stale_module_message"]("2.3.0", "2.3.0", "2.3.0", "/root", [], True) is None


def test_an_asset_built_against_other_code_is_a_hard_stop():
    ns = _guard()
    baked = {"deps.py": (100, "aaaa"), "preflight.py": (50, "bbbb")}
    loaded = {"deps.py": (140, "cccc"), "preflight.py": (50, "bbbb")}

    changed = ns["_asset_mismatch"](_FakePackage(loaded), baked)

    assert changed == ["deps.py"]
    message = ns["_stale_module_message"]("2.3.0", "2.2.0", "2.3.0", "/root", changed, True)
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" in message
    assert "deps.py" in message


def test_the_disk_takes_no_part_in_the_decision(tmp_path):
    """The correction, as a test: a checkout that moves while an artist has
    Houdini open must not block their cook. Both sides of the comparison are
    inside the session; nothing here reads a file."""
    ns = _guard()
    baked = {"deps.py": (100, "aaaa")}
    package = _FakePackage(dict(baked))

    pkg_dir = tmp_path / "rpfarm"
    pkg_dir.mkdir()
    (pkg_dir / "deps.py").write_text("a checkout that has moved on")

    assert ns["_asset_mismatch"](package, baked) == [], "the disk is not consulted"


def test_an_asset_with_no_baked_fingerprint_warns_but_does_not_stop():
    """An old node cannot be judged. Refusing to cook on "I cannot tell" is
    how a guard gets switched off for good."""
    ns = _guard()

    assert ns["_asset_mismatch"](_FakePackage({"deps.py": (1, "a")}), {}) == []
    message = ns["_stale_module_message"]("2.3.0", "2.3.0", "0", "/root", ["<no fingerprint>"], False)
    assert message is not None
    assert "ПЕРЕЗАПУСТИТЕ" not in message
    assert "ВНИМАНИЕ" in message


def test_a_package_too_old_to_carry_a_fingerprint_is_reported_the_same_way():
    ns = _guard()

    class _Ancient:
        VERSION = "2.2.0"

    changed = ns["_asset_mismatch"](_Ancient(), {"deps.py": (1, "a")})

    assert changed and "fingerprint" in changed[0]
    assert "ВНИМАНИЕ" in ns["_stale_module_message"]("2.3.0", None, "2.3.0", "/root", changed, True)


def test_the_guard_and_the_package_measure_the_same_thing():
    """Two implementations on purpose -- the package's own copy may be the
    stale one -- so something has to hold them to the same answer."""
    import rpfarm

    ns = _guard()
    package_dir = pathlib.Path(rpfarm.__file__).parent

    assert ns["_ondisk_fingerprint"](str(package_dir)) == rpfarm.fingerprint(str(package_dir))
    assert rpfarm.FINGERPRINT == rpfarm.fingerprint(), "taken at import, over the same files"


def test_the_scheduler_checks_before_it_rents_anything():
    """The owner was bitten at COOK time, in a session that was fine when the
    scene opened. A guard that only runs on scene load misses that -- and so
    does one that only runs on scene load when Houdini reloads a reinstalled
    asset without reopening the scene."""
    src = MODULE.read_text()
    setup = src.index("def onSetupCook(self):")
    guard = src.index("_stale_module_message(", setup)
    first_pod = src.index("ensure_sync_pod", setup)

    assert guard < first_pod, "the check must come before a machine is rented"
    assert "raise CookError(_stale)" in src[setup:first_pod]


# ---------------------------------------------------------------------------
# $OCIO on the farm (2026-09-05)
#
# A pod has no OCIO, so Houdini there loads the config from the volume while
# the artist works in his own. The render finishes; the colour is wrong. The
# upload ships the config directory, and this is the half that points the
# farm at it.
# ---------------------------------------------------------------------------


def test_the_task_environment_points_at_the_uploaded_colour_config(monkeypatch):
    ns = load_methods(["_ensureOcioEnv"], {
        "os": __import__("os"),
        "hou": types.SimpleNamespace(getenv=lambda name: None),
        "rppkg": rppkg,
    })
    monkeypatch.setenv("OCIO", "/Users/may/color/ocio/aces_2.0/config.ocio")
    self = types.SimpleNamespace(
        _pathmap={"/Users/may/color/ocio/aces_2.0": "/workspace/projects/may/shot/_ext/Users/may/color/ocio/aces_2.0"},
        _log=lambda m: None)
    env = {}

    ns["_ensureOcioEnv"](self, env)

    assert env["OCIO"] == (
        "/workspace/projects/may/shot/_ext/Users/may/color/ocio/aces_2.0/config.ocio")


def test_no_ocio_locally_means_no_ocio_on_the_farm(monkeypatch):
    """Both sides fall back to Houdini's own config, which is the same on
    both. Setting anything here would be inventing a difference."""
    ns = load_methods(["_ensureOcioEnv"], {
        "os": __import__("os"),
        "hou": types.SimpleNamespace(getenv=lambda name: None),
        "rppkg": rppkg,
    })
    monkeypatch.delenv("OCIO", raising=False)
    env = {}

    ns["_ensureOcioEnv"](types.SimpleNamespace(_pathmap={}, _log=lambda m: None), env)

    assert "OCIO" not in env


def test_an_unmapped_colour_config_is_reported_not_guessed(monkeypatch):
    """If the config was not part of this cook there is nothing to point at.
    Say so -- the alternative is a silent colour difference."""
    ns = load_methods(["_ensureOcioEnv"], {
        "os": __import__("os"),
        "hou": types.SimpleNamespace(getenv=lambda name: None),
        "rppkg": rppkg,
    })
    monkeypatch.setenv("OCIO", "/somewhere/else/config.ocio")
    said = []
    env = {}

    ns["_ensureOcioEnv"](types.SimpleNamespace(_pathmap={"/job": "/workspace/p"},
                                               _log=said.append), env)

    assert "OCIO" not in env
    assert any("nothing in this cook maps it" in m for m in said), said


# ---------------------------------------------------------------------------
# Houdini's own path map (cook 41f78681, 2026-09-05)
#
# $PDG_PATHMAP is applied by PDG, not by Houdini -- pdgcmd.localizePath covers
# the paths PDG resolves. $HOUDINI_PATHMAP is read by Houdini's core. Measured
# on the pod: it maps hou.findFile and makes hou.hda.installFile find an
# uploaded .hda, and USD's resolver ignores it completely (Ar.Resolve -> '').
# ---------------------------------------------------------------------------


def _pathmap_env(pathmap, mapmode=0, log=None):
    ns = load_methods(["_ensureHoudiniPathmapEnv"], {"json": __import__("json")})
    self = types.SimpleNamespace(
        _pathmap=pathmap, _log=log or (lambda m: None),
        __getitem__=None)
    self.__dict__["_parms"] = {"pdg_mapmode": mapmode}
    env = {}

    class _Self:
        def __init__(self, inner):
            self.__dict__.update(inner.__dict__)

        def __getitem__(self, name):
            return types.SimpleNamespace(evaluateInt=lambda: mapmode)

    ns["_ensureHoudiniPathmapEnv"](_Self(self), env)
    return env


def test_houdinis_own_path_map_gets_the_same_mapping():
    env = _pathmap_env({"/Users/may": "/workspace/projects/may/airship"})

    assert json.loads(env["HOUDINI_PATHMAP"]) == {
        "/Users/may": "/workspace/projects/may/airship"}


def test_path_mapping_set_to_none_is_honoured():
    """The artist turned mapping off deliberately; do not put it back."""
    assert _pathmap_env({"/Users/may": "/workspace/p"}, mapmode=1) == {}


def test_no_map_no_variable():
    assert _pathmap_env({}) == {}


def test_a_nested_root_is_not_pruned_from_houdinis_map():
    """Measured on the pod: with both targets present Houdini takes the
    LONGEST matching prefix, so a nested entry resolves its own files. Prune
    it and the shorter root takes the path instead -- silently producing a
    path inside the project mirror where nothing is. So the map goes over
    whole."""
    env = _pathmap_env({
        "/Users/may": "/workspace/projects/may/airship",
        "/Users/may/color/ocio/aces_2.0":
            "/workspace/projects/may/airship/_ext/Users/may/color/ocio/aces_2.0",
    })

    assert json.loads(env["HOUDINI_PATHMAP"]) == {
        "/Users/may": "/workspace/projects/may/airship",
        "/Users/may/color/ocio/aces_2.0":
            "/workspace/projects/may/airship/_ext/Users/may/color/ocio/aces_2.0",
    }


# ---------------------------------------------------------------------------
# Never more machines than there is work (2026-09-05)
#
# The owner's RunPod panel: two RTX PRO 4000 at $0.57/hr on a ONE-task cook,
# one pod at 99% and the other at exactly 0% for the whole render, because
# Min Pods was applied without looking at how much there was to do.
# ---------------------------------------------------------------------------


def _ceiling(pending=0, running=0, nodes=()):
    ns = load_methods(["_workCeiling"], {})
    work_items = []
    for node in nodes:
        for _ in range(1):
            work_items.append(types.SimpleNamespace(node=node))
    self = types.SimpleNamespace(
        _dispatcher=types.SimpleNamespace(
            pending=[None] * pending,
            running_tasks=lambda: [None] * running),
        _work_items={i: wi for i, wi in enumerate(work_items)})
    return ns["_workCeiling"](self)


def test_the_ceiling_counts_what_pdg_generated_not_just_the_queue():
    """On the first tick the queue can hold one item while the graph has six.
    Counting the queue alone would turn Min Pods into 1 and take away the
    parallelism the artist is paying for."""
    assert _ceiling(pending=1, nodes=[_StubPdgNode("fetch", 6)]) == 6


def test_the_ceiling_adds_up_distinct_nodes():
    assert _ceiling(pending=1, nodes=[_StubPdgNode("a", 4), _StubPdgNode("b", 4)]) == 8


def test_the_ceiling_falls_back_to_the_queue_when_pdg_says_nothing():
    """A node that cannot be read must not shrink the answer to zero."""
    class _Broken:
        @property
        def name(self):
            raise RuntimeError("gone")

    assert _ceiling(pending=3, running=2, nodes=[_Broken()]) == 5
    assert _ceiling() == 1, "never zero -- there is work, something must run it"


def test_one_task_and_min_pods_two_raises_one_machine():
    """The owner's case, end to end through the two pieces that decide it."""
    ceiling = _ceiling(pending=1, nodes=[_StubPdgNode("fetch_shot0012", 1)])

    assert ceiling == 1
    assert rpdispatch.initial_pods(2, ceiling) == 1


def test_three_tasks_and_min_pods_two_raises_two_machines():
    ceiling = _ceiling(pending=3, nodes=[_StubPdgNode("fetch_shot0012", 3)])

    assert ceiling == 3
    assert rpdispatch.initial_pods(2, ceiling) == 2


# ---------------------------------------------------------------------------
# A task that dies before it can report (2026-09-05)
#
# Cook 21bf1949: the task crashed on startup -- its RPC for the work item JSON
# answered in 192 ms with None, so pdgjson.fromString got None and raised. The
# worker never got a result to report, its /tasks/<id> answered 404, the client
# turned that into None, and the scheduler reads None as "still running". 21
# minutes later: item Cooking, pod idle at 0%, nothing in the ledger, $0.57/hr
# still running.
# ---------------------------------------------------------------------------


class _UnknownTaskClient:
    def status(self, task_id):
        return {"state": "unknown"}


def _poll_scheduler(started_ago, ledger_path):
    sched = FakeScheduler()
    sched._last_task_poll = {}
    sched._work_items = {}
    sched._cook_id = "21bf1949"
    sched._project = "airship"
    sched._cfg = types.SimpleNamespace(user="may")
    sched._ledger_path = str(ledger_path)
    sched._dispatcher.add_pod("pod1", cost_per_hr=0.57, status="RUNNING", gpu="RTX PRO 4000")
    task = rpdispatch.TaskState(task_id="t1", work_item_id=7, command="c", env={},
                                log_path="/workspace/ledger/logs/21bf1949/fetch_shot0018_6.log")
    sched._dispatcher.enqueue(task, now=0.0)
    sched._dispatcher.assign("t1", "pod1")
    sched._dispatcher.running_tasks()[0].started = time.time() - started_ago
    sched._clients["pod1"] = _UnknownTaskClient()
    return sched


def test_a_task_the_worker_never_heard_of_fails_the_item(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    sched = _poll_scheduler(started_ago=600, ledger_path=ledger)
    ns = load_methods(["_poll_tasks", "_failUnaccountedTask", "_itemName"],
                      {"time": time, "_ledger_append": _capture_ledger(ledger),
                       "_TASK_POLL_INTERVAL": 2.0, "_TASK_UNKNOWN_GRACE": 60.0})
    sched._failUnaccountedTask = lambda task: ns["_failUnaccountedTask"](sched, task)
    sched._itemName = lambda wid: str(wid)

    ns["_poll_tasks"](sched)

    assert sched._dispatcher.running_tasks() == [], "the machine is free again"
    assert [t.work_item_id for t in sched._dispatcher.failed_since_last_call()] == [7]
    assert any("has no record of task" in m for m in sched.logs), sched.logs
    assert ledger.read_text().strip(), "a cook that ends this way must leave a record"


def test_a_task_that_just_started_is_given_a_moment(tmp_path):
    """There is a real gap between dispatch and the worker registering the
    task; failing inside it would kill healthy items."""
    ledger = tmp_path / "ledger.jsonl"
    sched = _poll_scheduler(started_ago=5, ledger_path=ledger)
    ns = load_methods(["_poll_tasks", "_failUnaccountedTask", "_itemName"],
                      {"time": time, "_ledger_append": _capture_ledger(ledger),
                       "_TASK_POLL_INTERVAL": 2.0, "_TASK_UNKNOWN_GRACE": 60.0})
    sched._failUnaccountedTask = lambda task: ns["_failUnaccountedTask"](sched, task)
    sched._itemName = lambda wid: str(wid)

    ns["_poll_tasks"](sched)

    assert len(sched._dispatcher.running_tasks()) == 1, "still running, not yet judged"
    assert not ledger.exists() or not ledger.read_text().strip()


def _capture_ledger(path):
    def append(ledger_path, **row):
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    return append


def test_skipped_work_is_announced_with_a_path_to_look_at():
    """PDG not cooking an item whose outputs exist is correct behaviour and
    our silence about it is not: the owner pressed the button three times in
    one evening and each time the farm looked broken when every frame he
    asked for was already on disk."""
    import types as _types

    class _Item:
        def __init__(self, state, path):
            self.state = state
            self.outputFiles = [_types.SimpleNamespace(path=path)]
            self.expectedOutputFiles = []

    cached_state = object()
    other_state = object()
    pdg_stub = _types.SimpleNamespace(workItemState=_types.SimpleNamespace(CookedCache=cached_state))

    class _PdgNode:
        workItems = [_Item(cached_state, "/Users/may/BS/airship/render/shot0018/f.0001.exr"),
                     _Item(cached_state, "/x/b.exr"),
                     _Item(other_state, "/x/c.exr")]

    class _HouNode:
        def getPDGNode(self):
            return _PdgNode()

    sched = FakeScheduler()
    sched.topNode = lambda: _types.SimpleNamespace(
        parent=lambda: _types.SimpleNamespace(children=lambda: [_HouNode()]))

    ns = load_methods(["_announceCachedItems"], {})
    sys.modules["pdg"] = pdg_stub
    try:
        ns["_announceCachedItems"](sched)
    finally:
        sys.modules.pop("pdg", None)

    said = " ".join(sched.logs)
    assert "2 work item(s) skipped" in said, sched.logs
    assert "shot0018/f.0001.exr" in said, "and a path the artist can go and look at"


def test_nothing_is_said_when_nothing_was_skipped():
    import types as _types

    sched = FakeScheduler()
    sched.topNode = lambda: _types.SimpleNamespace(
        parent=lambda: _types.SimpleNamespace(children=lambda: []))

    load_methods(["_announceCachedItems"], {})["_announceCachedItems"](sched)

    assert not any("skipped" in m for m in sched.logs), sched.logs
    assert any("0 cached" in m for m in sched.logs), (
        "the count itself is still reported: a check that says nothing is "
        "indistinguishable from a check that did not run, which is exactly "
        "how the first version of this hid a bug in itself")


def test_a_rop_that_reports_only_its_intermediate_still_brings_the_frame_home():
    """Cook bf062eaa on the owner's scene: PDG asked the usdrender_rop for its
    default output parm and got `__render__.usd` -- the intermediate stage,
    not the picture. The frame rendered on the farm (6 MB EXR, exactly where
    the ROP asked for it), the item went CookedSuccess, and nothing came home.
    """
    import types as _types

    pathmap = {"/Users/may": "/workspace/projects/may/airship"}
    rop = _types.SimpleNamespace(
        parm=lambda name: _types.SimpleNamespace(
            evalAsStringAtFrame=lambda f:
                "/Users/may/BS/airship/render/shot0018/airship_0018_v003.acescg.%04d.exr" % f
        ) if name == "outputimage" else None)
    fetch = _types.SimpleNamespace(
        parm=lambda name: _types.SimpleNamespace(eval=lambda: "/stage/render_shot0018"))
    hou_stub = _types.SimpleNamespace(node=lambda path: rop, frame=lambda: 1.0)

    sched = FakeScheduler()
    sched._pathmap = pathmap
    sched.topNode = lambda: _types.SimpleNamespace(
        parent=lambda: _types.SimpleNamespace(node=lambda name: fetch))
    sched._ROP_OUTPUT_PARMS = ("outputimage",)

    item = _types.SimpleNamespace(
        node=_types.SimpleNamespace(name="fetch_shot0018"), frame=1.0, hasFrame=True)

    ns = load_methods(["_ropOutputPair"], {"rppkg": rppkg})
    sys.modules["hou"] = hou_stub
    try:
        farm, local = ns["_ropOutputPair"](sched, item)
    finally:
        sys.modules.pop("hou", None)

    assert local == "/Users/may/BS/airship/render/shot0018/airship_0018_v003.acescg.0001.exr"
    assert farm == ("/workspace/projects/may/airship/BS/airship/render/shot0018/"
                    "airship_0018_v003.acescg.0001.exr")
