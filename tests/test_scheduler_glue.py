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
import sys
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
         "_ondisk_fingerprint", "_changed_module_files"],
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


def test_stale_in_memory_but_fine_on_disk_says_restart_houdini():
    ns = _guard()

    message = ns["_stale_module_message"]("2.1.0", "2.0.0", "2.1.0", "/root")

    assert message is not None
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" in message
    assert "2.0.0" in message and "2.1.0" in message
    # The fix must not be confused with the other cause.
    assert "rpfarm setup" not in message


def test_an_old_package_without_a_version_at_all_is_still_caught():
    """The copy that broke predates VERSION being checked, so getattr gives
    None -- which must read as stale, not as 'no information'."""
    ns = _guard()

    message = ns["_stale_module_message"]("2.1.0", None, "2.1.0", "/root")

    assert message is not None
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" in message


def test_an_old_checkout_says_update_and_setup_not_restart():
    """Restarting cannot fix a checkout that is behind the installed asset."""
    ns = _guard()

    message = ns["_stale_module_message"]("2.1.0", "2.0.0", "2.0.0", "/root")

    assert message is not None
    assert "rpfarm setup" in message
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" not in message
    assert "/root" in message


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
        for i in range(pending):
            self._dispatcher.enqueue(
                rpdispatch.TaskState(task_id="t%d" % i, work_item_id=i,
                                     command="c", env={}), now=0.0)

    def __getitem__(self, name):
        value = self._parms[name]
        return types.SimpleNamespace(evaluateInt=lambda: int(value))

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


def test_the_first_queued_item_raises_min_pods_not_one():
    """Cold-start autoscale would add a single pod; asking for two and getting
    one silently removes the parallelism the artist is paying for."""
    sched = _ScaleScheduler(minpods=2, pending=1)

    _autoscale_fn()(sched)

    assert sched.scaled == [2]
    assert sched._raised_for_work is True


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
# The guard that slept through seven commits (2026-09-05)
#
# rpfarm.VERSION sat at 2.2.0 while deps.py, preflight.py and usddeps.py
# changed under it, so "loaded >= minimum" was true the whole time the loaded
# code was a week old -- and the owner's cook failed with two CookedFail items
# and nothing pointing at the cause. A guard that depends on someone
# remembering to bump a number is off whenever they forget. These tests are
# about the replacement: compare the FILES.
# ---------------------------------------------------------------------------


class _FakePackage:
    def __init__(self, path, fingerprint):
        self.__file__ = str(path)
        self.FINGERPRINT = fingerprint
        self.VERSION = "2.3.0"


def test_a_changed_file_is_stale_even_when_the_versions_agree(tmp_path):
    """The exact hole: same version in memory and on disk, different code."""
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()
    (pkg / "deps.py").write_text("first")
    ns = _guard()
    loaded = ns["_ondisk_fingerprint"](str(pkg))

    (pkg / "deps.py").write_text("second, longer")
    changed = ns["_changed_module_files"](_FakePackage(pkg / "__init__.py", loaded))

    assert changed == ["deps.py"]
    message = ns["_stale_module_message"]("2.3.0", "2.3.0", "2.3.0", str(tmp_path), changed)
    assert message is not None, "versions matched, the code did not"
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" in message
    assert "deps.py" in message, "and it names what changed"


def test_an_untouched_package_says_nothing(tmp_path):
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()
    (pkg / "deps.py").write_text("first")
    ns = _guard()
    loaded = ns["_ondisk_fingerprint"](str(pkg))

    assert ns["_changed_module_files"](_FakePackage(pkg / "__init__.py", loaded)) == []
    assert ns["_stale_module_message"]("2.3.0", "2.3.0", "2.3.0", str(tmp_path), []) is None


def test_a_file_restored_to_identical_content_is_not_stale(tmp_path):
    """Content, not mtime: a git checkout that puts the same bytes back must
    not send the artist to restart Houdini for nothing."""
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()
    (pkg / "deps.py").write_text("same bytes")
    ns = _guard()
    loaded = ns["_ondisk_fingerprint"](str(pkg))

    (pkg / "deps.py").write_text("same bytes")  # rewritten, new mtime

    assert ns["_changed_module_files"](_FakePackage(pkg / "__init__.py", loaded)) == []


def test_a_package_too_old_to_carry_a_fingerprint_is_stale_by_definition(tmp_path):
    pkg = tmp_path / "rpfarm"
    pkg.mkdir()
    ns = _guard()

    class _Ancient:
        __file__ = str(pkg / "__init__.py")
        VERSION = "2.2.0"

    changed = ns["_changed_module_files"](_Ancient())

    assert changed and "fingerprint" in changed[0]


def test_the_guard_and_the_package_measure_the_same_thing(tmp_path):
    """Two implementations on purpose -- the package's own copy may be the
    stale one -- so something has to hold them to the same answer."""
    import rpfarm

    ns = _guard()
    package_dir = pathlib.Path(rpfarm.__file__).parent

    assert ns["_ondisk_fingerprint"](str(package_dir)) == rpfarm.fingerprint(str(package_dir))
    assert rpfarm.FINGERPRINT == rpfarm.fingerprint(), "taken at import, over the same files"


def test_the_scheduler_checks_before_it_rents_anything():
    """The owner was bitten at COOK time, in a session that was fine when the
    scene opened. A guard that only runs on scene load misses that."""
    src = MODULE.read_text()
    setup = src.index("def onSetupCook(self):")
    guard = src.index("_stale_module_message(", setup)
    first_pod = src.index("ensure_sync_pod", setup)

    assert guard < first_pod, "the check must come before a machine is rented"
    assert "raise CookError(_stale)" in src[setup:first_pod]
