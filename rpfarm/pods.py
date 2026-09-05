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


def pod_env(cfg, role, token, slots, pubkey, extra=None, cook="", project=""):
    """Environment a pod is created with.

    ``RPFARM_USER``/``RPFARM_COOK``/``RPFARM_PROJECT`` are identity, not
    configuration. RunPod returns a pod's ``env`` from ``GET /pods``, so they
    let anyone holding the account key answer "whose pod is this, and which
    cook does it belong to?" from the outside, without the local state of the
    machine that created it. That matters because the account is shared: an
    agent that could only see its own state killed a colleague's pods, having
    no way to tell them apart from its own leftovers.

    The name carries the same facts, but parsing it is guesswork -- a user or
    project containing "-" makes ``rpfarm-<user>-<project>-<cook>-<n>``
    ambiguous. These are unambiguous; the name stays for humans.
    """
    env = {
        "RPFARM_TOKEN": token,
        "RPFARM_ROLE": role,
        "RPFARM_SLOTS": str(slots),
        "RPFARM_USER": cfg.user,
        "RPFARM_COOK": cook,
        "RPFARM_PROJECT": project,
        "PUBLIC_KEY": pubkey,
        "HOUDINI_VERSION": cfg.houdini_version,
        "SESINETD_HOST": cfg.sesinetd_host,
        "SESINETD_PORT": str(cfg.sesinetd_port),
    }
    if extra:
        env.update(extra)
    return env


# -- who owns a pod, and is its cook alive -----------------------------------

# How long after a scheduler last spoke to a pod we still treat it as driven.
# A live cook's gap between two tasks is seconds; this is generous on purpose,
# because the cost of waiting is a few cents and the cost of being wrong is
# somebody's render.
COOK_ALIVE_GRACE_S = 180.0


def pod_owner(pod):
    """The user a pod belongs to: its env first, its name second.

    Falls back to the name because pods created before RPFARM_USER existed are
    still on the account, and "I cannot tell" must never silently become "mine".
    """
    env = pod.get("env") or {}
    owner = (env.get("RPFARM_USER") or "").strip()
    if owner:
        return owner
    name = pod.get("name") or ""
    if name.startswith("rpfarm-sync-"):
        return name[len("rpfarm-sync-"):]
    if name.startswith("rpfarm-"):
        rest = name[len("rpfarm-"):]
        return rest.split("-", 1)[0] if rest else ""
    return ""


def pod_cook(pod):
    env = pod.get("env") or {}
    return (env.get("RPFARM_COOK") or "").strip()


SYNC_KEEP = "keep"
SYNC_STOP = "stop"
SYNC_DELETE = "delete"


def sync_pod_action(pod, idle_s, stopped_s, stop_after_s, delete_after_s):
    """What to do with the sync pod right now: keep it, stop it, or delete it.

    The owner's policy, in one pure function so both the cook-start check and
    the timer that runs while Houdini is open decide identically.

    Stop first, delete later, because the two states cost very differently and
    neither is free: running is about $43/month, stopped about $2/month (RunPod
    bills a stopped pod's container disk at double the running rate, and 10 GB
    is what the sync pod has). Parking it keeps the disk and lets the next cook
    resume in seconds; deleting it reclaims the last couple of dollars at the
    price of a rebuild.

    A threshold of 0 means "never" for that step -- someone who wants the pod
    parked indefinitely should not have to invent a large number.

    ``idle_s`` is time since the pod was last used, ``stopped_s`` time since it
    was stopped; either may be ``None`` when unknown, and unknown never
    triggers an action. Same asymmetry as everywhere else here: doing nothing
    costs a little money, doing the wrong thing costs someone's work.
    """
    running = pod.get("desiredStatus") == "RUNNING"
    if running:
        if stop_after_s > 0 and idle_s is not None and idle_s >= stop_after_s:
            return SYNC_STOP
        return SYNC_KEEP
    if delete_after_s > 0 and stopped_s is not None and stopped_s >= delete_after_s:
        return SYNC_DELETE
    return SYNC_KEEP


def classify_for_kill(pod, me, health=None, health_error=None,
                      grace_s=COOK_ALIVE_GRACE_S):
    """``(verdict, reason)`` -- may this pod be terminated without being asked?

    ``verdict`` is one of ``"safe"``, ``"busy"``, ``"foreign"``, ``"unknown"``.
    Only ``"safe"`` is killed by a plain ``farm kill --all``; the rest need a
    flag that says out loud what it is overriding.

    Ownership is checked before liveness on purpose: another artist's *idle*
    pod is still not ours to take, and reporting it as "foreign" says something
    the caller can act on, where "safe" would be a lie with a price.

    ``health`` is the pod's own ``/health``. Unreachable (``health_error``)
    means we do not know, and not knowing resolves to leaving it alone -- the
    expensive mistake is killing something live, not paying a few cents while
    a human decides.

    "Idle" needs evidence from every channel, not just HTTP. ``busy`` and
    ``idle_s`` describe requests to the worker; ``ssh_sessions`` and
    ``transfers`` describe everything else. A pod that reports none of the
    latter two is ``unknown`` rather than safe -- see the tests, which are
    written from the incident where this function cleared a pod that was
    rendering.
    """
    owner = pod_owner(pod)
    if owner and me and owner != me:
        return "foreign", "belongs to {}".format(owner)
    if health_error is not None or health is None:
        return "unknown", "could not reach it to ask ({})".format(
            health_error or "no answer")
    busy = int(health.get("busy") or 0)
    if busy > 0:
        return "busy", "running {} task(s)".format(busy)

    # Work that never reaches the worker. `busy`/`idle_s` only ever counted
    # HTTP, so a pod driven over SSH -- or a sync pod taking a 4GB tarball over
    # SFTP, the heaviest thing this farm does -- reported itself idle while
    # working flat out, and this function called it safe to kill. It did that
    # to a pod that was rendering.
    transfers = health.get("transfers")
    if transfers:
        return "busy", "{} file transfer(s) in flight".format(int(transfers))
    sessions = health.get("ssh_sessions")
    if sessions:
        return "busy", "{} open ssh session(s) -- something is driving it " \
                       "outside the worker".format(int(sessions))

    # Neither of the two above can be trusted on its own: rclone opens and
    # closes a connection per file, so mid-upload there are instants with no
    # socket and no live child. Verified on a live pod. busy_idle_s comes from
    # the pod's own sampler and is what survives that churn.
    busy_idle = health.get("busy_idle_s")
    if busy_idle is not None and float(busy_idle) < grace_s:
        return "busy", ("something was transferring or connected {:.0f}s ago"
                        .format(float(busy_idle)))

    # A pod that cannot report these is a pod we cannot clear. Absent evidence
    # is not evidence of absence, and the asymmetry is deliberate: a false
    # "busy" costs a few cents of idle pod, a false "safe" costs a render.
    if sessions is None or transfers is None:
        return "unknown", ("it did not report ssh sessions or transfers, so "
                           "work outside the worker cannot be ruled out")

    idle = health.get("idle_s")
    if idle is None:
        return "unknown", "it did not report how long it has been idle"
    if float(idle) < grace_s:
        return "busy", "a scheduler was talking to it {:.0f}s ago".format(float(idle))
    return "safe", "idle for {:.0f}s, no ssh sessions, no transfers".format(float(idle))


# -- readiness ----------------------------------------------------------


def wait_ready(api, client, pod_id, timeout=300, cancel=lambda: False, sleep=time.sleep, log=print):
    """Poll ``get_pod`` until the pod has a public port 22 mapping *and* its
    8000/http proxy answers ``health()``. Returns the pod dict, or raises
    ``TimeoutError`` after ``timeout`` seconds / ``RuntimeError("canceled")``
    if ``cancel()`` returns true, / :class:`PodGoneError` if the pod is
    deleted while we wait."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cancel():
            raise RuntimeError("canceled")
        try:
            pod = api.get_pod(pod_id)
        except RunPodError as e:
            # 404 means this pod is gone, which is survivable: the caller finds
            # or creates another. Anything else -- 401, 403, a bad request --
            # is not about this pod and is re-raised untouched.
            if getattr(e, "status", None) == 404:
                raise PodGoneError(
                    "pod {} disappeared while we were waiting for it "
                    "(terminated elsewhere, or its host failed)".format(pod_id)) from e
            raise
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


class PodGoneError(RuntimeError):
    """The pod we were waiting on no longer exists.

    A pod can vanish legitimately: a host failure, a manual delete in the
    RunPod panel, another user's `farm kill`. It happened for real -- a pod was
    terminated out from under an in-flight ``ensure_sync_pod`` and the raw
    ``RunPod 404 pod not found`` came out of ``wait_ready`` and failed the work
    item. Same family as Ruling R32: a missing machine is a reason to get
    another one, not a reason to fail. Distinct from :class:`RunPodError` so
    the caller can tell "this one is gone" from "the key is wrong", which
    waiting never fixes.
    """


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

    # A pod that is not RUNNING used to be terminated here and replaced. That
    # was harmless while nothing ever stopped one -- and becomes the opposite
    # of a fix the moment the scheduler starts parking the sync pod instead of
    # deleting it: we would pay for the stopped pod AND its replacement, having
    # thrown away the container disk we stopped it to keep.
    #
    # Resume rather than replace. Which exact `desiredStatus` a stopped pod
    # reports is not relied on: anything not RUNNING is offered to start_pod,
    # and only a pod that refuses to come back is terminated and rebuilt. That
    # keeps this correct whatever RunPod calls the state.
    for p in existing:
        state = p.get("desiredStatus")
        try:
            api.start_pod(p["id"])
            log(f"sync pod {p['id']} was {state}; started it again")
            return p
        except RunPodError as e:
            log(f"sync pod {p['id']} ({state}) would not start ({e}); replacing it")
            try:
                api.terminate_pod(p["id"])
            except RunPodError as e2:
                log(f"  and could not terminate it either: {e2}")
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

    # Acquiring and waiting are one loop, not two steps. A pod can be adopted
    # and then vanish before it is ready -- terminated elsewhere, host failure
    # -- and that used to escape as `RunPod 404 pod not found` and fail the
    # work item. It happened for real to a download item whose sync pod was
    # killed under it. Losing the pod puts us back at "find or create one",
    # inside the same capacity deadline, exactly like being told there is no
    # capacity in the first place.
    deadline = None if capacity_wait_s <= 0 else clock() + capacity_wait_s
    while True:
        remaining = None if deadline is None else max(0.0, deadline - clock())
        pod = _acquire_sync_pod(
            api, cfg, token, pubkey, log,
            cloud_type=cloud_type,
            capacity_wait_s=0 if remaining is None else remaining,
            sleep=sleep, cancel=cancel, clock=clock, rand=rand,
        )
        try:
            return wait_ready(api, client_factory(pod["id"]), pod["id"],
                              timeout=timeout, cancel=cancel, sleep=sleep, log=log)
        except PodGoneError as e:
            if deadline is not None and clock() >= deadline:
                raise SyncPodCapacityError(
                    "The sync pod kept disappearing while we waited for it, and "
                    "the {} we were given to wait ran out.\nLast: {}".format(
                        _fmt_wait(capacity_wait_s), e)) from e
            log("{} -- looking for another".format(e))


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
                    "usually clears in a few minutes, so try again shortly, or "
                    "raise Wait For Capacity.\nLast word from RunPod: {}".format(
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
