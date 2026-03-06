"""B2 sync agent for pre/post-task file synchronization.

Uses rclone to transfer files between Backblaze B2 and the local Network Volume
with content-addressable shared cache and smart compression.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any

import redis

from .cache_manager import mark_cache_used, ensure_free_space_cache
from .compression import CompressionStrategy, classify_file, compress_file, decompress_file
from .config import WorkerConfig
from .manifest import compute_sha256

logger = logging.getLogger(__name__)

ManifestEntry = dict[str, Any]
Manifest = dict[str, ManifestEntry]

_RCLONE_CONF_PATH = "/tmp/rclone_rpfarm.conf"
_CACHE_BASE = "/workspace/cache"
_TASKS_BASE = "/workspace/tasks"
_LOCK_TTL = 600  # 10 minutes
_LOCK_RETRY_DELAY = 2  # seconds
_LOCK_MAX_RETRIES = 60  # 2 minutes max wait


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _push_log(redis_client: redis.Redis, task_id: str, line: str) -> None:
    """Append a sync log line to the task's log list in Redis."""
    key = f"rp:logs:{task_id}"
    try:
        redis_client.rpush(key, line)
        redis_client.expire(key, 86400)
    except redis.RedisError:
        pass


def _configure_rclone(config: WorkerConfig) -> str:
    """Write rclone config file with B2 credentials and return its path."""
    lines = [
        "[b2]",
        "type = b2",
        f"account = {config.b2_key_id}",
        f"key = {config.b2_app_key}",
    ]
    if config.b2_endpoint:
        lines.append(f"endpoint = {config.b2_endpoint}")

    os.makedirs(os.path.dirname(_RCLONE_CONF_PATH), exist_ok=True)
    with open(_RCLONE_CONF_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    return _RCLONE_CONF_PATH


def _run_rclone(
    config: WorkerConfig,
    src: str,
    dst: str,
    task_id: str,
    redis_client: redis.Redis,
    extra_args: list[str] | None = None,
) -> bool:
    """Run ``rclone copy`` and return True on success."""
    cmd = [
        config.rclone_bin,
        "copy",
        src,
        dst,
        "--config", _RCLONE_CONF_PATH,
        "--checksum",
        "--transfers=8",
        "--checkers=16",
        "--stats-one-line",
        "-q",
    ]
    if extra_args:
        cmd.extend(extra_args)

    _push_log(redis_client, task_id, f"[sync] rclone {src} -> {dst}")
    logger.info("rclone copy: %s -> %s", src, dst)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode == 0:
            logger.info("rclone copy succeeded")
            _push_log(redis_client, task_id, "[sync] OK")
            return True

        error = result.stderr.strip() or result.stdout.strip()
        logger.error("rclone copy failed (rc=%d): %s", result.returncode, error)
        _push_log(redis_client, task_id, f"[sync] FAILED (rc={result.returncode}): {error}")
        return False

    except subprocess.TimeoutExpired:
        logger.error("rclone copy timed out after 1800s")
        _push_log(redis_client, task_id, "[sync] TIMEOUT after 30min")
        return False
    except FileNotFoundError:
        logger.error("rclone binary not found at %s", config.rclone_bin)
        _push_log(redis_client, task_id, f"[sync] rclone not found: {config.rclone_bin}")
        return False


def _b2_remote(config: WorkerConfig, rel_path: str) -> str:
    """Build a B2 remote path for rclone."""
    return f"b2:{config.b2_bucket}/projects/{config.project_id}/{rel_path}"


# ---------------------------------------------------------------------------
# Cache locking (Redis-based, cross-pod coordination)
# ---------------------------------------------------------------------------

def _acquire_cache_lock(redis_client: redis.Redis, sha256: str) -> bool:
    """Acquire a distributed lock for a cache entry. Returns True if acquired."""
    lock_key = f"rp:lock:cache:{sha256}"
    for attempt in range(_LOCK_MAX_RETRIES):
        try:
            if redis_client.set(lock_key, "1", nx=True, ex=_LOCK_TTL):
                return True
        except redis.RedisError as exc:
            logger.warning("Lock acquire error for %s: %s", sha256, exc)
            return False

        if attempt < _LOCK_MAX_RETRIES - 1:
            time.sleep(_LOCK_RETRY_DELAY)

    logger.warning("Timeout acquiring cache lock for %s after %d retries", sha256, _LOCK_MAX_RETRIES)
    return False


def _release_cache_lock(redis_client: redis.Redis, sha256: str) -> None:
    """Release a distributed cache lock."""
    lock_key = f"rp:lock:cache:{sha256}"
    try:
        redis_client.delete(lock_key)
    except redis.RedisError:
        pass


# ---------------------------------------------------------------------------
# Cache path helpers
# ---------------------------------------------------------------------------

def _cache_path(sha256: str, filename: str) -> str:
    """Return the cache path for a file: /workspace/cache/{prefix}/{sha256}/{filename}."""
    return os.path.join(_CACHE_BASE, sha256[:2], sha256, filename)


def _setup_task_dir(task_id: str) -> tuple[str, str]:
    """Create and return (input_dir, output_dir) for a task."""
    input_dir = os.path.join(_TASKS_BASE, task_id, "input")
    output_dir = os.path.join(_TASKS_BASE, task_id, "output")
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    return input_dir, output_dir


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pre_task_sync(
    task: dict[str, Any],
    config: WorkerConfig,
    redis_client: redis.Redis,
) -> bool:
    """Download project files from B2 to shared cache before task execution.

    Reads the ``manifest`` field from the task dict.  For each file, checks
    the content-addressable cache first, downloads from B2 if missing, and
    creates symlinks into the task input directory.

    Returns True on success (including partial success).
    """
    if not config.b2_key_id:
        logger.debug("B2 not configured, skipping pre-task sync")
        return True

    task_id = task.get("task_id", "unknown")
    manifest: Manifest | None = task.get("manifest")

    if not manifest:
        logger.debug("No manifest in task %s, skipping pre-task sync", task_id)
        return True

    _push_log(redis_client, task_id, f"[sync] Pre-task sync: {len(manifest)} files in manifest")
    _configure_rclone(config)
    input_dir, _output_dir = _setup_task_dir(task_id)

    start = time.monotonic()
    cached_count = 0
    downloaded_count = 0
    failed_count = 0

    for rel_path, entry in manifest.items():
        sha256 = entry.get("sha256", "")
        if not sha256:
            logger.warning("No sha256 for %s, skipping", rel_path)
            failed_count += 1
            continue

        filename = os.path.basename(rel_path)
        cached_file = _cache_path(sha256, filename)

        # Check if already in cache
        if os.path.isfile(cached_file):
            cached_count += 1
            mark_cache_used(sha256, redis_client)
        else:
            # Need to download
            file_size = entry.get("size", 0)
            if file_size > 0:
                ensure_free_space_cache(file_size, config, redis_client, task_id)

            if not _acquire_cache_lock(redis_client, sha256):
                logger.warning("Could not acquire lock for %s, skipping", rel_path)
                failed_count += 1
                continue

            try:
                # Re-check after acquiring lock (another pod may have downloaded it)
                if os.path.isfile(cached_file):
                    cached_count += 1
                    mark_cache_used(sha256, redis_client)
                    continue

                # Download from B2 to a temp location, then move to cache
                cache_dir = os.path.dirname(cached_file)
                tmp_dir = cache_dir + ".tmp"
                os.makedirs(tmp_dir, exist_ok=True)

                b2_src = _b2_remote(config, rel_path)
                ok = _run_rclone(config, b2_src, tmp_dir, task_id, redis_client)

                if not ok:
                    logger.error("Failed to download %s from B2", rel_path)
                    failed_count += 1
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    continue

                # Find the downloaded file in tmp_dir
                downloaded_file = os.path.join(tmp_dir, filename)
                if not os.path.isfile(downloaded_file):
                    # rclone may preserve directory structure
                    for root, _dirs, files in os.walk(tmp_dir):
                        for f in files:
                            downloaded_file = os.path.join(root, f)
                            break
                        break

                if not os.path.isfile(downloaded_file):
                    logger.error("Downloaded file not found in %s for %s", tmp_dir, rel_path)
                    failed_count += 1
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    continue

                # Decompress if needed
                strategy_name = entry.get("strategy", "SKIP")
                try:
                    strategy = CompressionStrategy(strategy_name.lower()) if strategy_name != "SKIP" else CompressionStrategy.SKIP
                except ValueError:
                    try:
                        strategy = CompressionStrategy[strategy_name]
                    except KeyError:
                        strategy = CompressionStrategy.SKIP

                if strategy != CompressionStrategy.SKIP:
                    decompressed_file = os.path.join(tmp_dir, "decompressed_" + filename)
                    if decompress_file(downloaded_file, decompressed_file, strategy):
                        os.remove(downloaded_file)
                        downloaded_file = decompressed_file
                    else:
                        logger.warning("Decompression failed for %s, using as-is", rel_path)

                # Move to final cache location
                os.makedirs(cache_dir, exist_ok=True)
                final_name = filename
                # If we decompressed, the filename may have changed
                final_cached = os.path.join(cache_dir, final_name)
                shutil.move(downloaded_file, final_cached)
                shutil.rmtree(tmp_dir, ignore_errors=True)

                mark_cache_used(sha256, redis_client)
                downloaded_count += 1
                logger.info("Cached %s -> %s", rel_path, final_cached)

            finally:
                _release_cache_lock(redis_client, sha256)

        # Create symlink from cache to task input dir
        link_target = cached_file if os.path.isfile(cached_file) else _cache_path(sha256, filename)
        if os.path.isfile(link_target):
            link_path = os.path.join(input_dir, rel_path)
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            try:
                if os.path.exists(link_path):
                    os.remove(link_path)
                os.symlink(link_target, link_path)
            except OSError as exc:
                logger.warning("Failed to symlink %s: %s", rel_path, exc)

    elapsed = time.monotonic() - start
    msg = (
        f"[sync] Pre-task sync complete in {elapsed:.1f}s: "
        f"{cached_count} cached, {downloaded_count} downloaded, {failed_count} failed"
    )
    _push_log(redis_client, task_id, msg)
    logger.info(msg)

    return True  # partial success is OK


def post_task_sync(
    task: dict[str, Any],
    config: WorkerConfig,
    redis_client: redis.Redis,
    output_files: list[str] | None = None,
) -> bool:
    """Upload task output files from Network Volume to B2.

    For "intermediate" output: cache locally + upload to B2 cache path.
    For "final" output: compress + upload to B2 results path + cleanup.

    Returns True on success.
    """
    if not config.b2_key_id:
        logger.debug("B2 not configured, skipping post-task sync")
        return True

    task_id = task.get("task_id", "unknown")
    output_type = task.get("output_type", "final")
    output_dir = os.path.join(_TASKS_BASE, task_id, "output")

    # Collect output files
    files_to_upload: list[str] = []
    if output_files:
        files_to_upload = [f for f in output_files if os.path.isfile(f)]
    elif os.path.isdir(output_dir):
        for root, _dirs, files in os.walk(output_dir):
            for fname in files:
                files_to_upload.append(os.path.join(root, fname))

    if not files_to_upload:
        logger.debug("No output files to sync for task %s", task_id)
        return True

    _push_log(redis_client, task_id, f"[sync] Post-task sync: {len(files_to_upload)} files, type={output_type}")
    _configure_rclone(config)

    start = time.monotonic()
    total_original_bytes = 0
    total_compressed_bytes = 0
    files_compressed = 0
    files_skipped = 0
    all_ok = True

    if output_type == "intermediate":
        all_ok = _post_sync_intermediate(
            task_id, config, redis_client, files_to_upload, output_dir,
        )
    else:
        # Final output
        staging_dir = os.path.join(config.staging_dir, task_id)
        os.makedirs(staging_dir, exist_ok=True)

        result_manifest: dict[str, Any] = {}

        for fpath in files_to_upload:
            rel_path = os.path.relpath(fpath, output_dir) if fpath.startswith(output_dir) else os.path.basename(fpath)
            original_size = os.path.getsize(fpath)
            total_original_bytes += original_size

            upload_path = fpath
            strategy = CompressionStrategy.SKIP

            if config.compression_enabled:
                strategy = classify_file(fpath)
                if strategy == CompressionStrategy.ZSTD:
                    compressed_path = os.path.join(staging_dir, rel_path + ".zst")
                    if compress_file(fpath, compressed_path, CompressionStrategy.ZSTD, config.compression_level):
                        upload_path = compressed_path
                        files_compressed += 1
                    else:
                        files_skipped += 1
                        strategy = CompressionStrategy.SKIP
                else:
                    files_skipped += 1

            compressed_size = os.path.getsize(upload_path)
            total_compressed_bytes += compressed_size

            # Upload to B2 results path
            b2_dst = _b2_remote(config, f"results/{rel_path}")
            if strategy == CompressionStrategy.ZSTD:
                b2_dst = _b2_remote(config, f"results/{rel_path}.zst")

            ok = _run_rclone(config, upload_path, os.path.dirname(b2_dst), task_id, redis_client)
            if not ok:
                all_ok = False
                logger.error("Failed to upload %s to B2", rel_path)
            else:
                sha256 = compute_sha256(fpath)
                result_manifest[rel_path] = {
                    "sha256": sha256,
                    "size": original_size,
                    "compressed_size": compressed_size,
                    "strategy": strategy.value,
                }

        # Publish result manifest to Redis
        if result_manifest:
            manifest_key = f"rp:manifest:result:{task_id}"
            try:
                redis_client.set(manifest_key, json.dumps(result_manifest), ex=86400)
            except redis.RedisError as exc:
                logger.warning("Failed to publish result manifest: %s", exc)

        # Cleanup staging and task dirs
        shutil.rmtree(staging_dir, ignore_errors=True)
        task_dir = os.path.join(_TASKS_BASE, task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
        logger.info("Cleaned up task directory: %s", task_dir)

    elapsed = time.monotonic() - start
    upload_time_ms = int(elapsed * 1000)

    # Record metrics
    compression_ratio = (total_compressed_bytes / total_original_bytes) if total_original_bytes > 0 else 1.0
    metrics = {
        "upload_original_bytes": total_original_bytes,
        "upload_compressed_bytes": total_compressed_bytes,
        "upload_time_ms": upload_time_ms,
        "compression_ratio": round(compression_ratio, 4),
        "files_compressed": files_compressed,
        "files_skipped": files_skipped,
    }
    publish_sync_metrics(redis_client, config.project_id, task_id, metrics)

    status = "complete" if all_ok else "PARTIAL"
    msg = (
        f"[sync] Post-task sync {status} in {elapsed:.1f}s: "
        f"{len(files_to_upload)} files, ratio={compression_ratio:.2f}"
    )
    _push_log(redis_client, task_id, msg)
    logger.info(msg)

    return all_ok


def _post_sync_intermediate(
    task_id: str,
    config: WorkerConfig,
    redis_client: redis.Redis,
    files: list[str],
    output_dir: str,
) -> bool:
    """Handle intermediate output: cache + upload to B2 cache path."""
    all_ok = True
    intermediate_manifest: dict[str, Any] = {}

    for fpath in files:
        rel_path = os.path.relpath(fpath, output_dir) if fpath.startswith(output_dir) else os.path.basename(fpath)
        sha256 = compute_sha256(fpath)
        filename = os.path.basename(fpath)
        size = os.path.getsize(fpath)

        # Put in shared cache
        cached_file = _cache_path(sha256, filename)
        if not os.path.isfile(cached_file):
            os.makedirs(os.path.dirname(cached_file), exist_ok=True)
            try:
                shutil.copy2(fpath, cached_file)
            except OSError as exc:
                logger.warning("Failed to cache %s: %s", rel_path, exc)

        mark_cache_used(sha256, redis_client)

        # Upload to B2 cache path
        b2_dst_dir = _b2_remote(config, f"cache/{os.path.dirname(rel_path)}")
        ok = _run_rclone(config, fpath, b2_dst_dir, task_id, redis_client)
        if not ok:
            all_ok = False

        intermediate_manifest[rel_path] = {
            "sha256": sha256,
            "size": size,
        }

    # Publish intermediate manifest to Redis
    if intermediate_manifest:
        manifest_key = f"rp:manifest:intermediate:{task_id}"
        try:
            redis_client.set(manifest_key, json.dumps(intermediate_manifest), ex=86400)
        except redis.RedisError as exc:
            logger.warning("Failed to publish intermediate manifest: %s", exc)

    return all_ok


def publish_sync_metrics(
    redis_client: redis.Redis,
    project_id: str,
    task_id: str,
    metrics: dict[str, Any],
) -> None:
    """Publish sync metrics to Redis with per-task detail and project totals."""
    task_key = f"rp:metrics:sync:{project_id}:{task_id}"
    totals_key = f"rp:metrics:sync:totals:{project_id}"

    try:
        redis_client.hset(task_key, mapping={k: str(v) for k, v in metrics.items()})
        redis_client.expire(task_key, 86400)
    except redis.RedisError as exc:
        logger.debug("Failed to publish task metrics: %s", exc)

    # Increment totals
    int_fields = ["upload_original_bytes", "upload_compressed_bytes", "upload_time_ms", "files_compressed", "files_skipped"]
    for field in int_fields:
        val = metrics.get(field, 0)
        if val:
            try:
                redis_client.hincrby(totals_key, field, int(val))
            except redis.RedisError:
                pass
