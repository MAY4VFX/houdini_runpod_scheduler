import contextlib

import pytest

from rpfarm import config as rpcfg
from rpfarm import pods
from rpfarm import pods as rppods


class FakeAPI:
    def __init__(self):
        self.created, self.pods, self.terminated = [], {}, []
        self.create_kwargs = []

    def list_pods(self, prefix=""):
        return [p for p in self.pods.values() if p["name"].startswith(prefix)]

    def create_cpu_pod(self, name, template_id, volume_id, env, ports, **kw):
        p = {
            "id": "sync1",
            "name": name,
            "desiredStatus": "RUNNING",
            "publicIp": "9.9.9.9",
            "portMappings": {"22": 1022, "4440": 14440, "4442": 14442, "8000": 18000},
        }
        self.pods[p["id"]] = p
        self.created.append(env)
        self.create_kwargs.append(kw)
        return p

    def get_pod(self, pid):
        return self.pods[pid]

    def terminate_pod(self, pid):
        self.terminated.append(pid)
        self.pods.pop(pid, None)


class FakeClient:
    def __init__(self):
        self.execs, self.files = [], {}

    def health(self):
        return {"role": "sync"}

    def exec(self, command, timeout_s=600):
        self.execs.append(command)
        self.files["/workspace/.rpfarm/mq_c1.txt"] = (
            "PDG_MQ 10.0.0.5 4440 4440 4442\n## Message Queue Server Running\n"
        )
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def read_file(self, path):
        return self.files.get(path)


def cfg():
    from rpfarm.config import Config

    return Config(api_key="k", user="may", volume_id="v", template_id="t", gpu_priority=["g"])


def test_ensure_sync_pod_creates_once(tmp_path, monkeypatch):
    # ensure_sync_pod takes a file lock under $RPFARM_HOME/locks (Ruling
    # R24) -- point it at a tmp dir so tests never touch the real
    # ~/.rpfarm, matching the pattern test_config.py already uses.
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = FakeAPI()
    p1 = pods.ensure_sync_pod(api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None)
    p2 = pods.ensure_sync_pod(api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None)
    assert p1["id"] == p2["id"] == "sync1" and len(api.created) == 1
    assert api.created[0]["RPFARM_ROLE"] == "sync" and api.created[0]["PUBLIC_KEY"].startswith("ssh-ed25519")


def test_ensure_sync_pod_terminates_non_running_and_recreates(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = FakeAPI()
    api.pods["old1"] = {
        "id": "old1",
        "name": pods.sync_pod_name("may"),
        "desiredStatus": "EXITED",
        "publicIp": None,
        "portMappings": {},
    }
    p = pods.ensure_sync_pod(api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None)
    assert "old1" not in api.pods
    assert api.terminated == ["old1"]
    assert p["id"] == "sync1"
    assert len(api.created) == 1


def test_ensure_sync_pod_does_not_adopt_prefix_match(tmp_path, monkeypatch):
    # list_pods is a prefix match; sync_pod_name("may") == "rpfarm-sync-may"
    # is a prefix of another user's "rpfarm-sync-mayakovsky" pod. That other
    # user's RUNNING pod must never be adopted as "may"'s sync pod.
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = FakeAPI()
    api.pods["alien1"] = {
        "id": "alien1",
        "name": "rpfarm-sync-mayakovsky",
        "desiredStatus": "RUNNING",
        "publicIp": "1.1.1.1",
        "portMappings": {"22": 1000, "8000": 2000},
    }
    p = pods.ensure_sync_pod(api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None)
    assert p["id"] == "sync1"
    assert len(api.created) == 1
    assert "alien1" in api.pods
    assert api.terminated == []


def test_sync_pod_is_created_in_the_configured_datacenter(tmp_path, monkeypatch):
    """Finding 5: the sync pod mounts the network volume, and a pod can only
    mount a volume in its own region -- so the region has to come from the
    config, not from RunPodAPI's fallback."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    from rpfarm.config import Config

    c = Config(api_key="k", user="may", volume_id="v", template_id="t",
               gpu_priority=["g"], datacenter="US-KS-2")
    api = FakeAPI()
    pods.ensure_sync_pod(api, c, "tok", "ssh-ed25519 AAA",
                         client_factory=lambda pid: FakeClient(), sleep=lambda s: None)
    assert [kw.get("datacenter") for kw in api.create_kwargs] == ["US-KS-2"]


def test_start_mq_rewrites_public_address():
    c = FakeClient()
    pod = FakeAPI().create_cpu_pod("rpfarm-sync-may", "t", "v", {}, [])
    line = pods.start_mq(c, pod, "c1", sleep=lambda s: None)
    assert line == "PDG_MQ 9.9.9.9 14440 14440 14442"
    assert "mqserver -p 4440" in c.execs[0] and "-w 4442" in c.execs[0]


def test_names():
    assert pods.pod_name("may", "shot010", "abcd1234", 2) == "rpfarm-may-shot010-abcd1234-2"
    assert pods.sync_pod_name("may") == "rpfarm-sync-may"


def test_pod_env_contents():
    c = cfg()
    env = pods.pod_env(c, "gpu", "tok123", 2, "ssh-ed25519 AAA", extra={"NODE_ID": "n1"})
    assert env == {
        "RPFARM_TOKEN": "tok123",
        "RPFARM_ROLE": "gpu",
        "RPFARM_SLOTS": "2",
        # identity, readable back out of GET /pods -- see classify_for_kill
        "RPFARM_USER": c.user,
        "RPFARM_COOK": "",
        "RPFARM_PROJECT": "",
        "PUBLIC_KEY": "ssh-ed25519 AAA",
        "HOUDINI_VERSION": c.houdini_version,
        "SESINETD_HOST": c.sesinetd_host,
        "SESINETD_PORT": str(c.sesinetd_port),
        "NODE_ID": "n1",
    }
    assert "NVIDIA" not in "".join(env.keys())


def test_pod_env_without_extra():
    c = cfg()
    env = pods.pod_env(c, "sync", "tok", 4, "ssh-ed25519 AAA")
    assert set(env.keys()) == {
        "RPFARM_TOKEN",
        "RPFARM_ROLE",
        "RPFARM_SLOTS",
        "RPFARM_USER",
        "RPFARM_COOK",
        "RPFARM_PROJECT",
        "PUBLIC_KEY",
        "HOUDINI_VERSION",
        "SESINETD_HOST",
        "SESINETD_PORT",
    }


def test_wait_ready_returns_pod_when_healthy():
    api = FakeAPI()
    pod = api.create_cpu_pod("rpfarm-x", "t", "v", {}, [])
    result = pods.wait_ready(api, FakeClient(), pod["id"], timeout=10, sleep=lambda s: None)
    assert result["id"] == "sync1"


def test_wait_ready_timeout_raises():
    api = FakeAPI()
    pod = api.create_cpu_pod("rpfarm-x", "t", "v", {}, [])

    class NeverHealthyClient:
        def health(self):
            return None

    with pytest.raises(TimeoutError):
        pods.wait_ready(api, NeverHealthyClient(), pod["id"], timeout=0.05, sleep=lambda s: None)


def test_wait_ready_honors_cancel():
    api = FakeAPI()
    pod = api.create_cpu_pod("rpfarm-x", "t", "v", {}, [])
    with pytest.raises(RuntimeError, match="canceled"):
        pods.wait_ready(api, FakeClient(), pod["id"], timeout=10, sleep=lambda s: None, cancel=lambda: True)


def test_find_orphans_excludes_sync_pod():
    api = FakeAPI()
    api.pods["gpu1"] = {"id": "gpu1", "name": "rpfarm-may-shot010-abcd1234-1", "desiredStatus": "RUNNING"}
    api.pods["gpu2"] = {"id": "gpu2", "name": "rpfarm-may-shot010-abcd1234-2", "desiredStatus": "RUNNING"}
    api.pods["sync1"] = {"id": "sync1", "name": "rpfarm-sync-may", "desiredStatus": "RUNNING"}
    api.pods["other"] = {"id": "other", "name": "rpfarm-bob-shot-xxxxxxxx-1", "desiredStatus": "RUNNING"}
    orphans = pods.find_orphans(api, "may")
    assert {p["id"] for p in orphans} == {"gpu1", "gpu2"}


def test_find_orphans_filters_to_running_only():
    api = FakeAPI()
    api.pods["gpu1"] = {"id": "gpu1", "name": "rpfarm-may-shot010-abcd1234-1", "desiredStatus": "RUNNING"}
    api.pods["gpu2"] = {"id": "gpu2", "name": "rpfarm-may-shot010-abcd1234-2", "desiredStatus": "EXITED"}
    orphans = pods.find_orphans(api, "may")
    assert {p["id"] for p in orphans} == {"gpu1"}


def test_terminate_all_calls_terminate_pod_for_each():
    api = FakeAPI()
    api.pods["a"] = {"id": "a", "name": "rpfarm-may-x-1"}
    api.pods["b"] = {"id": "b", "name": "rpfarm-may-x-2"}
    pods.terminate_all(api, ["a", "b"])
    assert api.terminated == ["a", "b"]
    assert api.pods == {}


def test_stop_mq_kills_mqserver():
    c = FakeClient()
    pods.stop_mq(c)
    assert "pkill -f mqserver" in c.execs[0]


# ---------------------------------------------------------------------------
# ensure_sync_pod waits out a shortage of CPU machines (Ruling R32)
#
# RunPod had no CPU capacity in EU-RO-1 for ~2.5 minutes and killed two cooks
# at second zero with a raw 500. The sync pod comes up before anything else
# and nothing starts without it, so this is the more damaging of the two
# capacity paths -- and it had no retry at all.
# ---------------------------------------------------------------------------


class _CapacityAPI:
    """create_cpu_pod fails `failures` times, then succeeds."""

    def __init__(self, failures, status=500, body="no longer any instances available"):
        self.failures = failures
        self.status = status
        self.body = body
        self.creates = 0
        self.bodies = []

    def list_pods(self, prefix=""):
        return []

    def terminate_pod(self, pod_id):
        pass

    def create_cpu_pod(self, name, template_id, volume_id, env, ports,
                       vcpu=2, flavors=("cpu3c", "cpu5c"),
                       datacenter=None, cloud_type=None):
        self.creates += 1
        self.bodies.append({"cloud_type": cloud_type, "datacenter": datacenter})
        if self.creates <= self.failures:
            raise rppods.RunPodError(self.status, self.body)
        return {"id": "sync1", "name": name, "desiredStatus": "RUNNING"}


def _cfg_for_capacity(tmp_path, wait_min=15, cloud="SECURE"):
    cfg = rpcfg.Config(api_key="k", user="u", volume_id="v", template_id="t")
    cfg.datacenter = "EU-RO-1"
    cfg.capacity_wait_min = wait_min
    cfg.cloud_type = cloud
    cfg.ssh_key_path = str(tmp_path / "id")
    return cfg


def _acquire(api, cfg, clock, sleeps, **kw):
    return rppods._acquire_sync_pod(
        api, cfg, "token", "pub", log=lambda m: None,
        cloud_type=kw.get("cloud_type", cfg.cloud_type),
        capacity_wait_s=kw.get("capacity_wait_s", cfg.capacity_wait_min * 60),
        sleep=sleeps.append, cancel=kw.get("cancel", lambda: False),
        clock=clock, rand=lambda a, b: 1.0,
    )


def test_a_shortage_is_waited_out_and_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=3)
    cfg = _cfg_for_capacity(tmp_path)
    ticks = iter([0, 0, 10, 30, 60])   # deadline, started, one per refusal
    sleeps = []

    pod = _acquire(api, cfg, lambda: next(ticks), sleeps)

    assert pod["id"] == "sync1"
    assert api.creates == 4          # three refusals, then a machine
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps)  # backoff grows


def test_the_wait_gives_up_with_a_message_a_person_can_act_on(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=99)
    cfg = _cfg_for_capacity(tmp_path, wait_min=1)
    ticks = iter([0, 0, 61])
    sleeps = []

    with pytest.raises(rppods.SyncPodCapacityError) as excinfo:
        _acquire(api, cfg, lambda: next(ticks), sleeps)

    message = str(excinfo.value)
    assert "EU-RO-1" in message
    assert "try again shortly" in message              # the actionable way out
    # No blanket "switch to Community": in EU-RO-1 that cloud has 0 of 48
    # types priced, so the advice would send someone to wait for nothing.
    assert "Community" not in message
    assert "no longer any instances" in message        # RunPod's own last word
    assert "1m 01s" in message or "61s" in message     # how long it actually waited


def test_a_4xx_is_never_waited_on(tmp_path, monkeypatch):
    """No amount of waiting fixes a bad key or a missing template."""
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=99, status=401, body="unauthorized")
    cfg = _cfg_for_capacity(tmp_path)
    sleeps = []

    with pytest.raises(rppods.RunPodError):
        _acquire(api, cfg, lambda: 0, sleeps)

    assert api.creates == 1
    assert sleeps == []


def test_zero_wait_means_wait_forever_like_the_parm_says(tmp_path, monkeypatch):
    """Same meaning as Wait For Capacity 0 on the GPU side: no deadline."""
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=5)
    cfg = _cfg_for_capacity(tmp_path, wait_min=0)
    sleeps = []

    pod = _acquire(api, cfg, lambda: 0, sleeps, capacity_wait_s=0)

    assert pod["id"] == "sync1"
    assert api.creates == 6      # kept waiting well past any 15-minute budget
    assert len(sleeps) == 5


def test_cloud_type_reaches_the_sync_pod_create(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=0)
    cfg = _cfg_for_capacity(tmp_path, cloud="COMMUNITY")

    _acquire(api, cfg, lambda: 0, [])

    assert api.bodies[0]["cloud_type"] == "COMMUNITY"
    assert api.bodies[0]["datacenter"] == "EU-RO-1"


def test_cancelling_stops_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _CapacityAPI(failures=99)
    cfg = _cfg_for_capacity(tmp_path)

    with pytest.raises(rppods.SyncPodCapacityError, match="cancelled"):
        _acquire(api, cfg, lambda: 0, [], cancel=lambda: True)

    assert api.creates == 0


def test_backoff_never_overshoots_the_remaining_deadline():
    assert rppods._capacity_backoff(1, remaining=3.0, rand=lambda a, b: 1.0) == 3.0
    assert rppods._capacity_backoff(9, remaining=None, rand=lambda a, b: 1.0) == 60.0
    assert rppods._capacity_backoff(1, remaining=None, rand=lambda a, b: 1.0) == 10.0


@contextlib.contextmanager
def _nolock(*_args, **_kwargs):
    yield


# ---------------------------------------------------------------------------
# who owns a pod, and is its cook alive
#
# The account is shared. An agent that could only see its own state assumed
# every running pod was its own leftover and terminated a colleague's -- 36
# seconds after their cook happened to finish. A minute earlier and it would
# have destroyed a live render. These pin the classification that makes the
# default incapable of it.
# ---------------------------------------------------------------------------


def test_pod_owner_prefers_the_env_it_was_created_with():
    pod = {"name": "rpfarm-may-demo-abc12345-1",
           "env": {"RPFARM_USER": "bob", "RPFARM_COOK": "abc12345"}}

    assert rppods.pod_owner(pod) == "bob"
    assert rppods.pod_cook(pod) == "abc12345"


def test_pod_owner_falls_back_to_the_name_for_pods_created_before_the_env():
    assert rppods.pod_owner({"name": "rpfarm-may-demo-abc-1"}) == "may"
    assert rppods.pod_owner({"name": "rpfarm-sync-may"}) == "may"
    assert rppods.pod_owner({"name": "something-else"}) == ""


def test_an_unknown_owner_is_not_silently_mine():
    """'I cannot tell' must never resolve to 'mine' -- that is the whole bug."""
    pod = {"name": "something-else"}

    # No owner and no health: unreachable wins, and it is still not killed.
    verdict, _reason = rppods.classify_for_kill(pod, "may", None, "timeout")
    assert verdict == "unknown"


def test_an_idle_pod_of_mine_is_safe():
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}

    verdict, reason = rppods.classify_for_kill(
        pod, "may", {"busy": 0, "idle_s": 900, "ssh_sessions": 0, "transfers": 0})

    assert verdict == "safe"
    assert "900" in reason


def test_a_pod_running_a_task_is_busy():
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}

    verdict, reason = rppods.classify_for_kill(
        pod, "may", {"busy": 2, "idle_s": 5000})

    assert verdict == "busy"
    assert "2 task(s)" in reason


def test_a_pod_spoken_to_seconds_ago_is_busy_even_with_no_task_running():
    """The exact 36-second window that made the real incident survivable by
    luck: between two tasks of a live cook `busy` dips to zero."""
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}

    verdict, reason = rppods.classify_for_kill(
        pod, "may", {"busy": 0, "idle_s": 36, "ssh_sessions": 0, "transfers": 0})

    assert verdict == "busy"
    assert "36s ago" in reason


def test_the_grace_boundary_is_configurable_and_exclusive():
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}
    health = {"busy": 0, "idle_s": 100, "ssh_sessions": 0, "transfers": 0}

    assert rppods.classify_for_kill(pod, "may", health, grace_s=100)[0] == "safe"
    assert rppods.classify_for_kill(pod, "may", health, grace_s=101)[0] == "busy"


def test_someone_elses_pod_is_foreign_even_when_idle():
    """Ownership is checked before liveness: another artist's idle pod is
    still not ours to take."""
    pod = {"name": "rpfarm-bob-demo-a-1", "env": {"RPFARM_USER": "bob"}}

    verdict, reason = rppods.classify_for_kill(
        pod, "may", {"busy": 0, "idle_s": 9999, "ssh_sessions": 0, "transfers": 0})

    assert verdict == "foreign"
    assert "bob" in reason


def test_a_pod_that_cannot_be_reached_is_left_alone():
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}

    assert rppods.classify_for_kill(pod, "may", None, "connection refused")[0] == "unknown"
    assert rppods.classify_for_kill(pod, "may", {"busy": 0})[0] == "unknown"
    # reports the channels but not how long: still not clearable
    assert rppods.classify_for_kill(
        pod, "may", {"busy": 0, "ssh_sessions": 0, "transfers": 0})[0] == "unknown"


def test_pod_env_carries_identity_readable_from_get_pods(tmp_path):
    cfg = rpcfg.Config(api_key="k", user="may", volume_id="v", template_id="t")
    cfg.ssh_key_path = str(tmp_path / "id")

    env = rppods.pod_env(cfg, "gpu", "tok", 1, "ssh-ed25519 AAA",
                         cook="abc12345", project="demo")

    assert env["RPFARM_USER"] == "may"
    assert env["RPFARM_COOK"] == "abc12345"
    assert env["RPFARM_PROJECT"] == "demo"


# ---------------------------------------------------------------------------
# a pod that vanishes while we wait for it
#
# Real incident: a sync pod was terminated out from under an in-flight
# ensure_sync_pod, and `RunPod 404 pod not found` came out of wait_ready and
# failed a download work item. A pod can disappear legitimately -- host
# failure, a manual delete, someone else's kill -- so this is the same family
# as R32: get another machine, do not fail the item.
# ---------------------------------------------------------------------------


class _VanishingAPI(_CapacityAPI):
    """get_pod 404s `vanish_times` times, then answers normally."""

    def __init__(self, vanish_times=1):
        super().__init__(failures=0)
        self.vanish_times = vanish_times
        self.get_calls = 0

    def get_pod(self, pod_id):
        self.get_calls += 1
        if self.get_calls <= self.vanish_times:
            raise rppods.RunPodError(404, "pod not found")
        return {"id": pod_id, "name": "rpfarm-sync-u", "desiredStatus": "RUNNING",
                "portMappings": {"22": 12345}, "publicIp": "1.2.3.4"}


def test_wait_ready_reports_a_vanished_pod_as_its_own_kind_of_problem():
    api = _VanishingAPI()

    with pytest.raises(rppods.PodGoneError, match="disappeared"):
        rppods.wait_ready(api, _HealthyClient(), "sync1", timeout=30, sleep=lambda _s: None)


def test_wait_ready_still_raises_an_auth_failure_untouched():
    """403 is not about this pod and waiting never fixes it."""

    class _Forbidden(_CapacityAPI):
        def get_pod(self, pod_id):
            raise rppods.RunPodError(403, "forbidden")

    with pytest.raises(rppods.RunPodError):
        rppods.wait_ready(_Forbidden(0), _HealthyClient(), "sync1", timeout=30,
                          sleep=lambda _s: None)


def test_ensure_sync_pod_finds_another_when_the_first_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _VanishingAPI(vanish_times=1)
    cfg = _cfg_for_capacity(tmp_path)

    pod = rppods.ensure_sync_pod(
        api, cfg, "token", "pub", log=lambda m: None,
        client_factory=lambda _pid: _HealthyClient(),
        sleep=lambda _s: None, timeout=30, clock=lambda: 0.0,
        rand=lambda a, b: 1.0)

    assert pod["id"] == "sync1"
    assert api.creates == 2          # the vanished one, then a replacement


def test_a_pod_that_keeps_vanishing_past_the_deadline_gives_up_readably(tmp_path, monkeypatch):
    monkeypatch.setattr(rppods, "_file_lock", _nolock)
    api = _VanishingAPI(vanish_times=99)
    cfg = _cfg_for_capacity(tmp_path, wait_min=1)
    ticks = iter([0, 0, 0, 0, 61, 61, 61, 61])

    with pytest.raises(rppods.SyncPodCapacityError) as excinfo:
        rppods.ensure_sync_pod(
            api, cfg, "token", "pub", log=lambda m: None,
            client_factory=lambda _pid: _HealthyClient(),
            sleep=lambda _s: None, timeout=30,
            clock=lambda: next(ticks), rand=lambda a, b: 1.0)

    assert "kept disappearing" in str(excinfo.value)


class _HealthyClient:
    def health(self):
        return {"busy": 0, "idle_s": 9999}


# ---------------------------------------------------------------------------
# liveness must not depend on which channel the work arrived by
#
# /health only ever counted HTTP. A pod driven over SSH -- or a sync pod
# receiving a 4GB Houdini tarball over SFTP, which is the heaviest thing this
# farm ever does -- reports busy 0 and a climbing idle_s while working flat
# out. The guard below then calls it safe to kill. That is how a benchmark pod
# rendering 15 frames read as abandoned, and it is how the idle watchdog would
# terminate a transfer at its midpoint.
# ---------------------------------------------------------------------------


def test_a_pod_driven_over_ssh_is_busy_not_safe():
    """The exact case: no HTTP for 24 minutes, and rendering the whole time."""
    pod = {"name": "rpfarm-may-perframe-0", "env": {"RPFARM_USER": "may"}}
    health = {"busy": 0, "idle_s": 1440, "ssh_sessions": 1, "transfers": 0}

    verdict, reason = rppods.classify_for_kill(pod, "may", health)

    assert verdict == "busy"
    assert "ssh" in reason.lower()


def test_a_sync_pod_receiving_a_tarball_is_busy():
    """rclone arrives over SFTP; the worker never sees a request."""
    pod = {"name": "rpfarm-sync-may", "env": {"RPFARM_USER": "may"}}
    health = {"busy": 0, "idle_s": 3600, "ssh_sessions": 1, "transfers": 2}

    verdict, reason = rppods.classify_for_kill(pod, "may", health)

    assert verdict == "busy"
    assert "transfer" in reason.lower()


def test_a_genuinely_empty_pod_is_still_safe():
    """Erring toward busy must not mean never killing anything."""
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}
    health = {"busy": 0, "idle_s": 9999, "ssh_sessions": 0, "transfers": 0}

    assert rppods.classify_for_kill(pod, "may", health)[0] == "safe"


def test_an_old_pod_that_cannot_report_the_new_fields_is_not_assumed_idle():
    """A pod from before this shipped answers /health without them. Absent
    evidence is not evidence of absence, and the asymmetry is the whole point:
    a false busy costs cents, a false idle costs someone's render."""
    pod = {"name": "rpfarm-may-demo-a-1", "env": {"RPFARM_USER": "may"}}
    health = {"busy": 0, "idle_s": 9999}          # no ssh_sessions, no transfers

    verdict, reason = rppods.classify_for_kill(pod, "may", health)

    assert verdict == "unknown"
    assert "cannot" in reason.lower() or "did not" in reason.lower()
