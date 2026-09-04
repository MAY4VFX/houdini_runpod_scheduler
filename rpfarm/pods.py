"""Pod lifecycle: naming, env, create/wait/terminate, the CPU "sync pod",
and starting PDG's ``mqserver`` on it.

Lifts the ``_scale_up`` / ``_wait_for_pods_ready`` / ``_terminate_pod``
logic out of the v1 HDA into a ``pdg``-free module. Stdlib only (see
:mod:`rpfarm.runpod_api` for why).

Ruling R24: :func:`ensure_sync_pod`'s check-then-create is guarded by a
file lock (:func:`_file_lock`) so two callers racing it -- the normal case
once Ruling R22 made ``runpodfarm_upload`` dispatch out of process, where
several package items can each call this within the same second -- adopt
one pod instead of creating two. The lock is per-machine only (an
``fcntl``/``msvcrt`` advisory lock under ``$RPFARM_HOME/locks``, stdlib
only): two different workstations sharing one RunPod account can still
race each other. That case, and any duplicate a crashed previous run left
behind, is covered by a second, independent check inside the lock (or,
if the lock itself can't be acquired before its timeout, without it) --
if more than one exact-name ``RUNNING`` sync pod exists, the oldest is
kept and the rest are terminated.
"""

from __future__ import annotations

import contextlib
import platform
import time

from . import config as rpcfg
from .runpod_api import (
    CLOUD_TYPE_SECURE,
    RunPodError,
    is_capacity_error,
    pod_public_endpoint,
)
from .worker_client import WorkerClient

PORTS = ["22/tcp", "4440/tcp", "4442/tcp", "8000/http"]

SYNC_SLOTS = 4

# How long ensure_sync_pod waits to acquire the sync-pod lock before
# giving up on it and proceeding unlocked (best effort) -- matches
# wait_ready's own default pod-boot timeout, since a caller already
# willing to wait that long for a pod to boot can wait that long for
# another caller to finish creating one.
_SYNC_POD_LOCK_TIMEOUT_S = 300


# -- naming / env -----------------------------------------------------------


def pod_name(user, project, cook8, n):
    return f"rpfarm-{user}-{project}-{cook8}-{n}"


def sync_pod_name(user):
    return f"rpfarm-sync-{user}"


def pod_env(cfg, role, token, slots, pubkey, extra=None):
    env = {
        "RPFARM_TOKEN": token,
        "RPFARM_ROLE": role,
        "RPFARM_SLOTS": str(slots),
        "PUBLIC_KEY": pubkey,
        "HOUDINI_VERSION": cfg.houdini_version,
        "SESINETD_HOST": cfg.sesinetd_host,
        "SESINETD_PORT": str(cfg.sesinetd_port),
    }
    if extra:
        env.update(extra)
    return env


# -- readiness ----------------------------------------------------------


def wait_ready(api, client, pod_id, timeout=300, cancel=lambda: False, sleep=time.sleep, log=print):
    """Poll ``get_pod`` until the pod has a public port 22 mapping *and* its
    8000/http proxy answers ``health()``. Returns the pod dict, or raises
    ``TimeoutError`` after ``timeout`` seconds / ``RuntimeError("canceled")``
    if ``cancel()`` returns true."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel():
            raise RuntimeError("canceled")
        pod = api.get_pod(pod_id)
        try:
            pod_public_endpoint(pod, 22)
            has_ssh_port = True
        except RunPodError:
            has_ssh_port = False
        if has_ssh_port and client.health():
            return pod
        sleep(3)
    raise TimeoutError(f"pod {pod_id} not ready in {timeout}s")


# -- per-machine lock (Ruling R24) ---------------------------------------


def _sync_pod_lock_path():
    d = rpcfg.home() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / "sync-pod.lock"


def _try_acquire(fileobj):
    """Attempt a non-blocking exclusive lock on ``fileobj``; raise OSError
    if it's already held. Split out from :func:`_file_lock` so tests can
    monkeypatch just this half (simulating "always busy") without faking
    an entire platform's locking API.
    """
    if platform.system() == "Windows":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fileobj):
    if platform.system() == "Windows":
        import msvcrt

        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _file_lock(path, timeout=_SYNC_POD_LOCK_TIMEOUT_S, poll=0.2, sleep=time.sleep):
    """Exclusive, cross-platform, stdlib-only advisory file lock.

    Blocks (polling, not natively -- portable across ``fcntl``/``msvcrt``
    without signals) until acquired or ``timeout`` seconds pass, in which
    case it raises :class:`TimeoutError`. Never deadlocks on a crashed
    holder: both ``fcntl.flock`` and ``msvcrt.locking`` are tied to the
    holding process's open file descriptor, which the OS releases the
    moment that process exits or dies -- there is no lock *content* (a
    stale pid, say) that could make a lock look held after its owner is
    gone, unlike a plain "does this file exist" lock.
    """
    f = open(path, "a+")
    try:
        deadline = time.time() + timeout
        while True:
            try:
                _try_acquire(f)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"could not acquire lock {path} in {timeout}s")
                sleep(poll)
        try:
            yield
        finally:
            _release(f)
    finally:
        f.close()


# -- sync pod -----------------------------------------------------------

# Backoff between attempts to create the sync pod while RunPod has no CPU
# machines. Jittered so several waiters on one account do not all ask again on
# the same second and hand the winner to whoever happens to be first.
_CAPACITY_RETRY_FIRST_S = 10.0
_CAPACITY_RETRY_MAX_S = 60.0
_CAPACITY_RETRY_GROWTH = 1.6
_CAPACITY_RETRY_JITTER = 0.2


class SyncPodCapacityError(RuntimeError):
    """RunPod had no CPU machine for the whole wait.

    Distinct from :class:`RunPodError` because the caller acts on it
    differently: the scheduler turns this one into a ``CookError``. Unlike a
    GPU shortage there is nothing to fail item by item -- without the sync pod
    the cook never starts at all -- so failing the cook IS the right answer
    here, and the message is written to be read by a person.
    """


def _capacity_backoff(attempt, remaining=None, rand=None):
    """Seconds to wait before create attempt ``attempt`` (1-based), jittered.

    Clamped to ``remaining`` so the last sleep never overshoots the deadline
    and turn a 15-minute wait into a 16-minute one.
    """
    import random

    rand = rand or random.uniform
    delay = min(_CAPACITY_RETRY_MAX_S,
                _CAPACITY_RETRY_FIRST_S * (_CAPACITY_RETRY_GROWTH ** max(0, attempt - 1)))
    delay *= rand(1.0 - _CAPACITY_RETRY_JITTER, 1.0 + _CAPACITY_RETRY_JITTER)
    if remaining is not None:
        delay = min(delay, max(0.0, remaining))
    return delay


def _dedupe_running(api, running, log):
    """Keep the oldest of several exact-name RUNNING sync pods, terminate
    the rest. Belt and braces alongside the lock in :func:`ensure_sync_pod`
    -- covers a duplicate a crashed previous run left behind, or a race
    between two different *machines* sharing one account (the lock is
    per-machine only, see this module's docstring).
    """
    if len(running) <= 1:
        return running
    keep = min(running, key=lambda p: p.get("createdAt") or p.get("id", ""))
    for p in running:
        if p["id"] != keep["id"]:
            log(f"duplicate sync pod {p['id']} (keeping {keep['id']}); terminating")
            api.terminate_pod(p["id"])
    return [keep]


def _find_or_create_sync_pod(api, cfg, token, pubkey, log, cloud_type=None):
    name = sync_pod_name(cfg.user)
    # list_pods is a prefix match, and sync_pod_name has no trailing
    # delimiter, so "rpfarm-sync-may" would also match another user's
    # "rpfarm-sync-mayakovsky" pod -- filter down to an exact name match.
    existing = [p for p in api.list_pods(name) if p.get("name") == name]
    running = _dedupe_running(api, [p for p in existing if p.get("desiredStatus") == "RUNNING"], log)
    if running:
        return running[0]
    for p in existing:
        log(f"sync pod {p['id']} not running ({p.get('desiredStatus')}); terminating")
        api.terminate_pod(p["id"])
    # cfg.datacenter, not the API's fallback: the sync pod has to be created
    # in the same region as the network volume it mounts (finding 5).
    pod = api.create_cpu_pod(
        name, cfg.template_id, cfg.volume_id, pod_env(cfg, "sync", token, SYNC_SLOTS, pubkey), PORTS,
        datacenter=cfg.datacenter, cloud_type=cloud_type or CLOUD_TYPE_SECURE,
    )
    log(f"sync pod created: {pod['id']}")
    return pod


def ensure_sync_pod(
    api,
    cfg,
    token,
    pubkey,
    log=print,
    client_factory=None,
    sleep=time.sleep,
    timeout=300,
    cancel=lambda: False,
    capacity_wait_s=None,
    cloud_type=None,
    clock=time.monotonic,
    rand=None,
):
    """Find the user's CPU sync pod (creating it if missing) and wait for it
    to become reachable. A sync pod that exists but isn't ``RUNNING`` (e.g.
    ``EXITED``) is terminated and replaced.

    The check-then-create is serialized by a per-machine file lock
    (Ruling R24) so two callers racing this within the same cook -- the
    normal case for out-of-process ``runpodfarm_upload`` items -- adopt one
    pod rather than creating two. If the lock can't be acquired within
    :data:`_SYNC_POD_LOCK_TIMEOUT_S`, this proceeds unlocked rather than
    failing outright: a waiter that gives up on the lock re-checks for an
    existing pod exactly the same way a lock holder would, just without
    the exclusivity guarantee for that one attempt -- the dedup pass in
    :func:`_find_or_create_sync_pod` is what keeps that safe.

    A shortage of CPU machines is waited out rather than raised (Ruling R32):
    see :func:`_acquire_sync_pod`. ``capacity_wait_s`` defaults to the
    config's ``capacity_wait_min``, and ``cloud_type`` to its ``cloud_type``,
    so every caller -- the scheduler, ``package_runner``, the stats and
    download nodes -- gets the same behaviour without passing anything; the
    scheduler passes its own parm values explicitly.
    """
    client_factory = client_factory or (lambda pid: WorkerClient(pid, token))
    if capacity_wait_s is None:
        capacity_wait_s = max(0, int(getattr(cfg, "capacity_wait_min", 0) or 0)) * 60
    cloud_type = cloud_type or getattr(cfg, "cloud_type", None) or CLOUD_TYPE_SECURE

    pod = _acquire_sync_pod(
        api, cfg, token, pubkey, log,
        cloud_type=cloud_type, capacity_wait_s=capacity_wait_s,
        sleep=sleep, cancel=cancel, clock=clock, rand=rand,
    )
    return wait_ready(api, client_factory(pod["id"]), pod["id"], timeout=timeout, cancel=cancel, sleep=sleep, log=log)


def _acquire_sync_pod(api, cfg, token, pubkey, log, cloud_type, capacity_wait_s,
                      sleep, cancel, clock, rand):
    """Find or create the sync pod, waiting out a shortage of CPU machines.

    The same rule the GPU side follows (Ruling R32): no machine is a *wait*,
    not a refusal -- our pods are not spot instances, so the only question is
    whether a free one exists yet, and that answer changes minute to minute.
    Verified live: RunPod had no CPU capacity in EU-RO-1 for ~2.5 minutes and
    killed two cooks at second zero; the sixth attempt a few minutes later
    succeeded.

    Each attempt takes and releases the lock rather than holding it across the
    whole wait: another waiter on this machine can then adopt a pod that
    appeared in the meantime instead of queueing behind a sleeper.

    A 4xx is raised immediately -- a bad key or a missing template is not
    something waiting fixes.
    """
    deadline = None if capacity_wait_s <= 0 else clock() + capacity_wait_s
    started = clock()
    attempt = 0
    last_error = None
    while True:
        attempt += 1
        if cancel():
            raise SyncPodCapacityError("cancelled while waiting for a sync pod")
        try:
            try:
                with _file_lock(_sync_pod_lock_path(), timeout=_SYNC_POD_LOCK_TIMEOUT_S, sleep=sleep):
                    return _find_or_create_sync_pod(api, cfg, token, pubkey, log, cloud_type)
            except TimeoutError as e:
                log(f"sync pod lock not acquired ({e}); proceeding unlocked")
                return _find_or_create_sync_pod(api, cfg, token, pubkey, log, cloud_type)
        except RunPodError as e:
            if not is_capacity_error(e):
                raise
            last_error = e
            # One clock read per attempt: reading twice let the reported wait
            # come from an earlier instant than the deadline check, so a run
            # that gave up after 61s could report "0s".
            now = clock()
            waited = now - started
            remaining = None if deadline is None else deadline - now
            if remaining is not None and remaining <= 0:
                raise SyncPodCapacityError(
                    "No CPU machine for the sync pod in {} after waiting {}. "
                    "The cook cannot start without it.\n"
                    "RunPod has no free instances of this size right now -- this "
                    "usually clears in a few minutes, so try again shortly. You can "
                    "also set Cloud Type to Community on the scheduler (cheaper, and "
                    "often has machines when Secure is empty), or raise Wait For "
                    "Capacity.\nLast word from RunPod: {}".format(
                        cfg.datacenter, _fmt_wait(waited), last_error)) from e
            delay = _capacity_backoff(attempt, remaining=remaining, rand=rand)
            log("no CPU machine for the sync pod in {} ({}) -- waited {}, "
                "retrying in {:.0f}s (attempt {})".format(
                    cfg.datacenter, cloud_type, _fmt_wait(waited), delay, attempt + 1))
            sleep(delay)


def _fmt_wait(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{}s".format(seconds)
    return "{}m {:02d}s".format(seconds // 60, seconds % 60)


# -- MQ -----------------------------------------------------------------


def start_mq(client: WorkerClient, pod: dict, cook_id: str, sleep=time.sleep, timeout=180) -> str:
    """Start ``mqserver`` on the sync pod for ``cook_id`` and return the
    connection-file line rewritten with the pod's public address, e.g.
    ``"PDG_MQ 9.9.9.9 14440 14440 14442"``.

    ``worker.py``'s ``/exec`` already sources ``houdini_setup_bash`` (it
    wraps every command when ``$HFS/houdini_setup_bash`` exists), so
    ``mqserver`` can be called by name without an explicit ``cd $HFS &&
    source ...`` prefix.
    """
    conn_path = f"/workspace/.rpfarm/mq_{cook_id}.txt"
    log_path = f"/workspace/ledger/logs/mq_{cook_id}.log"
    command = (
        f"rm -f {conn_path}; "
        f"nohup mqserver -p 4440 -n 64 -l 1 -c {conn_path} -w 4442 16 /result "
        f"> {log_path} 2>&1 & sleep 1"
    )
    client.exec(command, timeout_s=30)

    t0 = time.time()
    while time.time() - t0 < timeout:
        data = client.read_file(conn_path)
        if data and data.startswith("PDG_MQ"):
            first_line = data.splitlines()[0]
            _, _host, rpc, _relay, http = first_line.split()
            ip, pub_rpc = pod_public_endpoint(pod, int(rpc))
            _, pub_http = pod_public_endpoint(pod, int(http))
            return f"PDG_MQ {ip} {pub_rpc} {pub_rpc} {pub_http}"
        sleep(1)
    raise TimeoutError(f"mqserver did not write connection file for cook {cook_id} in {timeout}s")


def stop_mq(client: WorkerClient) -> None:
    client.exec("pkill -f mqserver", timeout_s=10)


# -- orphans --------------------------------------------------------------


def find_orphans(api, user):
    """GPU pods for ``user`` that are still running -- excludes the sync
    pod, whose lifecycle is managed separately by :func:`ensure_sync_pod`."""
    prefix = f"rpfarm-{user}-"
    sync_name = sync_pod_name(user)
    return [
        p
        for p in api.list_pods(prefix)
        if p.get("name") != sync_name and p.get("desiredStatus") == "RUNNING"
    ]


def terminate_all(api, pod_ids: list[str]):
    for pid in pod_ids:
        api.terminate_pod(pid)
