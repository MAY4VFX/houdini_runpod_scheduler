import pytest

from rpfarm import pods


class FakeAPI:
    def __init__(self):
        self.created, self.pods, self.terminated = [], {}, []

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
