"""JuiceFS sync agent for pre/post-task file synchronization.

Uses ``juicefs sync`` to transfer files between JuiceFS (B2 backend) and the
local Network Volume without requiring FUSE.  This is the core mechanism for
delivering project assets to RunPod pods and uploading render results back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

import redis

from .cache_manager import ensure_free_space, mark_group_used
from .config import WorkerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ManifestEntry = dict[str, Any]  # {size: int, type: str}
Manifest = dict[str, ManifestEntry]  # {rel_path: ManifestEntry}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _push_log(
    redis_client: redis.Redis,
    task_id: str,
    line: str,
) -> None:
    """Append a sync log line to the task's log list in Redis."""
    key = f"rp:logs:{task_id}"
    try:
        redis_client.rpush(key, line)
        redis_client.expire(key, 86400)
    except redis.RedisError:
        pass


def _run_juicefs_sync(
    config: WorkerConfig,
    src: str,
    dst: str,
    task_id: str,
    redis_client: redis.Redis,
    extra_args: list[str] | None = None,
) -> bool:
    """Run ``juicefs sync`` and return True on success.

    Parameters
    ----------
    src, dst:
        Source and destination paths.  For JuiceFS side, use a relative
        path (trailing-slash normalised by ``_jfs_uri``).  Paths that do
        NOT start with ``/`` are treated as JuiceFS-relative and wrapped
        with ``jfs://JFSMETA/``.  The actual meta URL is passed via the
        ``JFSMETA`` environment variable (Go's ``url.Parse`` cannot handle
        nested ``jfs://redis://…`` URIs).
    extra_args:
        Additional CLI flags forwarded to ``juicefs sync``.
    """
    # JuiceFS sync resolves jfs://NAME/path/ by reading os.Getenv(NAME).
    # We set JFSMETA=<redis://...> and reference it as jfs://JFSMETA/path/.
    env_name = "JFSMETA"
    jfs_src = f"jfs://{env_name}/{src}" if not src.startswith("/") else src
    jfs_dst = dst if dst.startswith("/") else f"jfs://{env_name}/{dst}"

    cmd = [
        config.juicefs_bin,
        "sync",
        jfs_src,
        jfs_dst,
        "--threads", "10",
        "--update",
    ]
    if extra_args:
        cmd.extend(extra_args)

    _push_log(redis_client, task_id, f"[sync] {src} -> {dst}")
    logger.info("juicefs sync: %s -> %s", src, dst)

    env = os.environ.copy()
    env[env_name] = config.juicefs_meta_url

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max per sync operation
            env=env,
        )
        if result.returncode == 0:
            logger.info("juicefs sync succeeded")
            _push_log(redis_client, task_id, "[sync] OK")
            return True

        error = result.stderr.strip() or result.stdout.strip()
        logger.error("juicefs sync failed (rc=%d): %s", result.returncode, error)
        _push_log(redis_client, task_id, f"[sync] FAILED (rc={result.returncode}): {error}")
        return False

    except subprocess.TimeoutExpired:
        logger.error("juicefs sync timed out after 1800s")
        _push_log(redis_client, task_id, "[sync] TIMEOUT after 30min")
        return False
    except FileNotFoundError:
        logger.error("juicefs binary not found at %s", config.juicefs_bin)
        _push_log(redis_client, task_id, f"[sync] juicefs not found: {config.juicefs_bin}")
        return False


def _jfs_uri(meta_url: str, path: str) -> str:
    """Normalise a relative path for use with ``_run_juicefs_sync``.

    Returns the path stripped of leading ``/`` and with a trailing ``/``.
    The ``meta_url`` argument is accepted for API compatibility but unused
    — the actual meta URL is injected as an env var by ``_run_juicefs_sync``.
    """
    path = path.lstrip("/")
    if not path.endswith("/"):
        path += "/"
    return path


def _local_path(base: str, rel: str) -> str:
    """Build a local filesystem path with trailing slash."""
    full = os.path.join(base, rel.lstrip("/"))
    if not full.endswith("/"):
        full += "/"
    return full


def _group_manifest_by_directory(manifest: Manifest) -> dict[str, list[str]]:
    """Group manifest entries by their top-level directory.

    For a manifest like::

        {
            "tex/bricks.exr": ...,
            "tex/wood.exr": ...,
            "geo/shot010/hero.bgeo.sc": ...,
        }

    Returns::

        {
            "tex": ["tex/bricks.exr", "tex/wood.exr"],
            "geo/shot010": ["geo/shot010/hero.bgeo.sc"],
        }

    The grouping depth is 2 for known asset directories (geo, sim, tex,
    cache) and 1 for everything else.
    """
    groups: dict[str, list[str]] = {}
    deep_prefixes = {"geo", "sim", "cache", "fx"}

    for rel_path in manifest:
        parts = rel_path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in deep_prefixes:
            group_key = "/".join(parts[:2])
        elif parts:
            group_key = parts[0]
        else:
            group_key = ""
        groups.setdefault(group_key, []).append(rel_path)

    return groups


def _check_files_present(base_dir: str, manifest: Manifest) -> tuple[list[str], list[str]]:
    """Check which manifest files are already present on the NV.

    Returns (present, missing) lists of relative paths.
    """
    present: list[str] = []
    missing: list[str] = []
    for rel_path, info in manifest.items():
        full = os.path.join(base_dir, rel_path.lstrip("/"))
        if os.path.exists(full):
            # Optionally verify size
            expected_size = info.get("size", 0)
            if expected_size > 0:
                actual_size = os.path.getsize(full)
                if actual_size != expected_size:
                    missing.append(rel_path)
                    continue
            present.append(rel_path)
        else:
            missing.append(rel_path)
    return present, missing


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pre_task_sync(
    task: dict[str, Any],
    config: WorkerConfig,
    redis_client: redis.Redis,
) -> bool:
    """Download project files from JuiceFS to Network Volume before task execution.

    Handles two sync sources:

    1. ``sync_dirs`` — explicit directories to sync (e.g. pdgtemp scripts).
       These are always synced unconditionally.
    2. ``manifest`` — file-level manifest for selective asset sync.
       Only missing files are downloaded.

    Returns True on success, False on failure.
    """
    task_id = task.get("task_id", "unknown")
    manifest: Manifest | None = task.get("manifest")
    sync_dirs: list[str] = task.get("sync_dirs", [])
    meta_url = config.juicefs_meta_url

    if not meta_url:
        logger.debug("JUICEFS_META_URL not configured, skipping pre-task sync")
        return True

    if not manifest and not sync_dirs:
        logger.debug("No manifest or sync_dirs in task %s, skipping pre-task sync", task_id)
        return True

    project_dir = config.project_dir
    start = time.monotonic()
    failed: list[str] = []

    # ---- Phase 1: sync explicit directories (pdgtemp, etc.) ----
    if sync_dirs:
        _push_log(
            redis_client, task_id,
            f"[sync] Syncing {len(sync_dirs)} directories: {sync_dirs}",
        )
        for rel_dir in sync_dirs:
            src = _jfs_uri(meta_url, rel_dir)
            dst = _local_path(project_dir, rel_dir)
            os.makedirs(dst, exist_ok=True)
            ok = _run_juicefs_sync(config, src, dst, task_id, redis_client)
            if not ok:
                failed.append(rel_dir)

    # ---- Phase 2: manifest-based selective sync ----
    if manifest:
        _push_log(redis_client, task_id, f"[sync] Pre-task sync: {len(manifest)} files in manifest")

        present, missing = _check_files_present(project_dir, manifest)
        _push_log(
            redis_client, task_id,
            f"[sync] NV cache: {len(present)} present, {len(missing)} missing",
        )
        logger.info(
            "Pre-task sync for %s: %d present, %d missing",
            task_id, len(present), len(missing),
        )

        if missing:
            missing_manifest = {p: manifest[p] for p in missing}
            groups = _group_manifest_by_directory(missing_manifest)

            total_missing_bytes = sum(
                manifest[p].get("size", 0) for p in missing
            )
            if total_missing_bytes > 0:
                ensure_free_space(total_missing_bytes, config, redis_client, task_id)

            for group_path, files in groups.items():
                mark_group_used(project_dir, group_path, redis_client)
                src = _jfs_uri(meta_url, group_path)
                dst = _local_path(project_dir, group_path)
                os.makedirs(dst, exist_ok=True)
                ok = _run_juicefs_sync(config, src, dst, task_id, redis_client)
                if not ok:
                    failed.append(group_path)

    elapsed = time.monotonic() - start

    if failed:
        msg = f"[sync] Pre-task sync PARTIAL ({len(failed)} groups failed) in {elapsed:.1f}s"
        _push_log(redis_client, task_id, msg)
        logger.warning(msg)
        return True  # partial sync is not fatal

    msg = f"[sync] Pre-task sync complete in {elapsed:.1f}s"
    _push_log(redis_client, task_id, msg)
    logger.info(msg)
    return True


def post_task_sync(
    task: dict[str, Any],
    config: WorkerConfig,
    redis_client: redis.Redis,
    output_files: list[str] | None = None,
) -> bool:
    """Upload render results from Network Volume back to JuiceFS.

    Scans output directories for new files and uploads them so artists can
    access results immediately via JuiceFS FUSE on their workstations.

    Parameters
    ----------
    output_files:
        Explicit list of output file paths (absolute).  If not provided,
        the function looks at common output locations.

    Returns True on success, False on failure.
    """
    task_id = task.get("task_id", "unknown")
    meta_url = config.juicefs_meta_url

    if not meta_url:
        logger.debug("JUICEFS_META_URL not configured, skipping post-task sync")
        return True

    project_dir = config.project_dir

    # Collect output directories to sync
    output_dirs: set[str] = set()

    if output_files:
        for fpath in output_files:
            if os.path.exists(fpath):
                output_dirs.add(os.path.dirname(fpath))
    else:
        # Check common output locations
        for subdir in ("renders", "output", "sim_output"):
            candidate = os.path.join(project_dir, subdir)
            if os.path.isdir(candidate) and os.listdir(candidate):
                output_dirs.add(candidate)

    if not output_dirs:
        logger.debug("No output files to sync for task %s", task_id)
        return True

    _push_log(
        redis_client, task_id,
        f"[sync] Post-task sync: {len(output_dirs)} output directories",
    )

    start = time.monotonic()
    all_ok = True

    for out_dir in output_dirs:
        # Compute the relative path from project_dir
        if out_dir.startswith(project_dir):
            rel = os.path.relpath(out_dir, project_dir)
        else:
            rel = os.path.basename(out_dir)

        src = out_dir if out_dir.endswith("/") else out_dir + "/"
        dst = _jfs_uri(meta_url, rel)

        ok = _run_juicefs_sync(config, src, dst, task_id, redis_client)
        if not ok:
            all_ok = False

    elapsed = time.monotonic() - start
    status = "complete" if all_ok else "PARTIAL"
    msg = f"[sync] Post-task sync {status} in {elapsed:.1f}s"
    _push_log(redis_client, task_id, msg)
    logger.info(msg)

    return all_ok
