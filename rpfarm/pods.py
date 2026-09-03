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
from .runpod_api import RunPodError, pod_public_endpoint
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


def _find_or_create_sync_pod(api, cfg, token, pubkey, log):
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
    pod = api.create_cpu_pod(
        name, cfg.template_id, cfg.volume_id, pod_env(cfg, "sync", token, SYNC_SLOTS, pubkey), PORTS
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
    """
    client_factory = client_factory or (lambda pid: WorkerClient(pid, token))
    try:
        with _file_lock(_sync_pod_lock_path(), timeout=_SYNC_POD_LOCK_TIMEOUT_S, sleep=sleep):
            pod = _find_or_create_sync_pod(api, cfg, token, pubkey, log)
    except TimeoutError as e:
        log(f"sync pod lock not acquired ({e}); proceeding unlocked")
        pod = _find_or_create_sync_pod(api, cfg, token, pubkey, log)
    return wait_ready(api, client_factory(pod["id"]), pod["id"], timeout=timeout, cancel=cancel, sleep=sleep, log=log)


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
