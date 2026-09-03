"""Ruling R24: the ensure_sync_pod race (two out-of-process package items
both creating a sync pod because R22 made parallel dispatch the default)
and its fix -- a per-machine file lock plus an API-side dedup as a second
line of defense.
"""

import threading
import time

import pytest

from rpfarm import pods
from tests.test_pods import FakeAPI, FakeClient, cfg


class SlowCreateFakeAPI(FakeAPI):
    """create_cpu_pod blocks on release_create until told to proceed, and
    signals create_started the moment it's entered -- lets a test force
    two threads to overlap deterministically instead of hoping the GIL
    schedules them unluckily.
    """

    def __init__(self):
        super().__init__()
        self._next_id = 0
        self.create_started = threading.Event()
        self.release_create = threading.Event()
        self.concurrent_creates = 0
        self.max_concurrent_creates = 0
        self._concurrency_lock = threading.Lock()

    def create_cpu_pod(self, name, template_id, volume_id, env, ports, **kw):
        with self._concurrency_lock:
            self.concurrent_creates += 1
            self.max_concurrent_creates = max(self.max_concurrent_creates, self.concurrent_creates)
        self.create_started.set()
        self.release_create.wait(timeout=5)
        self._next_id += 1
        pid = "sync{}".format(self._next_id)
        p = {
            "id": pid,
            "name": name,
            "desiredStatus": "RUNNING",
            "publicIp": "9.9.9.9",
            "portMappings": {"22": 1022, "4440": 14440, "4442": 14442, "8000": 18000},
        }
        self.pods[pid] = p
        self.created.append(env)
        with self._concurrency_lock:
            self.concurrent_creates -= 1
        return p


def test_ensure_sync_pod_race_produces_exactly_one_create(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "rpfarm_home"))
    api = SlowCreateFakeAPI()
    results, errors = [], []

    def run():
        try:
            p = pods.ensure_sync_pod(
                api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None
            )
            results.append(p)
        except Exception as e:  # pragma: no cover - surfaced via `errors`
            errors.append(e)

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)

    t1.start()
    assert api.create_started.wait(timeout=5), "t1 never reached create_cpu_pod"
    # t1 is now inside create_cpu_pod, blocked on release_create -- and,
    # under the fix, still holding the sync-pod lock. Start t2 and give it
    # a real chance to run: without the lock it would race straight into
    # its own list_pods -> not found -> create_cpu_pod, which the
    # concurrency counter below would catch.
    t2.start()
    time.sleep(0.3)

    api.release_create.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, errors
    assert len(results) == 2
    assert results[0]["id"] == results[1]["id"], "both callers must end up on the same pod"
    assert len(api.created) == 1, "exactly one create_cpu_pod call, not one per racing caller"
    assert api.max_concurrent_creates == 1, "create_cpu_pod must never run concurrently for the same user"


def test_ensure_sync_pod_dedups_existing_duplicate_running_pods(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "rpfarm_home"))
    api = FakeAPI()
    name = pods.sync_pod_name("may")
    # Simulates what a previous crashed/raced run left behind: two RUNNING
    # pods with the same sync-pod name. The older one (by createdAt) must
    # be kept; the newer one terminated.
    api.pods["dup_new"] = {
        "id": "dup_new",
        "name": name,
        "desiredStatus": "RUNNING",
        "createdAt": "2026-09-03 15:00:00.000 +0000 UTC",
        "publicIp": "9.9.9.9",
        "portMappings": {"22": 1, "4440": 2, "4442": 3, "8000": 4},
    }
    api.pods["dup_old"] = {
        "id": "dup_old",
        "name": name,
        "desiredStatus": "RUNNING",
        "createdAt": "2026-09-03 14:00:00.000 +0000 UTC",
        "publicIp": "9.9.9.9",
        "portMappings": {"22": 1, "4440": 2, "4442": 3, "8000": 4},
    }

    p = pods.ensure_sync_pod(
        api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None
    )

    assert p["id"] == "dup_old"
    assert api.terminated == ["dup_new"]
    assert set(api.pods) == {"dup_old"}
    assert api.created == []  # no new pod needed -- one of the duplicates was adopted


def test_ensure_sync_pod_dedup_falls_back_to_lowest_id_without_createdat(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "rpfarm_home"))
    api = FakeAPI()
    name = pods.sync_pod_name("may")
    for pid in ("zzz", "aaa"):
        api.pods[pid] = {
            "id": pid,
            "name": name,
            "desiredStatus": "RUNNING",
            "publicIp": "9.9.9.9",
            "portMappings": {"22": 1, "4440": 2, "4442": 3, "8000": 4},
        }

    p = pods.ensure_sync_pod(
        api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None
    )
    assert p["id"] == "aaa"
    assert api.terminated == ["zzz"]


def test_file_lock_creates_parent_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "rpfarm_home"))
    path = pods._sync_pod_lock_path()
    assert path.parent.is_dir()
    assert path.parent.name == "locks"


def test_file_lock_raises_timeout_error_when_never_acquirable(tmp_path, monkeypatch):
    lock_path = tmp_path / "held.lock"

    def never_acquire(*a, **kw):
        raise OSError("simulated: always busy")

    monkeypatch.setattr(pods, "_try_acquire", never_acquire)
    with pytest.raises(TimeoutError):
        with pods._file_lock(lock_path, timeout=0.2, poll=0.05):
            pass  # pragma: no cover - never reached


def test_ensure_sync_pod_does_not_deadlock_on_a_stale_lock(tmp_path, monkeypatch):
    """A lock that can never be acquired (simulating a stale lock whose
    holder is gone but the low-level primitive still reports busy for some
    reason) must not hang ensure_sync_pod forever -- it should fall back
    to re-checking for an existing pod instead of blocking indefinitely.
    """
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "rpfarm_home"))
    monkeypatch.setattr(pods, "_SYNC_POD_LOCK_TIMEOUT_S", 0.2)

    def never_acquire(*a, **kw):
        raise OSError("simulated: always busy")

    monkeypatch.setattr(pods, "_try_acquire", never_acquire)

    api = FakeAPI()
    started = time.time()
    p = pods.ensure_sync_pod(
        api, cfg(), "tok", "ssh-ed25519 AAA", client_factory=lambda pid: FakeClient(), sleep=lambda s: None
    )
    elapsed = time.time() - started
    assert p["id"] == "sync1"
    assert elapsed < 5, "must not block indefinitely on an unacquirable lock"
