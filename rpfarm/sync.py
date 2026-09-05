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

``FileEntry.remote`` is always a full path on the remote side (e.g.
``/workspace/projects/<user>/<project>/...``), never a bare relative path.
``build_rclone_args`` (ruling R8) and ``compress_stage`` (ruling R10) both
enforce that every entry's ``remote`` actually lands under the
``remote_root`` a caller passes in, raising :class:`SyncError` otherwise —
callers are responsible for grouping entries by a consistent
``(local_root, remote_root)`` pair before calling either.

``compress_stage`` splits one package into two (``raw_package`` /
``staged_package``, see its docstring) rather than compressing in place,
because a compressed file physically lives under ``staging_dir`` and so
needs its own ``local_root`` for R8 to hold.
"""

from __future__ import annotations

import datetime
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass

from rpfarm.compression import (
    CompressionStrategy, classify_file, compress_file, decompress_command, select_codec)

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


def remote_index(target, rclone_bin, remote_root, run=None):
    """``{rel path: (size, mtime)}`` for everything already on the farm.

    One listing, asked of the farm itself rather than kept in a manifest
    beside the project. A manifest is separate state, and separate state
    drifts: a cancelled cook leaves half a package uploaded and a manifest
    that says otherwise (the owner has already cancelled one mid-upload).
    What is actually there is the only thing worth comparing against, and
    asking costs one round trip before a package is even compressed.

    Missing remote root is not an error -- the first cook of a project has
    nothing on the farm, and that is exactly the case that must not fail.
    """
    runner = run or _run_rclone_json
    remote = ":sftp:{}".format(remote_root)
    args = ["lsjson", "--recursive", "--files-only", remote]
    args += _sftp_flags(target)
    try:
        listing = runner(rclone_bin, args)
    except SyncError:
        return {}
    out = {}
    for row in listing:
        path = row.get("Path")
        if not path:
            continue
        out[path] = (int(row.get("Size", -1)), _parse_modtime(row.get("ModTime")))
    return out


def _run_rclone_json(rclone_bin, args):
    try:
        proc = subprocess.run([rclone_bin, *args], capture_output=True, text=True)
    except OSError as e:
        # No rclone, or not runnable. The listing is an optimisation: without
        # it every file is sent, which is slower and still correct.
        raise SyncError("rclone lsjson could not run ({})".format(e))
    if proc.returncode != 0:
        raise SyncError("rclone lsjson exit {}: {}".format(
            proc.returncode, (proc.stderr or "").strip()[:200]))
    try:
        return json.loads(proc.stdout or "[]")
    except ValueError as e:
        raise SyncError("rclone lsjson returned no JSON ({})".format(e))


def _parse_modtime(text):
    """rclone's RFC3339 ModTime -> epoch seconds, or None.

    Fractional seconds are dropped rather than parsed: rclone prints
    nanoseconds, datetime accepts microseconds, and the comparison has a
    two-second tolerance anyway, so the precision is noise either way.
    """
    if not text:
        return None
    cleaned = re.sub(r"\.\d+", "", str(text).strip())
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


#: How far apart two mtimes may be and still count as the same file. rclone
#: uses a modify-window for the same reason: filesystems and transports round
#: timestamps differently, and a second of slack costs nothing next to
#: re-sending a gigabyte.
MTIME_TOLERANCE_S = 2.0


def already_on_farm(entry, index, local_root, remote_root, tolerance=MTIME_TOLERANCE_S,
                    getsize=os.path.getsize, getmtime=os.path.getmtime):
    """Is this entry already on the farm, current, under its FINAL name?

    Compared against the file as it ends up on the farm -- the original,
    decompressed -- never against the archive it travelled in. The archive
    is deleted after unpacking, so comparing archive to archive is what made
    every compressed file look missing and re-sent it on every cook.

    Size and mtime, not a hash: with the mtime restored on unpack (see
    compression.decompress_command) these are exactly what rclone itself
    trusts, and hashing gigabytes on the pod every cook would cost more than
    the transfer it saves.
    """
    rel = posixpath.relpath(entry.remote, remote_root)
    found = index.get(rel)
    if not found:
        return False
    size, mtime = found
    try:
        if getsize(entry.local) != size:
            return False
        local_mtime = getmtime(entry.local)
    except OSError:
        return False
    if mtime is None:
        return False
    return abs(local_mtime - mtime) <= tolerance


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


def compress_stage(package, staging_dir, remote_root, level=3):
    """Compress a package's compressible entries into ``staging_dir``.

    Ruling R10: staged files must actually live under ``staging_dir`` (not
    wherever ``os.path.join`` happens to land when ``e.remote`` is an
    absolute path — see the CRITICAL fix this replaces), and the result
    must still satisfy R8's invariant for both halves of the package. So
    this returns *two* packages instead of one:

    - ``raw_package``: entries left untouched (SKIP-classified,
      TAR_ZSTD-classified, or compression failed) — upload these with the
      original ``(local_root, remote_root)``.
    - ``staged_package``: successfully compressed entries, staged at
      ``staging_dir/<rel>.zst`` where ``rel = posixpath.relpath(e.remote,
      remote_root)`` — upload these with ``local_root=staging_dir,
      remote_root=remote_root`` (R8 then holds: ``relpath(staged_local,
      staging_dir) == relpath(e.remote + ".zst", remote_root)``).

    Every entry's ``remote`` must be under ``remote_root``; a
    :class:`SyncError` is raised otherwise (same contract as
    :func:`build_rclone_args`).

    TAR_ZSTD-classified entries (currently only ``.vdb``) are left
    uncompressed here: batching them into a single tar archive is a
    directory-level operation this per-package helper doesn't do — known
    limitation, they always go into ``raw_package`` uncompressed
    (v1's ``worker/compression.py`` ``compress_directory`` handled VDB batching
    for the older bulk-sync path). If ``zstd`` isn't available on this
    machine (or compression otherwise fails), the whole package goes into
    ``raw_package`` uncompressed and the log says so once -- verified by
    test, because this paragraph described the behaviour correctly while the
    code raised FileNotFoundError instead.

    Returns ``(raw_package, staged_package, post_command)`` where
    ``post_command`` is a single shell command that ``cd``s to
    ``remote_root`` and decompresses every staged ``.zst`` file there
    (``zstd -d --rm``, paths shell-quoted), or ``""`` if ``staged_package``
    is empty.
    """
    staging_dir = str(staging_dir)
    # One codec for the whole package, chosen once: the standard library
    # unless a real zstd binary is on this machine. The extension it stages
    # under is what tells the pod how to unpack -- so the format travels
    # with the files rather than being assumed at the far end.
    codec = select_codec()
    raw_package = []
    staged_package = []
    zst_rels = []

    for e in package:
        rel = posixpath.relpath(e.remote, remote_root)
        if rel == "." or rel.startswith(".."):
            raise SyncError(f"entry {e.remote} is not under remote_root {remote_root}")

        strategy = classify_file(e.local)
        if strategy in (CompressionStrategy.SKIP, CompressionStrategy.TAR_ZSTD):
            raw_package.append(e)
            continue

        staged_path = os.path.join(staging_dir, rel + codec.ext)
        os.makedirs(os.path.dirname(staged_path), exist_ok=True)
        if compress_file(e.local, staged_path, strategy, level=level, codec=codec):
            staged_package.append(
                FileEntry(local=staged_path, remote=e.remote + codec.ext,
                          size=os.path.getsize(staged_path))
            )
            # The ORIGINAL's mtime, not the archive's: it is what the farm
            # copy must end up wearing so the next cook can see it is there.
            zst_rels.append((rel + codec.ext, os.path.getmtime(e.local)))
        else:
            # No zstd available (or compression failed): leave uncompressed.
            raw_package.append(e)

    if not zst_rels:
        return raw_package, staged_package, ""

    return raw_package, staged_package, decompress_command(remote_root, zst_rels, codec)
