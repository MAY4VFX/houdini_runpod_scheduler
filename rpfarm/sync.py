"""Package planning and rclone-over-sftp file sync to/from the RunPod farm.

Stdlib only (runs inside Houdini's bundled Python and as a plain CLI). All
network I/O goes through the ``rclone`` binary as a subprocess; nothing here
talks HTTP/SFTP directly.

Two ways to move files:

- ``rclone_copy`` / ``build_rclone_args``: an explicit list of
  :class:`FileEntry` objects, grouped into packages by :func:`plan_packages`,
  transferred with ``rclone copy --files-from``.
- ``rclone_copy_dir`` / ``build_rclone_dir_args``: a plain ``rclone copy`` of
  a whole local directory, for callers that don't need per-file bookkeeping
  (e.g. the sync pod's own idle-sync loop).

Both share one private subprocess runner (``_run_rclone``) that parses
rclone's ``--use-json-log`` stderr stream for progress and raises
:class:`SyncError` on a non-zero exit.
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass

from rpfarm.compression import CompressionStrategy, classify_file, compress_file

DEFAULT_MAX_BYTES = int(1.5 * 2**30)

_COMMON_RCLONE_FLAGS = (
    "--sftp-set-modtime",
    "--transfers",
    "4",
    "--checkers",
    "8",
    "--stats",
    "1s",
    "--use-json-log",
    "--stats-log-level",
    "NOTICE",
)


class SyncError(Exception):
    """Raised when the rclone subprocess exits non-zero."""


@dataclass
class FileEntry:
    local: str
    remote: str  # full path on the remote side, e.g. "/workspace/projects/<user>/<project>/..."
    size: int


@dataclass
class SftpTarget:
    host: str
    port: int
    key_path: str
    user: str = "root"


@dataclass
class SyncStats:
    files: int
    bytes: int
    seconds: float


# -- package planning ---------------------------------------------------------


def plan_packages(entries, max_bytes=DEFAULT_MAX_BYTES):
    """Group entries by remote directory, then pack by size within each dir.

    Packages never span two directories, and never exceed ``max_bytes``
    except for a single file that is itself bigger than the limit (which
    always gets its own package). Directories are visited in sorted
    (remote-path) order, so the result is deterministic.
    """
    by_dir = {}
    for e in sorted(entries, key=lambda e: e.remote):
        by_dir.setdefault(os.path.dirname(e.remote), []).append(e)

    packages = []
    for d in sorted(by_dir):
        cur, cur_size = [], 0
        for e in by_dir[d]:
            if e.size > max_bytes:
                if cur:
                    packages.append(cur)
                    cur, cur_size = [], 0
                packages.append([e])
                continue
            if cur and cur_size + e.size > max_bytes:
                packages.append(cur)
                cur, cur_size = [], 0
            cur.append(e)
            cur_size += e.size
        if cur:
            packages.append(cur)
    return packages


# -- rclone argument construction ---------------------------------------------


def _sftp_flags(target: SftpTarget):
    return [
        f"--sftp-host={target.host}",
        f"--sftp-port={target.port}",
        f"--sftp-user={target.user}",
        f"--sftp-key-file={target.key_path}",
    ]


def build_rclone_args(package, target, direction, local_root, remote_root, tmp_dir):
    """Build args for an ``rclone copy --files-from`` of one package.

    Ruling R8: ``--files-from`` lines must be paths relative to BOTH the
    source root and the destination root — that's how rclone's
    ``src/<rel> -> dst/<rel>`` copy actually works. So ``rel`` is computed
    from the LOCAL side (``os.path.relpath(e.local, local_root)``, POSIX
    separators), and every entry is validated to land at the same place
    under ``remote_root``: ``posixpath.join(remote_root, rel) == e.remote``.
    A :class:`SyncError` is raised for any entry that doesn't line up.
    Grouping entries by a consistent ``(local_root, remote_root)`` pair
    before calling this is the caller's job (e.g. the upload node), not
    this function's.

    Returns ``(args, files_from_path)``.
    """
    files_from = os.path.join(str(tmp_dir), f"files_{uuid.uuid4().hex[:8]}.txt")
    with open(files_from, "w") as f:
        for e in package:
            rel = os.path.relpath(e.local, local_root).replace(os.sep, "/")
            if posixpath.join(remote_root, rel) != e.remote:
                raise SyncError(f"entry {e.local} does not map under {remote_root}")
            f.write(rel + "\n")

    remote = f":sftp:{remote_root}"
    src, dst = (local_root, remote) if direction == "up" else (remote, local_root)

    args = ["copy", src, dst, "--files-from", files_from]
    args += _sftp_flags(target)
    args += list(_COMMON_RCLONE_FLAGS)
    return args, files_from


def build_rclone_dir_args(local_dir, target, direction, remote_root):
    """Build args for a plain ``rclone copy`` of a whole directory (no
    ``--files-from``)."""
    remote = f":sftp:{remote_root}"
    src, dst = (local_dir, remote) if direction == "up" else (remote, local_dir)

    args = ["copy", src, dst]
    args += _sftp_flags(target)
    args += list(_COMMON_RCLONE_FLAGS)
    return args


# -- subprocess runner ---------------------------------------------------------


def _run_rclone(rclone_bin, args, progress_cb=None):
    """Run rclone, parse its ``--use-json-log`` stderr for progress.

    Returns ``(last_stats_dict, elapsed_seconds)``. Raises SyncError if the
    process exits non-zero.
    """
    t0 = time.time()
    proc = subprocess.Popen([rclone_bin, *args], stderr=subprocess.PIPE, text=True)
    last = {"bytes": 0, "transfers": 0}
    assert proc.stderr is not None
    for line in proc.stderr:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        st = msg.get("stats")
        if not st:
            continue
        last["bytes"] = st.get("bytes", last["bytes"])
        last["transfers"] = st.get("transfers", last["transfers"])
        if progress_cb:
            progress_cb(st.get("bytes", 0), st.get("totalBytes", 0), st.get("speed", 0))
    returncode = proc.wait()
    if returncode != 0:
        raise SyncError(f"rclone exit {returncode}")
    return last, time.time() - t0


def rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None, extra_args=()):
    """Transfer one package (list of FileEntry) via ``rclone copy --files-from``."""
    with tempfile.TemporaryDirectory() as tmp:
        args, _ = build_rclone_args(package, target, direction, local_root, remote_root, tmp)
        args = list(args) + list(extra_args)
        _, seconds = _run_rclone(rclone_bin, args, progress_cb)
        return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=seconds)


def rclone_copy_dir(local_dir, target, direction, rclone_bin, remote_root, progress_cb=None):
    """Transfer a whole directory via plain ``rclone copy`` (no package list).

    File/byte counts in the returned SyncStats come from rclone's own final
    JSON stats line (there's no local package to sum sizes from).
    """
    args = build_rclone_dir_args(local_dir, target, direction, remote_root)
    last, seconds = _run_rclone(rclone_bin, args, progress_cb)
    return SyncStats(files=last["transfers"], bytes=last["bytes"], seconds=seconds)


# -- compression staging -------------------------------------------------------


def compress_stage(package, staging_dir, level=3):
    """Compress a package's compressible entries into ``staging_dir``.

    For each entry, classify it (``compression.classify_file``); entries
    classified SKIP are left as-is. Entries classified ZSTD are compressed
    into ``staging_dir`` at ``<remote>.zst`` (preserving the remote-relative
    path) and the returned entry points at the staged file with ``.zst``
    appended to ``remote``. TAR_ZSTD-classified entries (currently only
    ``.vdb``) are left uncompressed here: batching them into a single tar
    archive is a directory-level operation this per-package helper doesn't
    do, so they upload uncompressed (worker/compression.py's
    ``compress_directory`` handles VDB batching for the older bulk-sync
    path). If ``zstd`` isn't available on this machine, ``compress_file``
    returns False and the entry is likewise left uncompressed.

    Returns ``(package_for_upload, post_command)`` where ``post_command`` is
    a single shell command that decompresses every staged ``.zst`` file on
    the sync pod (each with ``zstd -d --rm``), or ``""`` if nothing was
    compressed.
    """
    staging_dir = str(staging_dir)
    upload_entries = []
    zst_remotes = []

    for e in package:
        strategy = classify_file(e.local)
        if strategy in (CompressionStrategy.SKIP, CompressionStrategy.TAR_ZSTD):
            upload_entries.append(e)
            continue

        staged_path = os.path.join(staging_dir, e.remote) + ".zst"
        os.makedirs(os.path.dirname(staged_path), exist_ok=True)
        if compress_file(e.local, staged_path, strategy, level=level):
            staged_remote = e.remote + ".zst"
            upload_entries.append(
                FileEntry(local=staged_path, remote=staged_remote, size=os.path.getsize(staged_path))
            )
            zst_remotes.append(staged_remote)
        else:
            # No zstd available (or compression failed): leave uncompressed.
            upload_entries.append(e)

    if not zst_remotes:
        return upload_entries, ""

    post_command = "; ".join(f"zstd -d --rm {r}" for r in zst_remotes)
    return upload_entries, post_command
