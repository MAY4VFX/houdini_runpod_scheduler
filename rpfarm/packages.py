"""Upload package planning and execution for the ``runpodfarm_upload`` TOP node.

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
import os
import posixpath
import tempfile

from .deps import resolve_entries
from .sync import FileEntry, compress_stage, rclone_copy

DEFAULT_MAX_BYTES = int(1.5 * 2**30)

# SideFX EULA acceptance date for the Linux ``houdini.install`` silent
# installer, as fixed by the v2 design plan (docs/superpowers/plans/
# 2026-09-02-rpfarm-v2.md, Task 9). v1's installer script
# (infrastructure/install-houdini-on-volume.sh:60,72) used 2024-01-01 /
# 2025-01-01 for Houdini 20.5; this is the value the plan settled on for
# 22.0.393. The tarball is not installed against the real volume until Task
# 14 -- confirm this date against the actual installer's
# ``houdini.install --help`` output before that run, since SideFX only
# revises the EULA date when the license text itself changes.
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
    """
    roots = sorted(path_map.items(), key=lambda kv: -len(kv[0]))
    buckets = {local: [] for local, _ in roots}
    for e in entries:
        for local, _remote in roots:
            if e.local == local or e.local.startswith(local + os.sep) or e.local.startswith(local + "/"):
                buckets[local].append(e)
                break
        else:
            # Should not happen: resolve_entries always produces a path_map
            # entry covering every entry it returns. Fall back to the job
            # root (first/longest-shared entry) rather than dropping data.
            buckets[roots[0][0]].append(e)
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
            groups.append((local, remote_dir, entries))
        return _items_from_groups(groups, max_bytes)

    if mode == "deps":
        remote_project = f"/workspace/projects/{user}/{project}"
        entries, path_map = resolve_entries(refs, job_dir, remote_project)
        groups = _group_by_pathmap(entries, path_map)
        return _items_from_groups(groups, max_bytes)

    raise ValueError(f"unknown upload mode: {mode!r}")


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
    """
    tar_name = os.path.basename(tar_local_path)
    pairs = [(tar_local_path, "/workspace/apps/dist/")]
    install_dir = f"/workspace/houdini/{version}"
    post_command = (
        "cd /workspace/apps/dist && mkdir -p /tmp/hou && "
        f"tar xzf {tar_name} -C /tmp/hou && "
        f"cd /tmp/hou/houdini-{version}-* && "
        f"./houdini.install --auto-install --accept-EULA {HOUDINI_EULA_DATE} "
        "--no-install-license --no-install-menus "
        f"--install-dir {install_dir} --no-install-hfs-symlink && "
        "rm -rf /tmp/hou && "
        f"ls {install_dir}/bin/hython"
    )
    return pairs, post_command


# -- path map --------------------------------------------------------------


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

    if decompress_command:
        sync_client.exec(decompress_command)

    post_command = item.get("post_command") or ""
    if post_command:
        sync_client.exec(post_command)

    sync_client.exec("mkdir -p /workspace/.rpfarm && touch /workspace/.rpfarm/sync_last_used")

    return {"files": total_files, "bytes": total_bytes, "seconds": total_seconds}
