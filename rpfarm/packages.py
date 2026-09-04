"""Upload/download package planning and execution for the ``runpodfarm_upload``
and ``runpodfarm_download`` TOP nodes.

Two modes feed :func:`build_upload_items`:

- ``"deps"``: a hip file's dependencies (already collected by
  :func:`rpfarm.deps.collect_refs`) resolved against ``job_dir`` via
  :func:`rpfarm.deps.resolve_entries`. Everything under ``job_dir`` uploads
  to ``/workspace/projects/<user>/<project>``; files outside it go to that
  project's ``_ext/`` zone, one distinct local root per :mod:`rpfarm.deps`'s
  own path-map grouping.
- ``"custom"``: an explicit list of ``(local_dir_or_file, remote_dir)``
  pairs from the node's multiparm. Each pair is its own upload group,
  walked recursively when ``local`` is a directory.

Every group is (local_root, remote_root) — the pairing R8 in
:mod:`rpfarm.sync` requires the caller to keep consistent — then chunked by
byte size into one or more work-item dicts (:func:`_chunk_by_size`). This
module never groups by remote sub-directory the way
:func:`rpfarm.sync.plan_packages` does: that would change ``remote_root``
out from under a package and break R8 for nested paths, so chunking here
only ever shortens the file list, never touches the roots.

:func:`run_upload_item` is what ``onCookTask`` calls per work item: it
turns the item's file list back into :class:`rpfarm.sync.FileEntry` objects,
optionally compresses, transfers over sftp, runs any post-command on the
sync pod, and touches the sync pod's idle-timestamp file.

Nothing here imports ``hou`` — this module is pure stdlib plus
:mod:`rpfarm.sync`, :mod:`rpfarm.deps` and :mod:`rpfarm.compression`, and is
fully testable without Houdini installed.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import tempfile
import time

from .deps import resolve_entries
from .sync import FileEntry, SyncStats, compress_stage, rclone_copy

DEFAULT_MAX_BYTES = int(1.5 * 2**30)

# SideFX EULA acceptance date for the Linux ``houdini.install`` silent
# installer, as fixed by the v2 design plan (docs/superpowers/plans/
# 2026-09-02-rpfarm-v2.md, Task 9). v1's installer script
# (infrastructure/install-houdini-on-volume.sh:60,72) used 2024-01-01 /
# 2025-01-01 for Houdini 20.5; this is the value the plan settled on for
# 22.0.393. Verified in Task 14 against the real 22.0.393 installer:
# ``houdini.install`` compares ``--accept-EULA`` against its own
# ``LICENSE_DATE="2021-10-13"`` (line 1501; the previous date 2020-05-05
# expired 2021-11-12), so this is exactly the string it accepts. SideFX
# only revises the date when the license text itself changes, so recheck
# it when moving to a new Houdini major.
HOUDINI_EULA_DATE = "2021-10-13"


# -- package planning ---------------------------------------------------------


def _chunk_by_size(entries, max_bytes):
    """Split ``entries`` (already sorted by the caller) into chunks no
    bigger than ``max_bytes``, except a single entry that alone exceeds it
    (which always gets its own chunk). Unlike
    :func:`rpfarm.sync.plan_packages`, this never regroups by directory --
    the caller has already fixed one ``(local_root, remote_root)`` pair for
    the whole list, and chunking must not change that.
    """
    chunks = []
    cur, cur_size = [], 0
    for e in entries:
        if e.size > max_bytes:
            if cur:
                chunks.append(cur)
                cur, cur_size = [], 0
            chunks.append([e])
            continue
        if cur and cur_size + e.size > max_bytes:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(e)
        cur_size += e.size
    if cur:
        chunks.append(cur)
    return chunks


def _walk_custom_pair(local, remote_dir):
    """Yield FileEntry objects for one ``(local, remote_dir)`` custom pair.

    ``local`` may be a file (single-entry group) or a directory (walked
    recursively, sub-directories preserved under ``remote_dir``).
    """
    if os.path.isfile(local):
        remote = posixpath.join(remote_dir, os.path.basename(local))
        yield FileEntry(local=local, remote=remote, size=os.path.getsize(local))
        return

    for root, _dirs, files in os.walk(local, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, local).replace(os.sep, "/")
            remote = posixpath.join(remote_dir, rel)
            yield FileEntry(local=fp, remote=remote, size=os.path.getsize(fp))


def _items_from_groups(groups, max_bytes):
    """Turn an ordered list of ``(local_root, remote_root, [FileEntry])``
    groups into the work-item dict list :func:`build_upload_items` returns.
    """
    items = []
    for local_root, remote_root, entries in groups:
        entries = sorted(entries, key=lambda e: e.remote)
        for chunk in _chunk_by_size(entries, max_bytes):
            items.append(
                {
                    "index": len(items),
                    "local_root": local_root,
                    "remote_root": remote_root,
                    "files": [[e.local, e.remote, e.size] for e in chunk],
                    "bytes": sum(e.size for e in chunk),
                    "post_command": "",
                }
            )
    return items


def _group_by_pathmap(entries, path_map):
    """Assign each entry to the ``(local_root, remote_root)`` from
    ``path_map`` whose local prefix it falls under (longest match wins).

    Two passes, matching :func:`rpfarm.deps.resolve_entries`'s own
    inside/outside logic: a literal prefix check first, then -- because a
    symlinked ``$JOB`` (or any symlinked root) means a real file's
    ``FileEntry.local`` need not literally start with any ``path_map`` key
    even though it resolves under one -- a ``os.path.realpath`` comparison
    for anything the literal check misses. An entry that matches neither
    is a real bug upstream (``resolve_entries`` is supposed to produce a
    ``path_map`` entry covering everything it returns), so this raises
    rather than silently filing it under an arbitrary root.
    """
    roots = sorted(path_map.items(), key=lambda kv: -len(kv[0]))
    real_roots = [(local, os.path.realpath(local)) for local, _ in roots]
    buckets = {local: [] for local, _ in roots}

    for e in entries:
        matched = None
        for local, _remote in roots:
            if e.local == local or e.local.startswith(local + os.sep) or e.local.startswith(local + "/"):
                matched = local
                break
        if matched is None:
            e_real = os.path.realpath(e.local)
            for local, local_real in real_roots:
                if e_real == local_real or e_real.startswith(local_real + os.sep):
                    matched = local
                    break
        if matched is None:
            raise ValueError(
                "entry {!r} is not under any path_map root {}".format(e.local, [r for r, _ in roots])
            )
        buckets[matched].append(e)

    return [(local, path_map[local], es) for local, es in buckets.items() if es]


def build_upload_items(mode, job_dir, user, project, custom, refs, package_gb):
    """Plan the work items for one ``runpodfarm_upload`` cook.

    ``mode`` is ``"deps"`` (uses ``refs`` via
    :func:`rpfarm.deps.resolve_entries`) or ``"custom"`` (uses ``custom``,
    a list of ``(local, remote_dir)`` pairs; ``refs`` is ignored). Returns a
    list of dicts: ``{"index", "local_root", "remote_root", "files":
    [[local, remote, size], ...], "bytes", "post_command": ""}`` --
    ``post_command`` is always empty here; the caller (the HDA's
    ``onGenerate``) decides which item(s), if any, carry a post-command per
    Ruling R3 (run once, after all packages).
    """
    max_bytes = max(1, int(package_gb * 2**30))

    if mode == "custom":
        groups = []
        for local, remote_dir in custom:
            entries = list(_walk_custom_pair(local, remote_dir))
            # R8: local_root/rel and remote_root/rel must be the same rel.
            # _walk_custom_pair's remote for a single FILE is
            # remote_dir/basename(local); relpath(local, local) is "." --
            # not basename(local) -- so local_root has to be the file's
            # own containing directory here, not the file itself. For a
            # directory pair, local IS already the walk root, so it stays.
            local_root = os.path.dirname(local) if os.path.isfile(local) else local
            groups.append((local_root, remote_dir, entries))
        return _items_from_groups(groups, max_bytes)

    if mode == "deps":
        remote_project = f"/workspace/projects/{user}/{project}"
        entries, path_map = resolve_entries(refs, job_dir, remote_project)
        groups = _group_by_pathmap(entries, path_map)
        return _items_from_groups(groups, max_bytes)

    raise ValueError(f"unknown upload mode: {mode!r}")


# -- download package planning ------------------------------------------------

# Task 10 (runpodfarm_download): the mirror image of build_upload_items, but
# every entry a download plans already names its own local AND remote file --
# there is no job_dir/refs walk to do here, because:
#
# - "outputs" mode: the HDA's onGenerate reads each upstream work item's
#   resultData (farm paths) and localizes them via the rpfarm_pathmap
#   attribute the scheduler tags onto work items (_tagPathMap, hda/
#   runpodfarm_scheduler.hda/.../PythonModule), producing (remote, local)
#   pairs directly.
# - "custom" mode: the HDA's onGenerate turns each rpfarm_remote#/rpfarm_local#
#   multiparm row (a *directory* pair) into file-level (remote, local) pairs
#   by listing the remote directory on the sync pod (``find <dir> -type f
#   -printf '%s %p\n'``), which is also where per-file sizes for this mode
#   come from.
#
# So by the time build_download_items runs, "mode" no longer changes how
# pairs are grouped into work items -- it is accepted (and validated) purely
# so a typo'd mode fails the same way build_upload_items' does, rather than
# being silently accepted and producing a possibly-nonsensical plan.


def group_download_pairs(pairs, sizes=None):
    """Group ``(remote, local)`` download pairs into ``(local_root,
    remote_root, [FileEntry])`` groups by ``(dirname(local),
    dirname(remote))`` -- R8 for downloads, the same directory-pair
    invariant :func:`rpfarm.sync.build_rclone_args` enforces at transfer
    time.

    This is the exact grouping ``runpodfarm_scheduler``'s own
    ``_download_outputs`` (hda/runpodfarm_scheduler.hda/.../PythonModule)
    used inline before Task 10 extracted it here; the scheduler now calls
    this too (its per-item auto-download path) so it and the
    ``runpodfarm_download`` node's planning can't drift apart.

    ``sizes`` is an optional ``{remote: size}`` map; a remote missing from
    it (or ``sizes=None`` entirely) gets size ``0`` -- the scheduler's
    auto-download path never looks sizes up (its packages are one item's
    outputs, already small in practice), only the download node's own
    ``outputs``/``custom`` planning bothers to stat first.

    Order is dict-insertion order (first pair seen for a given directory
    pair fixes its position), so the result is deterministic for a given
    ``pairs`` list.
    """
    sizes = sizes or {}
    groups = {}
    for remote, local in pairs:
        key = (os.path.dirname(local), posixpath.dirname(remote))
        groups.setdefault(key, []).append(FileEntry(local=local, remote=remote, size=sizes.get(remote, 0)))
    return [(local_root, remote_root, entries) for (local_root, remote_root), entries in groups.items()]


def build_download_items(mode, pairs, package_gb, sizes=None):
    """Plan the work items for one ``runpodfarm_download`` cook.

    ``pairs`` is a list of ``(remote, local)`` file pairs -- already fully
    resolved by the caller for both ``"outputs"`` and ``"custom"`` modes
    (see the module-level note above); this function only groups them (via
    :func:`group_download_pairs`) and chunks each group by
    ``package_gb`` (:func:`_chunk_by_size`, shared with
    :func:`build_upload_items`). Returns the same work-item dict shape as
    :func:`build_upload_items`: ``{"index", "local_root", "remote_root",
    "files": [[local, remote, size], ...], "bytes", "post_command": ""}`` --
    ``post_command`` is always empty (downloads never run a remote
    decompress/post step; that is an upload-only concern, Ruling R10).
    """
    if mode not in ("outputs", "custom"):
        raise ValueError(f"unknown download mode: {mode!r}")

    max_bytes = max(1, int(package_gb * 2**30))
    groups = group_download_pairs(pairs, sizes)
    return _items_from_groups(groups, max_bytes)


# -- compression toggle --------------------------------------------------------

# Uplink threshold (Mbps) below which "auto" enables compression, per the
# design spec (4.3): "по умолчанию включена для uplink < 200 Мбит/с
# (измеряется в doctor)". Without a measurement (``measured_mbps=None``),
# auto defaults to on -- the safer choice for an unknown/likely-slow
# residential uplink.
AUTO_COMPRESS_THRESHOLD_MBPS = 200


def resolve_compress_flag(mode, measured_mbps=None):
    """Turn the node's Compression parm (``on``/``off``/``auto``) into a bool."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    if mode == "auto":
        if measured_mbps is None:
            return True
        return measured_mbps < AUTO_COMPRESS_THRESHOLD_MBPS
    raise ValueError(f"unknown compress mode: {mode!r}")


# -- Houdini install preset ----------------------------------------------------


def houdini_install_preset(tar_local_path, version):
    """Custom pairs + post-command for the "Install Houdini from tarball" preset.

    The tarball uploads to ``/workspace/apps/dist/`` under its own
    basename; the post-command (run once, after the upload, on the sync
    pod) extracts it to a scratch dir, runs SideFX's silent installer into
    ``/workspace/houdini/<version>``, and cleans the scratch dir up. The
    final ``ls`` both proves the binary landed and gives a work item a
    non-empty stdout to show for the whole preset.

    Review finding (fix round 3, "2"): an install adds ~10GB to the
    `houdini` zone, but nothing invalidated `_sizes["houdini"]` -- a
    stale-but-present cache entry doesn't set `partial` either, so the
    85%-full auto-grow guard would keep comparing against the pre-install
    figure with no signal anything was wrong. The last step now
    invalidates that zone (``housekeeping.py invalidate houdini``) right
    where the write happens, so the next `ls`/`disk-usage` remeasures it.

    Task 14 (first real run of this preset) corrected the installer
    invocation against the actual 22.0.393 ``houdini.install``:

    - There is no ``--install-dir`` option. ``houdini.install``'s usage is
      ``./houdini.install [options] [installation_directory]`` -- the
      target is a *positional* argument, and its option loop ``break``s on
      the first unrecognised token, so ``--install-dir /path`` was parsed
      as the directory itself and died with "Error: --install-dir must be
      a writeable directory". It must be the last word, with ``--make-dir``
      so a fresh ``/workspace/houdini/<ver>`` is created rather than
      rejected.
    - ``--no-install-avahi`` is required: run as root (which the pod is),
      the installer defaults ``avahi=yes`` and shells out to
      ``apt-get install avahi-daemon`` -- a pointless network install on a
      throwaway pod.
    - ``--no-install-bin-symlink`` pins the already-correct default so a
      future installer flip can't start writing into ``/usr/local/bin``.

    The uploaded tarball (~4.3GB for 22.0) is deleted from
    ``/workspace/apps/dist/`` once the install succeeds: it is dead weight
    on a 50GB volume, and a failed install deliberately keeps it so a
    retry does not have to re-upload.
    """
    tar_name = os.path.basename(tar_local_path)
    pairs = [(tar_local_path, "/workspace/apps/dist/")]
    install_dir = f"/workspace/houdini/{version}"
    post_command = (
        "cd /workspace/apps/dist && mkdir -p /tmp/hou && "
        f"tar xzf {tar_name} -C /tmp/hou && "
        f"cd /tmp/hou/houdini-{version}-* && "
        f"./houdini.install --auto-install --accept-EULA {HOUDINI_EULA_DATE} "
        "--no-install-license --no-install-menus --no-install-avahi "
        "--no-install-hfs-symlink --no-install-bin-symlink --make-dir "
        f"{install_dir} && "
        "rm -rf /tmp/hou && "
        f"rm -f /workspace/apps/dist/{tar_name} && "
        "python3 /opt/rpfarm/housekeeping.py invalidate houdini && "
        f"ls {install_dir}/bin/hython"
    )
    return pairs, post_command


# -- remote command execution -----------------------------------------------

# Timeout for the decompress/post-command exec() calls, scaled from the
# item's own byte size rather than left at exec()'s 600s default -- the
# addendum notes unpacking tens of GB takes minutes, and the default alone
# doesn't say so. A floor keeps a small package from being cut off by a
# slow pod; a ceiling keeps a genuinely stuck remote command from hanging
# a work item forever. The synthetic R3 "post" item always has bytes=0 (it
# carries no files of its own -- see build_upload_items' caller in the
# HDA's onGenerate), so a post-command that runs on it -- the Houdini
# install preset today -- always gets the floor; that has been enough in
# practice (design spec 3.2: a cold hython start alone was ~104s, and the
# installer itself is mostly a filesystem copy, not a multi-GB unpack).
_EXEC_TIMEOUT_FLOOR_S = 600
_EXEC_TIMEOUT_CEILING_S = 3 * 3600
_EXEC_BYTES_PER_SECOND = 5 * 2**20  # ~5 MB/s: a conservative pod-side IO/decompress rate


def _scaled_timeout(num_bytes):
    return int(min(_EXEC_TIMEOUT_CEILING_S, max(_EXEC_TIMEOUT_FLOOR_S, (num_bytes or 0) / _EXEC_BYTES_PER_SECOND)))


_PROJECTS_PREFIX = "/workspace/projects/"


def _project_key_from_remote_root(remote_root):
    """Best-effort ``<user>/<project>`` from a package's ``remote_root``.

    ``None`` if ``remote_root`` isn't under ``/workspace/projects/`` (a
    Houdini install, an ``apps/`` custom upload, ...) -- those have no
    project index entry to touch.
    """
    if not remote_root.startswith(_PROJECTS_PREFIX):
        return None
    rel = remote_root[len(_PROJECTS_PREFIX):].strip("/")
    parts = rel.split("/", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return "{}/{}".format(parts[0], parts[1])


def _shquote(value):
    """POSIX single-quote for interpolation into a shell command string
    sent to ``sync_client.exec`` (the pod runs these through a shell).
    Review finding (Ruling R26 fix round): ``key`` here is a project name,
    artist-controlled (the node's Project parm, or a job dir basename) --
    unquoted string interpolation into a shell command is injection, not
    just a style nit. Mirrors the scheduler HDA's own ``_shquote``
    (``hda/runpodfarm_scheduler.hda/.../PythonModule``) -- kept as an
    independent copy rather than a cross-import: trivial, stable POSIX
    quoting, and the HDA's python module isn't something this package can
    import from anyway.
    """
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _touch_project_index(sync_client, remote_root, event):
    """Best-effort ``housekeeping.py touch`` for the project index.

    Fire-and-forget like the ``sync_last_used`` touch beside every call
    site: an index update is bookkeeping for the Volume tab and ``prune``,
    never something that should fail a transfer that otherwise succeeded.
    """
    key = _project_key_from_remote_root(remote_root)
    if key is None:
        return
    sync_client.exec(
        "python3 /opt/rpfarm/housekeeping.py touch {} --event {}".format(_shquote(key), event)
    )


# Spec 4.1: "Ресайз вверх автоматический: при заполнении > 85% перед
# заливкой шедулер/upload увеличивает volume на нужный объём (с
# округлением до 10 ГБ) и пишет об этом в лог ноды." RunPod volumes only
# grow, never shrink, so this must run *before* the transfer that would
# push usage over the line, not after.
_AUTO_GROW_THRESHOLD = 0.85
_AUTO_GROW_TARGET_FRACTION = 0.8
_AUTO_GROW_STEP_GB = 10
_GB = 2**30
# Ruling R27 review finding: this used to trust `disk-usage`'s (or,
# before that, `ls`'s) `shutil.disk_usage(root)`-derived "total" -- which
# is the *backing storage pool's* capacity from inside a pod, not the
# RunPod network volume's own provisioned size (confirmed live: a real
# 50GB volume read back as ~2.14 PiB). The pod has no RunPod API key and
# cannot look up the real size itself, so the caller must supply it.
# `--volume-size-gb` makes `disk-usage` compute against that instead of
# ever touching `shutil.disk_usage`; `used` still comes from the same
# zone/`du -sb` numbers `ls` uses (cached, Ruling R26), never a syscall
# that reports the wrong thing for the same reason `total` did.
#
# Fix round 2 finding "A": `disk-usage` used to sum all four zones, so a
# stale cache meant walking `houdini` too -- ~30s cold on the real farm's
# legacy install, close enough to the old flat 60s exec timeout that the
# transport could still kill the whole process before `disk-usage`'s own
# bounded remeasure gave up gracefully, reproducing the exact silent-skip
# this call was built to avoid. `disk-usage` now only ever walks
# `projects/` fresh (the one zone an upload can change; `houdini`/`apps`/
# `ledger` are served from cache however stale, warmed off this path by
# the scheduler's `onSetupCook`) -- `du -sb /workspace/projects` measured
# 1.24s cold on the real farm's ~150MB of projects. `_AUTO_GROW_LS_BUDGET_S`
# leaves an order of magnitude of headroom over that as `projects/` grows
# over a cook's lifetime; the exec timeout leaves further margin over the
# internal budget for process startup and network round-trip.
_AUTO_GROW_LS_BUDGET_S = 20.0
_AUTO_GROW_EXEC_TIMEOUT_S = 30.0


def _volume_size_cache_path():
    """``~/.rpfarm/volume_size_cache.json``.

    Known residual (reviewer-noted, parked -- not fixed): reads and
    writes here are plain, unlocked file I/O -- unlike
    ``pod/housekeeping.py``'s ``_sizes``/``_houdini`` index cache (on the
    pod, ``flock``-protected), this file lives on the artist's own
    machine and is read/written by potentially-concurrent
    ``package_runner.py`` processes with no lock at all. A lost update
    here only means an extra, harmless RunPod ``get_volume`` call on the
    next lookup (worst case) -- never a wrong size served silently
    (a torn/corrupt read falls back to `{}`, i.e. "uncached", not a bad
    value). Task 13 inherits this as a known limitation.
    """
    from . import config as rpcfg  # local: packages.py has no other rpcfg dependency

    return rpcfg.home() / "volume_size_cache.json"


def _load_volume_size_cache():
    try:
        with open(_volume_size_cache_path()) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def set_volume_size_gb(volume_id, size_gb, when=None):
    """Write ``volume_id``'s size straight into the cache (no API call).

    Used right after a successful ``resize_volume`` (fix round 2, "D"):
    the new size is already known -- caching it immediately means the
    *next* upload item's ``get_volume_size_gb`` doesn't need another
    RunPod round-trip, and, more importantly, doesn't keep deciding
    against the pre-resize figure for up to 5 minutes and re-issuing an
    already-satisfied resize.
    """
    cache = _load_volume_size_cache()
    cache[volume_id] = {"size_gb": size_gb, "cached_at": time.time() if when is None else when}
    try:
        path = _volume_size_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def get_volume_size_gb(api, cfg, max_age_s=300.0):
    """The volume's real provisioned size in GB, from RunPod's own API.

    Cached in ``~/.rpfarm/volume_size_cache.json`` (per ``cfg.volume_id``,
    ``max_age_s`` default 5 min -- a resize is a rare, explicit action,
    not something worth polling every item) because ``maybe_grow_volume``
    is called once per upload item, and each item is its own
    ``package_runner.py`` process (Ruling R27 review: "cache the API
    answer per session" has to mean a file, not an in-process dict, or it
    would never actually be reused). Never raises: a failed lookup falls
    back to a stale cached value if there is one, else returns ``None`` --
    :func:`maybe_grow_volume` already treats an unknown size as "skip",
    not an error.
    """
    cache = _load_volume_size_cache()
    now = time.time()
    entry = cache.get(cfg.volume_id)
    if entry and (now - entry.get("cached_at", 0)) < max_age_s:
        return entry.get("size_gb")

    try:
        size_gb = (api.get_volume(cfg.volume_id) or {}).get("size")
    except Exception:  # noqa: BLE001 - RunPod control-plane hiccup, not fatal here
        size_gb = None

    if not size_gb:
        return entry.get("size_gb") if entry else None

    set_volume_size_gb(cfg.volume_id, size_gb, when=now)
    return size_gb


def maybe_grow_volume(api, cfg, sync_client, needed_bytes, log=None) -> str:
    """Grow the network volume when an upload would push it past 85% full.

    Called by ``rpfarm.package_runner`` right before ``run_upload_item``
    (only for uploads -- downloads free no space and need no headroom).
    Sizes the new volume so it lands at ~80% full afterwards, rounded up
    to the nearest 10 GB step (spec 4.1's formula:
    ``ceil((used+bytes)/0.8/10GB)*10``). Looks up the volume's real size
    via :func:`get_volume_size_gb` (RunPod's own API, cached -- the pod
    cannot do this itself) and passes it to housekeeping's ``disk-usage``
    as ``--volume-size-gb`` so ``used``/``total`` are never derived from
    ``shutil.disk_usage`` (Ruling R27).

    Never raises: a failed size check or resize must not block an upload
    that would otherwise succeed on its own. Returns a short status string
    (``"ok"``, ``"ok (partial data)"``, ``"grown to N GB"``,
    ``"skipped: ..."``, ``"error: ..."``) -- callers are expected to
    surface anything other than plain ``"ok"`` somewhere the artist will
    actually see it (package_runner.py sets a pdgcmd attribute), not just
    this function's own ``log()`` line, so a silently-disabled or
    partial-data guard doesn't stay silent.
    """
    log = log or (lambda msg: None)
    try:
        volume_size_gb = get_volume_size_gb(api, cfg)
        if not volume_size_gb:
            note = "skipped: volume size unknown (RunPod get_volume unavailable)"
            log("volume auto-grow check " + note)
            return note

        result = sync_client.exec(
            "python3 /opt/rpfarm/housekeeping.py disk-usage --volume-size-gb {} --budget-s {}".format(
                volume_size_gb, int(_AUTO_GROW_LS_BUDGET_S)
            ),
            timeout_s=_AUTO_GROW_EXEC_TIMEOUT_S,
        )
        if result.get("exit_code") != 0:
            note = "skipped: {}".format(result.get("stderr") or "disk-usage failed")
            log("volume auto-grow check " + note)
            return note
        info = json.loads(result.get("stdout") or "{}")
        volume = info.get("volume") or {}
        used = int(volume.get("used") or 0)
        total = int(volume.get("total") or 0)
        partial_note = " (partial data)" if info.get("partial") else ""
        if info.get("partial"):
            log("volume auto-grow check: disk-usage returned partial (still-warming cache) "
                "sizes -- deciding from those anyway")
        if total <= 0:
            note = "skipped: disk-usage returned no total"
            log("volume auto-grow check " + note)
            return note
        if used + needed_bytes <= _AUTO_GROW_THRESHOLD * total:
            return "ok" + partial_note
        new_size_gb = (
            math.ceil((used + needed_bytes) / _AUTO_GROW_TARGET_FRACTION / (_AUTO_GROW_STEP_GB * _GB))
            * _AUTO_GROW_STEP_GB
        )
        current_gb = int(volume_size_gb)
        if new_size_gb <= current_gb:
            return "ok" + partial_note
        api.resize_volume(cfg.volume_id, new_size_gb)
        # Fix round 2, "D": cache the new size immediately -- otherwise
        # the next item, within the 5-minute window, still sees the
        # pre-resize figure and can re-issue an already-satisfied resize.
        set_volume_size_gb(cfg.volume_id, new_size_gb)
        note = "grown to {} GB".format(new_size_gb) + partial_note
        log(
            "volume {} auto-grown {} GB -> {} GB ({:.0f}% full before this upload)".format(
                cfg.volume_id, current_gb, new_size_gb, 100.0 * (used + needed_bytes) / total
            )
        )
        return note
    except Exception as e:  # noqa: BLE001 - background safety net, never fails the upload
        note = "error: {}".format(e)
        log("volume auto-grow check failed (continuing): {}".format(e))
        return note


def _exec_checked(sync_client, command, timeout_s):
    """Run one long-running command on the sync pod; raise RuntimeError on
    non-zero exit.

    Same contract as the scheduler's own ``_volume_exec`` (``hda/
    runpodfarm_scheduler.hda/.../PythonModule``, ``_volume_exec``): a
    failed remote command must never be swallowed as a successful work
    item. Only the tail of stderr is kept in the message -- a failed
    Houdini install can log megabytes.

    Goes through ``exec_wait`` (detached + polling), not the synchronous
    ``exec`` -- Ruling R31. The commands reaching here are decompressing
    tens of GB or running SideFX's installer, both far past the ~100s the
    RunPod proxy allows a response to take, and the old synchronous call
    did not merely time out: it SIGKILLed the shell and closed its stdout
    pipe, which let a grandchild keep running and then die of SIGPIPE
    mid-install. ``timeout_s`` is now the deadline for *watching*, so
    running past it is reported without touching the command itself.
    """
    if hasattr(sync_client, "exec_wait"):
        result = sync_client.exec_wait(command, deadline_s=timeout_s)
    else:  # pragma: no cover - older client stub in a caller's test
        result = sync_client.exec(command, timeout_s=timeout_s)
    if result.get("exit_code") != 0:
        stderr = (result.get("stderr") or "").strip()
        tail = stderr[-2000:] if stderr else "(no stderr)"
        raise RuntimeError(
            "remote command failed (exit {}): {}\ncommand: {}".format(result.get("exit_code"), tail, command)
        )
    return result


# -- path map --------------------------------------------------------------


def localize_via_pathmap(remote, path_map):
    """Turn a farm path back into a local one using a ``{local prefix: farm
    prefix}`` map -- the same shape :func:`write_pathmap`/the scheduler's
    ``_tagPathMap`` produce (``hda/runpodfarm_scheduler.hda/.../
    PythonModule``). This is the inverse direction of what
    :func:`build_upload_items`'s ``_group_by_pathmap`` does, and is a
    self-contained equivalent of PDG's own ``localizePath`` -- used by the
    ``runpodfarm_download`` node (Task 10) so it never needs a handle on
    the scheduler node itself, only the ``rpfarm_pathmap`` attribute the
    scheduler already stamps onto every work item it schedules.

    The longest matching farm prefix wins (mirrors ``_group_by_pathmap``'s
    longest-local-prefix-wins rule for the opposite direction). Returns
    ``None`` if no entry's farm prefix is a prefix of ``remote`` -- callers
    treat that the same way the scheduler's own ``_download_outputs``
    treats an unmapped ``localizePath`` result: nothing to pull it to.
    """
    best = None
    for local, farm in path_map.items():
        if remote == farm or remote.startswith(farm.rstrip("/") + "/"):
            if best is None or len(farm) > len(best[1]):
                best = (local, farm)
    if best is None:
        return None
    local_prefix, farm_prefix = best
    rel = remote[len(farm_prefix):].lstrip("/")
    return posixpath.join(local_prefix, rel) if rel else local_prefix


def write_pathmap(job_dir, path_map):
    """Write ``$JOB/.rpfarm_pathmap.json`` in the format the scheduler's
    ``_loadPathMap`` expects: a plain ``{local prefix: farm prefix}`` JSON
    object (see ``hda/runpodfarm_scheduler.hda/.../PythonModule``'s
    ``_loadPathMap``, which merges this file's entries into its own map).
    """
    path = os.path.join(job_dir, ".rpfarm_pathmap.json")
    with open(path, "w") as f:
        json.dump(path_map, f)
    return path


# -- execution ------------------------------------------------------------


def run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb=None):
    """Transfer one work item's package and run any post-command.

    ``sftp`` is a :class:`rpfarm.sync.SftpTarget` for the ``rclone``
    transfer; ``sync_client`` is a :class:`rpfarm.worker_client.WorkerClient`
    (or any object with a matching ``.exec(command)``) used for
    post-commands and the idle-timestamp touch on the sync pod.

    When ``compress`` is true, :func:`rpfarm.sync.compress_stage` splits
    the package into a raw half (uploaded as-is) and a staged half
    (compressed, uploaded from a temp staging dir, then decompressed on the
    sync pod via the returned decompress command) -- see its docstring for
    why two transfers are needed rather than one. Either half may be empty;
    an empty package is simply not transferred.

    ``item["post_command"]`` (the caller-assigned, once-per-cook
    post-command -- the Houdini install step, or the node's own
    Post-command parm) runs after the transfer and after any decompress
    step, when non-empty.

    Returns ``{"files", "bytes", "seconds"}`` summed across every transfer
    this call made.
    """
    entries = [FileEntry(local=local, remote=remote, size=size) for local, remote, size in item["files"]]
    local_root = item["local_root"]
    remote_root = item["remote_root"]

    total_files = 0
    total_bytes = 0
    total_seconds = 0.0
    decompress_command = ""

    def _accumulate(stats):
        nonlocal total_files, total_bytes, total_seconds
        total_files += stats.files
        total_bytes += stats.bytes
        total_seconds += stats.seconds

    if compress:
        with tempfile.TemporaryDirectory() as staging_dir:
            raw_package, staged_package, decompress_command = compress_stage(entries, staging_dir, remote_root)
            if raw_package:
                _accumulate(
                    rclone_copy(raw_package, sftp, "up", cfg.rclone_path, local_root, remote_root, progress_cb=progress_cb)
                )
            if staged_package:
                _accumulate(
                    rclone_copy(
                        staged_package, sftp, "up", cfg.rclone_path, staging_dir, remote_root, progress_cb=progress_cb
                    )
                )
    else:
        if entries:
            _accumulate(rclone_copy(entries, sftp, "up", cfg.rclone_path, local_root, remote_root, progress_cb=progress_cb))

    timeout_s = _scaled_timeout(item.get("bytes"))

    if decompress_command:
        _exec_checked(sync_client, decompress_command, timeout_s)

    post_command = item.get("post_command") or ""
    if post_command:
        _exec_checked(sync_client, post_command, timeout_s)

    sync_client.exec("mkdir -p /workspace/.rpfarm && touch /workspace/.rpfarm/sync_last_used")
    _touch_project_index(sync_client, remote_root, "upload")

    return {"files": total_files, "bytes": total_bytes, "seconds": total_seconds}


# Ruling R8/download: which rclone flag each ``rpfarm_overwrite`` setting
# maps to. "newer" skips a local file that is not older than the remote
# (rclone's own mtime-based --update); "always" adds nothing (the default:
# always re-transfer); "never" skips anything that already exists locally,
# regardless of mtime.
_OVERWRITE_EXTRA_ARGS = {
    "newer": ("--update",),
    "always": (),
    "never": ("--ignore-existing",),
}


def run_download_item(item, cfg, sftp, sync_client, overwrite, progress_cb=None):
    """Transfer one work item's package down from the farm.

    Mirrors :func:`run_upload_item` for ``direction="down"``, with two
    differences: there is no compression stage (staging/decompression is an
    upload-only concern -- Ruling R10's ``compress_stage`` splits a package
    for the *upload* side specifically, and a download's package is already
    exactly the files to fetch), and ``overwrite`` selects one of
    :data:`_OVERWRITE_EXTRA_ARGS` instead of a post-command.

    ``sync_client`` is used only to touch the sync pod's idle-timestamp file
    (same as ``run_upload_item`` -- a long download is real activity that
    must not let the pod look idle mid-transfer).

    Returns ``{"files", "bytes", "seconds"}``.
    """
    if overwrite not in _OVERWRITE_EXTRA_ARGS:
        raise ValueError(f"unknown overwrite mode: {overwrite!r}")

    entries = [FileEntry(local=local, remote=remote, size=size) for local, remote, size in item["files"]]
    local_root = item["local_root"]
    remote_root = item["remote_root"]

    if entries:
        os.makedirs(local_root, exist_ok=True)
        stats = rclone_copy(
            entries,
            sftp,
            "down",
            cfg.rclone_path,
            local_root,
            remote_root,
            progress_cb=progress_cb,
            extra_args=_OVERWRITE_EXTRA_ARGS[overwrite],
        )
    else:
        stats = SyncStats(files=0, bytes=0, seconds=0.0)

    sync_client.exec("mkdir -p /workspace/.rpfarm && touch /workspace/.rpfarm/sync_last_used")
    _touch_project_index(sync_client, remote_root, "download")

    return {"files": stats.files, "bytes": stats.bytes, "seconds": stats.seconds}
