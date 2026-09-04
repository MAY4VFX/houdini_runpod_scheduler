"""RunPodFarm pod housekeeping CLI.

Runs on the sync pod (the only pod with a live view of the whole Network
Volume). Tracks project usage in ``/workspace/.rpfarm/index.json`` and gives
the scheduler's Volume tab (and, later, ``rpfarm storage``) a single place
to ask "what's on the volume" and "get rid of X".

stdlib only -- deployed as-is to Ubuntu 22.04 / python3.10 pods alongside
``worker.py``, so no syntax newer than 3.10 and no third-party imports.

Commands (see ``main()``/each ``cmd_*`` docstring for exact shapes)::

    housekeeping.py ls [--root /workspace] [--refresh] [--budget-s N] [--max-age-s N]
                       [--volume-size-gb N]
    housekeeping.py du <path>
    housekeeping.py touch <user>/<project> [--event cook|upload|download]
    housekeeping.py rm <user>/<project> [--force]
    housekeeping.py prune [--older-days N] [--dry-run]
    housekeeping.py houdini ls [--refresh]
    housekeeping.py houdini rm <version> [--dry-run]
    housekeeping.py sync-idle
    housekeeping.py disk-usage [--volume-size-gb N] [--refresh] [--budget-s N] [--max-age-s N]
    housekeeping.py invalidate <houdini|apps|projects|ledger>

``--volume-size-gb`` is the RunPod network volume's real provisioned size
(the pod has no RunPod API key and cannot look this up itself -- callers
pass ``RunPodAPI.get_volume(cfg.volume_id)["size"]``). Without it, `ls`/
`disk-usage` report ``total``/``used_pct`` as ``null`` rather than ever
deriving them from ``shutil.disk_usage`` -- inside a pod that reports the
backing storage pool's capacity, not the volume's own size (Ruling R27,
confirmed live: a real 50GB volume read back as ~2.14 PiB).

``ls``/``houdini ls`` serve cached sizes from ``/workspace/.rpfarm/index.json``
when under 900s old and re-measure (bounded, see ``cmd_ls``/``cmd_houdini_ls``)
otherwise; ``--refresh`` forces a re-measure regardless of age (Ruling R26).

Every command prints one JSON value to stdout. On failure it prints a
one-line message to stderr and exits non-zero -- callers (``_volume_exec``
in the scheduler, ``WorkerClient.exec``) check ``exit_code``, not stdout.

Only ``rm``, ``prune`` and ``houdini rm`` are destructive, and only within
``/workspace/projects/<user>/<project>`` or ``/workspace/houdini/<version>``
respectively: ``/workspace/houdini`` (the zone itself), ``/workspace/ledger``
and ``/workspace/.rpfarm`` are never touched by them (ported from v1's
``worker/cache_manager.py::_is_protected``, v1, removed in Task 14). The CLI hardcodes
``root="/workspace"`` for every command except ``ls`` and ``du`` (read-only,
so a ``--root`` override is harmless and useful for debugging) -- there is
no flag to point a destructive command anywhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows dev boxes only, pods are Linux
    fcntl = None

DEFAULT_ROOT = "/workspace"

_ZONES = ("houdini", "apps", "projects", "ledger")
# Zone directory names that rm/prune/houdini-rm must never delete or descend
# past. Relative to the volume root -- ported from v1's _PROTECTED_PREFIXES.
_PROTECTED_ZONES = ("houdini", "ledger", ".rpfarm")

_INDEX_REL = os.path.join(".rpfarm", "index.json")
_SYNC_LAST_USED_REL = os.path.join(".rpfarm", "sync_last_used")
_BOOT_LOG_DIR_REL = os.path.join("ledger", "logs")
# Spec 4.1: "ledger: чистка -- никогда (ротация логов > 90 дней)". Boot logs
# accumulate one file per pod start (Task 4 had to delete 52 by hand); this
# is the only thing in ledger/ that prune ever removes, and only by age.
_BOOT_LOG_RETENTION_DAYS = 90

_TOUCH_EVENTS = ("cook", "upload", "download")
# Which index field each touch event stamps, in addition to last_used.
_EVENT_FIELD = {"cook": "last_cook", "upload": "last_upload", "download": "last_download"}

# Directory name components that mark a subtree as "cook output", used by
# outputs_pending: render/ and geo/ are the two conventional ROP output
# dirs, and PDG work-item result-data folders always have "resultdata" in
# their name (case varies by node).
_OUTPUT_DIR_NAMES = ("render", "geo")

# Ruling R26: a pure-Python os.walk + os.lstat-per-file size (the original
# Task 4/12 implementation) timed out at 60s on the real farm's Houdini
# install over the network-mounted volume. `du -sb` (GNU coreutils, on the
# pod image) is one native tree walk instead of one Python-level syscall
# per file -- dramatically faster for the same tree. Per-call timeout for
# that subprocess; see _size()'s docstring for the fallback/caching story.
_SIZE_TIMEOUT_S = 30.0
# ls/houdini ls default budget for a *full* re-measure sweep across every
# zone/project that needs one (Ruling R26 point 3) -- comfortably under
# _volume_exec's own 300s exec timeout, with margin for network latency.
_DEFAULT_BUDGET_S = 120.0
# How long a cached size is served before ls/houdini ls re-measures it
# (Ruling R26 point 2); --refresh forces a re-measure regardless of age.
_DEFAULT_MAX_AGE_S = 900.0

_GB = 2**30


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

# Sticky once we learn `du -sb` doesn't work in this environment (BSD `du`
# on macOS dev/test boxes has no -b; a real Ubuntu pod always supports it,
# so this stays False there and every call takes the fast path). Set only
# from an immediate "invalid/illegal option" or "command not found" --
# never from a timeout, which says nothing about whether the flag itself
# is supported.
_DU_B_UNSUPPORTED = False


def _size(path: str, timeout_s: float = _SIZE_TIMEOUT_S):
    """Total size in bytes of a file, or recursively of a directory.

    Directories go through ``du -sb`` first (falls back once, permanently
    for this process, to a pure-Python walk if ``-b`` isn't supported at
    all -- macOS/BSD ``du`` has no such flag). Returns ``None`` if the
    ``du`` subprocess itself exceeds ``timeout_s`` -- callers decide how to
    handle that (serve a cached value, report 0, ...); this never silently
    falls back to the slow walk on a timeout, which would just make a slow
    path slower still.
    """
    global _DU_B_UNSUPPORTED
    if os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    if not os.path.isdir(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0

    if not _DU_B_UNSUPPORTED:
        try:
            proc = subprocess.run(
                ["du", "-sb", path], capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            _DU_B_UNSUPPORTED = True  # no `du` binary at all -- never coming back
            proc = None
        except OSError:
            proc = None
        if proc is not None:
            if proc.returncode == 0 and proc.stdout:
                try:
                    return int(proc.stdout.split()[0])
                except (ValueError, IndexError):
                    pass
            elif "invalid option" in proc.stderr.lower() or "illegal option" in proc.stderr.lower():
                _DU_B_UNSUPPORTED = True

    return _size_walk(path)


def _size_walk(path: str) -> int:
    """Pure-Python recursive size -- the pre-R26 implementation, kept as
    the fallback for platforms without a `-b`-capable `du` (tests)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                continue
    return total


def du(path: str) -> list[dict]:
    """Sizes of the first-level children of ``path``. Unchanged Task 4 contract.

    A child whose ``du -sb`` exceeds :data:`_SIZE_TIMEOUT_S` reports
    ``bytes: null`` rather than blocking the rest of the listing or falling
    back to an even-slower full Python walk.
    """
    entries = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return entries
    for name in names:
        child = os.path.join(path, name)
        try:
            entries.append({"path": child, "bytes": _size(child)})
        except OSError:
            continue
    return entries


class HousekeepingError(Exception):
    """Raised for a caller mistake (bad path, unknown project, ...)."""


def _is_protected(root: str, target: str) -> bool:
    """True if ``target`` is, or is inside, one of the never-touch zones."""
    target = os.path.normpath(target)
    for zone in _PROTECTED_ZONES:
        zone_path = os.path.normpath(os.path.join(root, zone))
        if target == zone_path or target.startswith(zone_path + os.sep):
            return True
    return False


def _parse_user_project(user_project: str) -> tuple[str, str]:
    """Validate ``"<user>/<project>"``, guarding against path traversal."""
    parts = user_project.split("/")
    if len(parts) != 2 or not all(parts):
        raise HousekeepingError(f"expected <user>/<project>, got {user_project!r}")
    user, project = parts
    for part in (user, project):
        if part in (".", "..") or "/" in part or "\\" in part:
            raise HousekeepingError(f"invalid path component in {user_project!r}")
    return user, project


def _project_dir(root: str, user: str, project: str) -> str:
    path = os.path.normpath(os.path.join(root, "projects", user, project))
    projects_root = os.path.normpath(os.path.join(root, "projects"))
    if path == projects_root or not path.startswith(projects_root + os.sep):
        raise HousekeepingError(f"resolved path escapes projects/: {path}")
    return path


# ---------------------------------------------------------------------------
# Index: /workspace/.rpfarm/index.json
# ---------------------------------------------------------------------------


def _index_path(root: str) -> str:
    return os.path.join(root, _INDEX_REL)


def _load_index(root: str) -> dict:
    path = _index_path(root)
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _with_index_lock(root: str):
    """Open (creating if needed) the index file with an exclusive lock.

    Returns the open file handle, positioned at 0. Callers must read,
    modify, seek(0), truncate() and write, then close. Locking is advisory
    (fcntl.flock) and best-effort -- on a platform without fcntl (not any
    real pod) writes just race like they always did.
    """
    os.makedirs(os.path.dirname(_index_path(root)), exist_ok=True)
    path = _index_path(root)
    # "a+" creates the file if missing without truncating it, and allows
    # both read and write once we seek(0).
    f = open(path, "a+")
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    f.seek(0)
    return f


def _update_index(root: str, key: str, fields: dict) -> dict:
    """Merge ``fields`` into ``index[key]`` and persist. Returns the new entry."""
    f = _with_index_lock(root)
    try:
        raw = f.read()
        try:
            index = json.loads(raw) if raw.strip() else {}
        except ValueError:
            index = {}
        if not isinstance(index, dict):
            index = {}
        entry = dict(index.get(key) or {})
        entry.update(fields)
        index[key] = entry
        f.seek(0)
        f.truncate()
        json.dump(index, f)
        return entry
    finally:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _save_index(root: str, mutate) -> dict:
    """Read-modify-write the whole index under one lock.

    ``mutate(index) -> index`` gets the current (possibly empty) dict and
    returns the dict to persist -- used by ls/houdini ls to batch many
    size-cache updates (one per zone/project) into a single lock
    acquisition instead of one ``_update_index`` call each.
    """
    f = _with_index_lock(root)
    try:
        raw = f.read()
        try:
            index = json.loads(raw) if raw.strip() else {}
        except ValueError:
            index = {}
        if not isinstance(index, dict):
            index = {}
        index = mutate(index)
        f.seek(0)
        f.truncate()
        json.dump(index, f)
        return index
    finally:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _remove_index_entry(root: str, key: str) -> None:
    f = _with_index_lock(root)
    try:
        raw = f.read()
        try:
            index = json.loads(raw) if raw.strip() else {}
        except ValueError:
            index = {}
        if not isinstance(index, dict):
            index = {}
        index.pop(key, None)
        f.seek(0)
        f.truncate()
        json.dump(index, f)
    finally:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# ---------------------------------------------------------------------------
# outputs_pending
# ---------------------------------------------------------------------------


def _is_output_component(name: str) -> bool:
    return name in _OUTPUT_DIR_NAMES or "resultdata" in name.lower()


def _outputs_pending(project_dir: str, last_download) -> bool:
    """True if any file under an output dir is newer than ``last_download``.

    ``last_download`` is the index timestamp (float epoch seconds) or None
    (never downloaded -- any output file at all counts as pending).
    """
    threshold = last_download or 0
    for dirpath, _dirnames, filenames in os.walk(project_dir, onerror=lambda e: None):
        rel = os.path.relpath(dirpath, project_dir)
        parts = [] if rel == "." else rel.split(os.sep)
        if not any(_is_output_component(p) for p in parts):
            continue
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if os.lstat(fp).st_mtime > threshold:
                    return True
            except OSError:
                continue
    return False


# ---------------------------------------------------------------------------
# Size caching (Ruling R26): serve /workspace/.rpfarm/index.json's cached
# sizes when they're fresh enough, re-measure (bounded by a shared,
# shrinking deadline) when they're not, and never let one slow zone/project
# block every other one from at least reporting its last known size.
# ---------------------------------------------------------------------------


class _SizeCache:
    """One re-measure sweep's worth of state, shared across every zone or
    version cmd_ls/cmd_houdini_ls looks at, so a single deadline (not a
    fresh per-call budget) governs the whole command.

    Review finding (fix round 2, "C"): ``flush()`` used to replace the
    whole namespace with ``self.cache`` -- a snapshot read at
    construction time, *outside* any lock. Two concurrent
    ``package_runner.py`` processes each doing their own `ls`/
    `disk-usage`/`touch` would each load that stale snapshot, and
    whichever flushed second would silently discard whatever the first
    had written for keys it never touched -- `flock` only prevents a
    torn write, not this logical race. Fixed by tracking only the keys
    *this* sweep actually (re)measured (``updated``, separate from
    ``cache``, which still holds the full read-time view so ``get()``'s
    own within-sweep staleness logic is unaffected) and merging just
    those into a freshly re-read index at flush time, inside the lock.

    Known residual (Ruling R28, parked -- not fixed): a merge-flush can
    still resurrect a key another process invalidated (e.g.
    ``cmd_touch``'s upload invalidation, fix round 2 "B") *during* this
    sweep, between this process's own read and its flush -- the merge
    reads fresh at flush time, but a key this sweep measured and the
    other process deleted in between will still be written back by this
    flush. With fix round 3 "1" landed, sweeps are seconds long (a single
    zone's ``du -sb``, not a 90s multi-zone `--refresh` walk), which
    shrinks that window to roughly the size of any other read-modify-
    write in this module. The failure mode is a stale size for at most
    ``max_age_s`` -- it self-corrects on the next sweep and is never
    destructive (no deletion decision anywhere reads ``_sizes``). Task 13
    inherits this as a known limitation, not a bug to silently reintroduce
    a fix for without re-deriving why it was judged acceptable.
    """

    def __init__(self, root, index, namespace, refresh, max_age_s, budget_s):
        self.root = root
        self.namespace = namespace
        self.cache = dict(index.get(namespace) or {})
        self.updated = {}
        self.refresh = refresh
        self.max_age_s = max_age_s
        self.deadline = time.time() + budget_s
        self.partial = False

    def get(self, key, path):
        """(bytes, measured_at) for one zone/project/version, from cache or
        a fresh ``_size()`` call within the shared deadline."""
        now = time.time()
        cached = self.cache.get(key)
        stale = cached is None or (now - cached["measured_at"]) > self.max_age_s
        if not self.refresh and not stale:
            return cached["bytes"], cached["measured_at"]

        remaining = self.deadline - now
        measured = _size(path, timeout_s=remaining) if remaining > 0 else None
        if measured is None:
            self.partial = True
            if cached is not None:
                return cached["bytes"], cached["measured_at"]
            return 0, None

        entry = {"bytes": measured, "measured_at": now}
        self.cache[key] = entry
        self.updated[key] = entry
        return measured, now

    def flush(self, namespace=None):
        """Merge this sweep's freshly-measured keys into the *current*
        on-disk namespace (re-read inside the lock), not the read-time
        snapshot -- see the class docstring."""
        if not self.updated:
            return
        ns = namespace or self.namespace

        def _mutate(index):
            current = dict(index.get(ns) or {})
            current.update(self.updated)
            index[ns] = current
            return index

        _save_index(self.root, _mutate)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _volume_totals(used: int, volume_size_gb) -> dict:
    """``{"used", "total", "used_pct"[, "note"]}`` for the volume-wide figure.

    Ruling R27: ``shutil.disk_usage(root)`` inside a pod reports the
    *backing storage pool's* capacity, not the RunPod network volume's own
    provisioned/billed size -- confirmed live, a real 50GB volume read
    back as ~2.14 PiB. Never used here or anywhere else in this module.
    ``used`` is always the real, zone-summed byte count the caller already
    measured; ``total``/``used_pct`` require the caller to supply the
    volume's actual size (``volume_size_gb`` -- the pod has no RunPod API
    key and cannot look this up itself; callers get it from
    ``RunPodAPI.get_volume(cfg.volume_id)["size"]``). Without it, both are
    ``None`` and ``note`` says why, rather than silently reporting a
    number that means nothing.
    """
    result = {"used": used, "total": None, "used_pct": None}
    if volume_size_gb:
        total = int(float(volume_size_gb) * _GB)
        result["total"] = total
        result["used_pct"] = round(100.0 * used / total, 2) if total else None
    else:
        result["note"] = (
            "no --volume-size-gb given; total/used_pct unknown -- "
            "shutil.disk_usage() inside a pod reports the backing storage "
            "pool's capacity, not the network volume's real size (Ruling R27)"
        )
    return result


def cmd_ls(
    root: str,
    refresh: bool = False,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    budget_s: float = _DEFAULT_BUDGET_S,
    volume_size_gb=None,
) -> dict:
    """Zones, per-project usage, and volume totals.

    ``{"zones": {"houdini": bytes, "apps": bytes, "projects": bytes,
    "ledger": bytes}, "projects": [{"user", "project", "bytes", "last_used",
    "last_cook", "outputs_pending"}], "volume": {"used", "total",
    "used_pct"[, "note"]}, "partial": bool}``. Projects are sorted by
    (user, project) for a stable order. See :func:`_volume_totals` for
    ``volume``'s shape and why it needs ``volume_size_gb`` (CLI
    ``--volume-size-gb``) rather than ``shutil.disk_usage``.

    Sizes come from the ``_sizes`` cache (zone and per-project entries) in
    ``/workspace/.rpfarm/index.json`` when they're under ``max_age_s`` old
    (default 900s); otherwise re-measured, bounded by ``budget_s`` shared
    across the whole call (default 120s -- comfortably under
    ``_volume_exec``'s 300s exec timeout). ``partial`` is true if the
    budget ran out before every stale entry could be re-measured -- those
    entries report their last known (stale) size instead of blocking.
    ``refresh=True`` (CLI ``--refresh``) forces every entry to be
    re-measured regardless of age.
    """
    index = _load_index(root)
    size_cache = _SizeCache(root, index, "_sizes", refresh, max_age_s, budget_s)

    zones = {}
    for zone in _ZONES:
        nbytes, _ = size_cache.get(zone, os.path.join(root, zone))
        zones[zone] = nbytes

    projects_root = os.path.join(root, "projects")
    projects = []
    if os.path.isdir(projects_root):
        for user in sorted(os.listdir(projects_root)):
            if user.startswith("."):
                continue
            user_dir = os.path.join(projects_root, user)
            if not os.path.isdir(user_dir):
                continue
            for project in sorted(os.listdir(user_dir)):
                if project.startswith("."):
                    continue
                project_dir = os.path.join(user_dir, project)
                if not os.path.isdir(project_dir):
                    continue
                key = f"{user}/{project}"
                entry = index.get(key, {})
                nbytes, _ = size_cache.get(key, project_dir)
                projects.append(
                    {
                        "user": user,
                        "project": project,
                        "bytes": nbytes,
                        "last_used": entry.get("last_used"),
                        "last_cook": entry.get("last_cook"),
                        "outputs_pending": _outputs_pending(
                            project_dir, entry.get("last_download")
                        ),
                    }
                )

    size_cache.flush()

    volume = _volume_totals(sum(zones.values()), volume_size_gb)

    return {
        "zones": zones,
        "projects": projects,
        "volume": volume,
        "partial": size_cache.partial,
    }


def _invalidate_size_cache(root: str, keys) -> None:
    """Drop ``keys`` from the ``_sizes`` cache so the next `ls`/`disk-usage`
    remeasures them instead of serving a now-wrong cached figure."""
    keys = list(keys)
    if not keys:
        return

    def _mutate(index):
        cache = dict(index.get("_sizes") or {})
        for key in keys:
            cache.pop(key, None)
        index["_sizes"] = cache
        return index

    _save_index(root, _mutate)


def cmd_touch(root: str, user_project: str, event: str = "cook") -> dict:
    """Stamp ``last_used`` (and the per-event field) for a project.

    ``touch <user>/<project> [--event cook|upload|download]``.

    Review finding (fix round 2, "B"): an ``event="upload"`` touch means
    bytes just landed on disk, so it invalidates that project's and the
    ``projects`` zone's cached size (Ruling R26/R27) -- without this,
    every upload item after the first in a burst decided `maybe_grow_volume`
    against a figure that under-reports exactly the bytes the burst itself
    is writing, for up to ``max_age_s`` (900s default), with no ``partial``
    flag to say so (that only fires when a *fresh* measurement times out,
    not when a stale one is served without even trying). ``cook``/
    ``download`` touches don't change volume-side bytes and don't
    invalidate anything.
    """
    if event not in _TOUCH_EVENTS:
        raise HousekeepingError(f"unknown event {event!r}, expected one of {_TOUCH_EVENTS}")
    user, project = _parse_user_project(user_project)
    now = time.time()
    fields = {"last_used": now, _EVENT_FIELD[event]: now}
    key = f"{user}/{project}"
    entry = _update_index(root, key, fields)
    if event == "upload":
        _invalidate_size_cache(root, [key, "projects"])
    return {"ok": True, "user": user, "project": project, "event": event, **entry}


def cmd_invalidate(root: str, zone: str) -> dict:
    """Drop one zone's cached size so the next `ls`/`disk-usage` remeasures it.

    ``invalidate <houdini|apps|projects|ledger>``. Review finding (fix
    round 3, "2"): nothing invalidated ``_sizes["houdini"]`` when a
    version was installed -- a stale-but-present entry doesn't set
    ``partial`` either, so after the Houdini-install preset
    (``rpfarm.packages.houdini_install_preset``) added ~10GB, the
    85%-full auto-grow guard compared against the old figure with no
    signal at all that anything was wrong. The preset's post-command now
    calls this once the install finishes.

    Task 14 found the deletion side had the same hole, despite the note
    that once stood here: :func:`cmd_houdini_rm` pruned the per-version
    ``_houdini`` cache but left ``_sizes["houdini"]`` alone, so right
    after deleting the 10.8GB legacy install ``storage ls`` still billed
    the zone at 23.4GB (stale legacy + the new 12.6GB install) and the
    volume looked half again as full as it was. ``houdini rm`` now
    invalidates the zone itself on a real deletion.
    """
    if zone not in _ZONES:
        raise HousekeepingError(f"unknown zone {zone!r}, expected one of {_ZONES}")
    _invalidate_size_cache(root, [zone])
    return {"ok": True, "zone": zone}


def cmd_rm(root: str, user_project: str, force: bool = False) -> dict:
    """Delete a project directory.

    Refuses (``{"ok": False, ...}``) unless ``force`` or there is nothing
    pending download, and always refuses a protected path.

    Known residual (reviewer-noted, parked -- not fixed): this removes
    the project's *touch* index entry (``_remove_index_entry``) but does
    not invalidate ``_sizes["projects"]`` or the deleted project's own
    ``_sizes`` entry -- the ``projects`` zone total stays stale
    (over-counting the just-deleted bytes) until it naturally expires
    (``max_age_s``) or something else invalidates it. Over-stating a
    zone's size is the safe direction for the 85%-full auto-grow guard
    (it can only make the guard grow a little early, never miss a real
    "getting full" case) -- unlike ``cmd_touch``'s upload invalidation
    (fix round 2 "B"), which had to be fixed because *under*-stating is
    the unsafe direction.
    """
    user, project = _parse_user_project(user_project)
    project_dir = _project_dir(root, user, project)
    if _is_protected(root, project_dir):
        return {"ok": False, "error": "protected path", "path": project_dir}
    if not os.path.isdir(project_dir):
        return {"ok": False, "error": "not found", "path": project_dir}

    index = _load_index(root)
    key = f"{user}/{project}"
    last_download = index.get(key, {}).get("last_download")
    pending = _outputs_pending(project_dir, last_download)
    if pending and not force:
        return {
            "ok": False,
            "error": "outputs pending, not downloaded -- pass --force to delete anyway",
            "path": project_dir,
            "outputs_pending": True,
        }

    freed = _size(project_dir)
    shutil.rmtree(project_dir)
    _remove_index_entry(root, key)
    return {"ok": True, "path": project_dir, "bytes_freed": freed}


def cmd_prune(root: str, older_days: float = 30, dry_run: bool = True) -> dict:
    """Projects unused for ``older_days`` (never pending, never protected).

    ``prune [--older-days N] [--dry-run]``. Also rotates
    ``ledger/logs/boot-*.log`` older than
    :data:`_BOOT_LOG_RETENTION_DAYS`, independent of ``older_days`` (spec
    4.1: ledger itself is never pruned, only its boot logs, by age).
    """
    now = time.time()
    index = _load_index(root)
    projects_root = os.path.join(root, "projects")

    candidates = []
    if os.path.isdir(projects_root):
        for user in sorted(os.listdir(projects_root)):
            user_dir = os.path.join(projects_root, user)
            if user.startswith(".") or not os.path.isdir(user_dir):
                continue
            for project in sorted(os.listdir(user_dir)):
                project_dir = os.path.join(user_dir, project)
                if project.startswith(".") or not os.path.isdir(project_dir):
                    continue
                if _is_protected(root, project_dir):
                    continue  # never true here (projects/ isn't a protected zone), kept as a belt-and-braces guard
                key = f"{user}/{project}"
                entry = index.get(key, {})
                last_used = entry.get("last_used")
                if last_used is None:
                    try:
                        last_used = os.path.getmtime(project_dir)
                    except OSError:
                        last_used = now
                age_days = (now - last_used) / 86400.0
                if age_days < older_days:
                    continue
                if _outputs_pending(project_dir, entry.get("last_download")):
                    continue
                candidates.append(
                    {
                        "path": project_dir,
                        "user": user,
                        "project": project,
                        "bytes": _size(project_dir),
                        "age_days": age_days,
                    }
                )

    if not dry_run:
        for c in candidates:
            shutil.rmtree(c["path"], ignore_errors=True)
            _remove_index_entry(root, f"{c['user']}/{c['project']}")

    boot_logs = _rotate_boot_logs(root, now, dry_run)

    return {"candidates": candidates, "deleted": not dry_run, "boot_logs_rotated": boot_logs}


def _rotate_boot_logs(root: str, now: float, dry_run: bool) -> list[dict]:
    log_dir = os.path.join(root, _BOOT_LOG_DIR_REL)
    if not os.path.isdir(log_dir):
        return []
    threshold = now - _BOOT_LOG_RETENTION_DAYS * 86400.0
    rotated = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in sorted(names):
        if not (name.startswith("boot-") and name.endswith(".log")):
            continue
        fp = os.path.join(log_dir, name)
        try:
            mtime = os.lstat(fp).st_mtime
        except OSError:
            continue
        if mtime >= threshold:
            continue
        rotated.append({"path": fp, "age_days": (now - mtime) / 86400.0})
        if not dry_run:
            try:
                os.remove(fp)
            except OSError:
                pass
    return rotated


# A proper version directory looks like "22.0.393" (rpfarm's own installs,
# via the Houdini-install upload preset). v1 installed straight into
# /workspace/houdini/ with no version subdirectory at all (spec 4.1: "сейчас
# legacy 20.5 лежит прямо в /workspace/houdini/") -- every top-level entry
# that doesn't match this shape is grouped into one synthetic "legacy"
# version instead of being listed (and sized) as N separate fake versions.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

LEGACY_VERSION = "legacy"


def cmd_houdini_ls(
    root: str,
    refresh: bool = False,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    budget_s: float = _DEFAULT_BUDGET_S,
) -> dict:
    """Installed Houdini versions under ``/workspace/houdini``.

    Real ``NN.N.NNN`` version directories are listed individually; any
    other top-level entry (v1's flat legacy install: `bin/`, `houdini/`,
    loose files, ...) is summed into one ``"legacy"`` entry so it shows up
    as one cleanup unit, not dozens. ``{"versions": [...], "partial":
    bool}`` -- caching/budget/``refresh`` semantics match :func:`cmd_ls`
    (Ruling R26); each top-level entry (real version or legacy piece) is
    cached individually under its own name so an unrelated legacy file
    changing doesn't invalidate a real version's cached size.
    """
    houdini_root = os.path.join(root, "houdini")
    index = _load_index(root)
    size_cache = _SizeCache(root, index, "_houdini", refresh, max_age_s, budget_s)

    versions = []
    legacy_bytes = 0
    have_legacy = False
    if os.path.isdir(houdini_root):
        for name in sorted(os.listdir(houdini_root)):
            if name.startswith("."):
                continue
            entry = os.path.join(houdini_root, name)
            nbytes, _ = size_cache.get(name, entry)
            if _VERSION_RE.match(name) and os.path.isdir(entry):
                versions.append({"version": name, "bytes": nbytes})
            else:
                have_legacy = True
                legacy_bytes += nbytes
    if have_legacy:
        versions.append({"version": LEGACY_VERSION, "bytes": legacy_bytes})

    size_cache.flush()
    return {"versions": versions, "partial": size_cache.partial}


def _prune_houdini_cache(root: str, names) -> None:
    """Drop ``names`` from the ``_houdini`` size cache (entries that no
    longer exist after a real deletion)."""
    names = list(names)
    if not names:
        return

    def _mutate(index):
        cache = dict(index.get("_houdini") or {})
        for name in names:
            cache.pop(name, None)
        index["_houdini"] = cache
        return index

    _save_index(root, _mutate)


def cmd_houdini_rm(
    root: str,
    version: str,
    dry_run: bool = False,
    refresh: bool = False,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    budget_s: float = _DEFAULT_BUDGET_S,
) -> dict:
    """Delete one installed Houdini version (or report what would be deleted).

    ``version="legacy"`` removes every top-level entry under
    ``/workspace/houdini`` that is *not* a proper ``NN.N.NNN`` directory
    (v1's flat install) in one call, leaving real versions untouched --
    otherwise deletes that one version's directory. ``dry_run=True``
    (CLI ``--dry-run``) computes and reports the same ``removed``/
    ``bytes_freed`` shape without deleting anything -- Task 14's
    destructive step is expected to run this first.

    Sizes come from the same ``_houdini`` cache :func:`cmd_houdini_ls`
    uses (review follow-up: this used to always measure every entry
    fresh -- 48s for the real farm's 14-entry legacy install -- even
    right after a `houdini ls` had just cached the same numbers). A real
    (non-dry-run) deletion prunes the removed names from the cache.
    """
    if not version or version in (".", "..") or "/" in version or "\\" in version:
        raise HousekeepingError(f"invalid version {version!r}")
    houdini_root = os.path.normpath(os.path.join(root, "houdini"))
    index = _load_index(root)
    size_cache = _SizeCache(root, index, "_houdini", refresh, max_age_s, budget_s)

    if version == LEGACY_VERSION:
        if not os.path.isdir(houdini_root):
            return {"ok": False, "error": "not found", "path": houdini_root}
        removed = []
        removed_names = []
        freed = 0
        for name in sorted(os.listdir(houdini_root)):
            if name.startswith(".") or _VERSION_RE.match(name):
                continue
            entry = os.path.join(houdini_root, name)
            nbytes, _ = size_cache.get(name, entry)
            freed += nbytes
            if not dry_run:
                if os.path.isdir(entry) and not os.path.islink(entry):
                    shutil.rmtree(entry)
                else:
                    os.remove(entry)
                removed_names.append(name)
            removed.append(entry)
        size_cache.flush()
        if removed_names:
            _prune_houdini_cache(root, removed_names)
            _invalidate_size_cache(root, ["houdini"])
        if not removed:
            return {"ok": False, "error": "no legacy entries found", "path": houdini_root}
        return {
            "ok": True,
            "path": houdini_root,
            "removed": removed,
            "bytes_freed": freed,
            "dry_run": dry_run,
            "partial": size_cache.partial,
        }

    vdir = os.path.normpath(os.path.join(root, "houdini", version))
    if vdir == houdini_root or not vdir.startswith(houdini_root + os.sep):
        raise HousekeepingError(f"resolved path escapes houdini/: {vdir}")
    if not os.path.isdir(vdir):
        return {"ok": False, "error": "not found", "path": vdir}
    freed, _ = size_cache.get(version, vdir)
    size_cache.flush()
    if not dry_run:
        shutil.rmtree(vdir)
        _prune_houdini_cache(root, [version])
        _invalidate_size_cache(root, ["houdini"])
    return {
        "ok": True,
        "path": vdir,
        "bytes_freed": freed,
        "dry_run": dry_run,
        "partial": size_cache.partial,
    }


def cmd_disk_usage(
    root: str,
    volume_size_gb=None,
    refresh: bool = False,
    max_age_s: float = _DEFAULT_MAX_AGE_S,
    budget_s: float = _DEFAULT_BUDGET_S,
) -> dict:
    """``{"volume": {"used", "total", "used_pct"[, "note"]}, "partial": bool}``.

    Ruling R26 review finding: ``maybe_grow_volume`` doesn't need ``ls``'s
    full project listing/``outputs_pending`` scanning (that scanning has
    no budget of its own -- see the Task 12 report) just to decide whether
    to grow the volume. Ruling R27: it also must never use
    ``shutil.disk_usage`` (see :func:`_volume_totals`) -- so ``used`` is
    zone-summed, from the same ``_sizes`` cache ``ls`` uses (Ruling R26),
    same staleness/``budget_s``/``partial`` semantics for every zone --
    no special-casing.

    Fix round 2 finding "A" tried treating ``houdini``/``apps``/``ledger``
    as permanently-cached "static" zones, measured fresh only with
    ``--refresh``. Fix round 3 finding "1" walked that back: it made the
    once-per-cook warm-up (``onSetupCook``'s ``_warmSizeCache``, below)
    *unconditionally* expensive too, since ``--refresh`` is the only way
    those zones are ever remeasured at all, defeating the warm-up's own
    point. Plain staleness-based caching (this function, ``ls``,
    ``houdini ls`` all now agree) gets both properties for free instead:
    a zone under ``max_age_s`` old costs nothing to any caller; a zone
    that's gone stale gets remeasured (bounded by ``budget_s``,
    ``partial: true`` if that times out) whether the caller is a per-item
    auto-grow check or the periodic warm-up. The warm-up's actual job is
    keeping zones fresh often enough that the per-item hot path's calls
    are the common, cheap case -- not preventing them from ever
    remeasuring at all.
    """
    index = _load_index(root)
    size_cache = _SizeCache(root, index, "_sizes", refresh, max_age_s, budget_s)

    used = 0
    for zone in _ZONES:
        nbytes, _ = size_cache.get(zone, os.path.join(root, zone))
        used += nbytes
    size_cache.flush()

    return {"volume": _volume_totals(used, volume_size_gb), "partial": size_cache.partial}


def cmd_sync_touch(root: str) -> dict:
    """Stamp ``.rpfarm/sync_last_used`` with the current epoch seconds.

    The **single writer** for that file. Everything that keeps the sync
    pod alive -- the scheduler HDA's ``_touchSyncPod`` and
    ``rpfarm.packages``' upload/download items -- calls this rather than
    formatting the stamp itself, because both readers
    (:func:`cmd_sync_idle` and the HDA's ``_retireStaleSyncPod``) parse
    the file's *content* as a float.

    Final-review fix: the package path used to run a bare ``touch``,
    which creates an **empty** file and never rewrites an existing one's
    content. After the README's own first-run sequence (``rpfarm houdini
    install`` creates the sync pod, and its upload item wrote the empty
    stamp) neither reader could parse it, so the pod could never be
    auto-retired -- the likely cause of the ~8h idle sync pod recorded as
    R32 -- and ``rpfarm farm status`` silently dropped its idle line.
    """
    path = os.path.join(root, _SYNC_LAST_USED_REL)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    now = time.time()
    with open(path, "w") as f:
        f.write("{:.3f}\n".format(now))
    return {"last_used": now}


def cmd_sync_idle(root: str) -> dict:
    """Seconds since ``.rpfarm/sync_last_used`` was last touched.

    Used by the scheduler (``_retireStaleSyncPod``) to decide whether to
    terminate the idle sync pod. ``idle_seconds`` is ``null`` if the
    timestamp file is missing or unreadable (pod just created, or the file
    was never written).
    """
    path = os.path.join(root, _SYNC_LAST_USED_REL)
    try:
        with open(path, "r") as f:
            raw = f.read().strip()
        last_used = float(raw)
    except (OSError, ValueError):
        return {"idle_seconds": None}
    return {"idle_seconds": max(0.0, time.time() - last_used)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="housekeeping.py")
    sub = p.add_subparsers(dest="command", required=True)

    p_ls = sub.add_parser("ls")
    p_ls.add_argument("--root", default=DEFAULT_ROOT)
    p_ls.add_argument("--refresh", action="store_true")
    p_ls.add_argument("--budget-s", type=float, default=_DEFAULT_BUDGET_S)
    p_ls.add_argument("--max-age-s", type=float, default=_DEFAULT_MAX_AGE_S)
    p_ls.add_argument("--volume-size-gb", type=float, default=None)

    p_du = sub.add_parser("du")
    p_du.add_argument("path")

    p_touch = sub.add_parser("touch")
    p_touch.add_argument("user_project")
    p_touch.add_argument("--event", choices=_TOUCH_EVENTS, default="cook")

    p_rm = sub.add_parser("rm")
    p_rm.add_argument("user_project")
    p_rm.add_argument("--force", action="store_true")

    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--older-days", type=float, default=30)
    p_prune.add_argument("--dry-run", action="store_true")

    p_houdini = sub.add_parser("houdini")
    houdini_sub = p_houdini.add_subparsers(dest="houdini_command", required=True)
    p_houdini_ls = houdini_sub.add_parser("ls")
    p_houdini_ls.add_argument("--refresh", action="store_true")
    p_houdini_rm = houdini_sub.add_parser("rm")
    p_houdini_rm.add_argument("version")
    p_houdini_rm.add_argument("--dry-run", action="store_true")

    sub.add_parser("sync-idle")
    sub.add_parser("sync-touch")
    p_disk_usage = sub.add_parser("disk-usage")
    p_disk_usage.add_argument("--volume-size-gb", type=float, default=None)
    p_disk_usage.add_argument("--budget-s", type=float, default=_DEFAULT_BUDGET_S)
    p_disk_usage.add_argument("--max-age-s", type=float, default=_DEFAULT_MAX_AGE_S)
    p_disk_usage.add_argument("--refresh", action="store_true")

    p_invalidate = sub.add_parser("invalidate")
    p_invalidate.add_argument("zone", choices=_ZONES)

    return p


def main(argv: list[str]) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as e:
        # argparse already printed usage to stderr; keep its exit code.
        return e.code if isinstance(e.code, int) else 2

    try:
        if args.command == "ls":
            result = cmd_ls(
                args.root,
                refresh=args.refresh,
                max_age_s=args.max_age_s,
                budget_s=args.budget_s,
                volume_size_gb=args.volume_size_gb,
            )
        elif args.command == "du":
            result = du(args.path)
        elif args.command == "touch":
            result = cmd_touch(DEFAULT_ROOT, args.user_project, args.event)
        elif args.command == "rm":
            result = cmd_rm(DEFAULT_ROOT, args.user_project, args.force)
        elif args.command == "prune":
            result = cmd_prune(DEFAULT_ROOT, args.older_days, args.dry_run)
        elif args.command == "houdini":
            if args.houdini_command == "ls":
                result = cmd_houdini_ls(DEFAULT_ROOT, refresh=args.refresh)
            else:
                result = cmd_houdini_rm(DEFAULT_ROOT, args.version, dry_run=args.dry_run)
        elif args.command == "sync-idle":
            result = cmd_sync_idle(DEFAULT_ROOT)
        elif args.command == "sync-touch":
            result = cmd_sync_touch(DEFAULT_ROOT)
        elif args.command == "disk-usage":
            result = cmd_disk_usage(
                DEFAULT_ROOT,
                volume_size_gb=args.volume_size_gb,
                refresh=args.refresh,
                max_age_s=args.max_age_s,
                budget_s=args.budget_s,
            )
        elif args.command == "invalidate":
            result = cmd_invalidate(DEFAULT_ROOT, args.zone)
        else:  # pragma: no cover - argparse enforces `command` is one of the above
            print(f"unknown command: {args.command}", file=sys.stderr)
            return 2
    except HousekeepingError as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"{args.command} failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
