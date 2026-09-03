"""RunPodFarm pod housekeeping CLI.

Runs on the sync pod (the only pod with a live view of the whole Network
Volume). Tracks project usage in ``/workspace/.rpfarm/index.json`` and gives
the scheduler's Volume tab (and, later, ``rpfarm storage``) a single place
to ask "what's on the volume" and "get rid of X".

stdlib only -- deployed as-is to Ubuntu 22.04 / python3.10 pods alongside
``worker.py``, so no syntax newer than 3.10 and no third-party imports.

Commands (see ``main()``/each ``cmd_*`` docstring for exact shapes)::

    housekeeping.py ls [--root /workspace]
    housekeeping.py du <path>
    housekeeping.py touch <user>/<project> [--event cook|upload|download]
    housekeeping.py rm <user>/<project> [--force]
    housekeeping.py prune [--older-days N] [--dry-run]
    housekeeping.py houdini ls
    housekeeping.py houdini rm <version>
    housekeeping.py sync-idle

Every command prints one JSON value to stdout. On failure it prints a
one-line message to stderr and exits non-zero -- callers (``_volume_exec``
in the scheduler, ``WorkerClient.exec``) check ``exit_code``, not stdout.

Only ``rm``, ``prune`` and ``houdini rm`` are destructive, and only within
``/workspace/projects/<user>/<project>`` or ``/workspace/houdini/<version>``
respectively: ``/workspace/houdini`` (the zone itself), ``/workspace/ledger``
and ``/workspace/.rpfarm`` are never touched by them (ported from v1's
``worker/cache_manager.py::_is_protected``). The CLI hardcodes
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


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _size(path: str) -> int:
    """Total size in bytes of a file, or recursively of a directory."""
    if os.path.isdir(path) and not os.path.islink(path):
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    total += os.lstat(fp).st_size
                except OSError:
                    continue
        return total
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def du(path: str) -> list[dict]:
    """Sizes of the first-level children of ``path``. Unchanged Task 4 contract."""
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
# Commands
# ---------------------------------------------------------------------------


def cmd_ls(root: str) -> dict:
    """Zones, per-project usage, and volume totals.

    ``{"zones": {"houdini": bytes, "apps": bytes, "projects": bytes,
    "ledger": bytes}, "projects": [{"user", "project", "bytes", "last_used",
    "last_cook", "outputs_pending"}], "volume": {"used": bytes, "total":
    bytes}}``. Projects are sorted by (user, project) for a stable order.
    """
    zones = {zone: _size(os.path.join(root, zone)) for zone in _ZONES}

    index = _load_index(root)
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
                entry = index.get(f"{user}/{project}", {})
                projects.append(
                    {
                        "user": user,
                        "project": project,
                        "bytes": _size(project_dir),
                        "last_used": entry.get("last_used"),
                        "last_cook": entry.get("last_cook"),
                        "outputs_pending": _outputs_pending(
                            project_dir, entry.get("last_download")
                        ),
                    }
                )

    try:
        usage = shutil.disk_usage(root)
        volume = {"used": usage.used, "total": usage.total}
    except OSError:
        volume = {"used": 0, "total": 0}

    return {"zones": zones, "projects": projects, "volume": volume}


def cmd_touch(root: str, user_project: str, event: str = "cook") -> dict:
    """Stamp ``last_used`` (and the per-event field) for a project.

    ``touch <user>/<project> [--event cook|upload|download]``.
    """
    if event not in _TOUCH_EVENTS:
        raise HousekeepingError(f"unknown event {event!r}, expected one of {_TOUCH_EVENTS}")
    user, project = _parse_user_project(user_project)
    now = time.time()
    fields = {"last_used": now, _EVENT_FIELD[event]: now}
    entry = _update_index(root, f"{user}/{project}", fields)
    return {"ok": True, "user": user, "project": project, "event": event, **entry}


def cmd_rm(root: str, user_project: str, force: bool = False) -> dict:
    """Delete a project directory.

    Refuses (``{"ok": False, ...}``) unless ``force`` or there is nothing
    pending download, and always refuses a protected path.
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


def cmd_houdini_ls(root: str) -> dict:
    """Installed Houdini versions under ``/workspace/houdini``.

    Real ``NN.N.NNN`` version directories are listed individually; any
    other top-level entry (v1's flat legacy install: `bin/`, `houdini/`,
    loose files, ...) is summed into one ``"legacy"`` entry so it shows up
    as one cleanup unit, not dozens.
    """
    houdini_root = os.path.join(root, "houdini")
    versions = []
    legacy_bytes = 0
    have_legacy = False
    if os.path.isdir(houdini_root):
        for name in sorted(os.listdir(houdini_root)):
            if name.startswith("."):
                continue
            entry = os.path.join(houdini_root, name)
            if _VERSION_RE.match(name) and os.path.isdir(entry):
                versions.append({"version": name, "bytes": _size(entry)})
            else:
                have_legacy = True
                legacy_bytes += _size(entry)
    if have_legacy:
        versions.append({"version": LEGACY_VERSION, "bytes": legacy_bytes})
    return {"versions": versions}


def cmd_houdini_rm(root: str, version: str) -> dict:
    """Delete one installed Houdini version.

    ``version="legacy"`` removes every top-level entry under
    ``/workspace/houdini`` that is *not* a proper ``NN.N.NNN`` directory
    (v1's flat install) in one call, leaving real versions untouched --
    otherwise deletes that one version's directory.
    """
    if not version or version in (".", "..") or "/" in version or "\\" in version:
        raise HousekeepingError(f"invalid version {version!r}")
    houdini_root = os.path.normpath(os.path.join(root, "houdini"))

    if version == LEGACY_VERSION:
        if not os.path.isdir(houdini_root):
            return {"ok": False, "error": "not found", "path": houdini_root}
        removed = []
        freed = 0
        for name in sorted(os.listdir(houdini_root)):
            if name.startswith(".") or _VERSION_RE.match(name):
                continue
            entry = os.path.join(houdini_root, name)
            freed += _size(entry)
            if os.path.isdir(entry) and not os.path.islink(entry):
                shutil.rmtree(entry)
            else:
                os.remove(entry)
            removed.append(entry)
        if not removed:
            return {"ok": False, "error": "no legacy entries found", "path": houdini_root}
        return {"ok": True, "path": houdini_root, "removed": removed, "bytes_freed": freed}

    vdir = os.path.normpath(os.path.join(root, "houdini", version))
    if vdir == houdini_root or not vdir.startswith(houdini_root + os.sep):
        raise HousekeepingError(f"resolved path escapes houdini/: {vdir}")
    if not os.path.isdir(vdir):
        return {"ok": False, "error": "not found", "path": vdir}
    freed = _size(vdir)
    shutil.rmtree(vdir)
    return {"ok": True, "path": vdir, "bytes_freed": freed}


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
    houdini_sub.add_parser("ls")
    p_houdini_rm = houdini_sub.add_parser("rm")
    p_houdini_rm.add_argument("version")

    sub.add_parser("sync-idle")

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
            result = cmd_ls(args.root)
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
                result = cmd_houdini_ls(DEFAULT_ROOT)
            else:
                result = cmd_houdini_rm(DEFAULT_ROOT, args.version)
        elif args.command == "sync-idle":
            result = cmd_sync_idle(DEFAULT_ROOT)
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
