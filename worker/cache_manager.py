"""Network Volume cache manager.

Tracks asset group usage via Redis timestamps and evicts cold groups when
the NV disk usage exceeds a configurable threshold.  Protected paths
(Houdini install, active task data) are never evicted.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import TYPE_CHECKING

import redis

if TYPE_CHECKING:
    from .config import WorkerConfig

logger = logging.getLogger(__name__)

# Paths that must NEVER be evicted
_PROTECTED_PREFIXES = (
    "/workspace/houdini",
    "/workspace/.juicefs",
    "/workspace/tasks",
)

_CACHE_USED_PREFIX = "rp:cache:used:"


# ---------------------------------------------------------------------------
# Redis-backed usage tracking
# ---------------------------------------------------------------------------

def mark_cache_used(sha256: str, redis_client: redis.Redis) -> None:
    """Mark a SHA-256 cache entry as recently used."""
    key = f"{_CACHE_USED_PREFIX}{sha256}"
    try:
        redis_client.set(key, str(time.time()))
    except redis.RedisError as exc:
        logger.debug("Failed to mark cache entry used: %s", exc)


def mark_group_used(
    project_dir: str,
    group_path: str,
    redis_client: redis.Redis,
) -> None:
    """Mark an asset group as recently used.

    Stores the current timestamp in Redis so the cache manager can determine
    which groups are cold and eligible for eviction.
    """
    key = f"{_CACHE_USED_PREFIX}{project_dir}/{group_path}"
    try:
        redis_client.set(key, str(time.time()))
    except redis.RedisError as exc:
        logger.debug("Failed to mark cache group used: %s", exc)


def _get_group_last_used(
    project_dir: str,
    group_path: str,
    redis_client: redis.Redis,
) -> float:
    """Return the last-used timestamp for a group, or 0.0 if unknown."""
    key = f"{_CACHE_USED_PREFIX}{project_dir}/{group_path}"
    try:
        val = redis_client.get(key)
        if val:
            return float(val)
    except (redis.RedisError, ValueError):
        pass
    return 0.0


def _delete_group_tracking(
    project_dir: str,
    group_path: str,
    redis_client: redis.Redis,
) -> None:
    """Remove the usage tracking key for an evicted group."""
    key = f"{_CACHE_USED_PREFIX}{project_dir}/{group_path}"
    try:
        redis_client.delete(key)
    except redis.RedisError:
        pass


# ---------------------------------------------------------------------------
# Disk usage helpers
# ---------------------------------------------------------------------------

def _disk_usage_pct(path: str = "/workspace") -> float:
    """Return disk usage percentage for the filesystem containing *path*."""
    try:
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100.0
    except OSError:
        return 0.0


def _dir_size_bytes(path: str) -> int:
    """Calculate total size of a directory tree in bytes."""
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _is_protected(full_path: str) -> bool:
    """Check if a path is protected from eviction."""
    for prefix in _PROTECTED_PREFIXES:
        if full_path.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------

def _enumerate_asset_groups(project_dir: str) -> list[str]:
    """List top-level asset group directories under *project_dir*.

    Returns relative paths like ``tex``, ``geo/shot010``, etc.
    """
    groups: list[str] = []
    deep_prefixes = {"geo", "sim", "cache", "fx"}

    if not os.path.isdir(project_dir):
        return groups

    for entry in os.scandir(project_dir):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith("."):
            continue

        full = entry.path
        if _is_protected(full):
            continue

        if name in deep_prefixes:
            # Go one level deeper
            try:
                for sub in os.scandir(full):
                    if sub.is_dir() and not sub.name.startswith("."):
                        groups.append(f"{name}/{sub.name}")
            except OSError:
                groups.append(name)
        else:
            groups.append(name)

    return groups


def _evict_group(project_dir: str, group_path: str, redis_client: redis.Redis) -> int:
    """Delete a group directory and return bytes freed."""
    full = os.path.join(project_dir, group_path)
    if not os.path.isdir(full):
        return 0

    if _is_protected(full):
        logger.warning("Refusing to evict protected path: %s", full)
        return 0

    size = _dir_size_bytes(full)
    try:
        shutil.rmtree(full)
        _delete_group_tracking(project_dir, group_path, redis_client)
        logger.info("Evicted cache group %s (%d MB)", group_path, size // (1024 * 1024))
        return size
    except OSError as exc:
        logger.error("Failed to evict %s: %s", full, exc)
        return 0


def ensure_free_space(
    needed_bytes: int,
    config: WorkerConfig,
    redis_client: redis.Redis,
    task_id: str = "",
) -> None:
    """DEPRECATED: use ensure_free_space_cache instead.

    Ensure at least *needed_bytes* of free space on the Network Volume.

    If the NV disk usage exceeds ``config.nv_cache_max_pct`` after accounting
    for the requested space, cold asset groups are evicted oldest-first until
    usage drops below the threshold or there is nothing left to evict.
    """
    usage = shutil.disk_usage("/workspace")
    free = usage.total - usage.used
    threshold_bytes = int(usage.total * config.nv_cache_max_pct / 100.0)
    used_after = usage.used + needed_bytes

    if used_after < threshold_bytes and free > needed_bytes:
        return  # plenty of room

    logger.info(
        "NV space: %.1f%% used, need %d MB, threshold %d%%",
        (usage.used / usage.total) * 100,
        needed_bytes // (1024 * 1024),
        config.nv_cache_max_pct,
    )

    # Enumerate and sort groups by last-used time (oldest first)
    project_dir = config.project_dir
    groups = _enumerate_asset_groups(project_dir)
    scored: list[tuple[float, str]] = []

    for g in groups:
        last_used = _get_group_last_used(project_dir, g, redis_client)
        scored.append((last_used, g))

    scored.sort(key=lambda x: x[0])  # oldest first

    freed = 0
    for last_used, group_path in scored:
        # Never evict recently used groups (within cold_days)
        age_days = (time.time() - last_used) / 86400 if last_used > 0 else float("inf")
        if age_days < config.nv_cache_cold_days:
            logger.debug("Skipping hot group %s (%.1f days old)", group_path, age_days)
            continue

        freed += _evict_group(project_dir, group_path, redis_client)
        log_msg = f"[cache] Evicted {group_path}, freed {freed // (1024 * 1024)} MB total"
        logger.info(log_msg)

        if task_id:
            try:
                redis_client.rpush(f"rp:logs:{task_id}", log_msg)
            except redis.RedisError:
                pass

        # Re-check disk usage
        current_usage = shutil.disk_usage("/workspace")
        current_free = current_usage.total - current_usage.used
        if current_free > needed_bytes:
            break

    if freed > 0:
        logger.info("Cache eviction freed %d MB total", freed // (1024 * 1024))


# ---------------------------------------------------------------------------
# SHA-256 cache eviction
# ---------------------------------------------------------------------------

def _enumerate_cache_entries(cache_dir: str = "/workspace/cache") -> list[str]:
    """List SHA-256 cache entries under *cache_dir*.

    Layout: /workspace/cache/ab/abc123.../filename
    Returns SHA-256 directory names (e.g. "ab/abc123...").
    """
    entries: list[str] = []
    if not os.path.isdir(cache_dir):
        return entries

    for prefix_entry in os.scandir(cache_dir):
        if not prefix_entry.is_dir() or len(prefix_entry.name) != 2:
            continue
        try:
            for sha_entry in os.scandir(prefix_entry.path):
                if sha_entry.is_dir():
                    entries.append(f"{prefix_entry.name}/{sha_entry.name}")
        except OSError:
            pass

    return entries


def _get_cache_last_used(sha256: str, redis_client: redis.Redis) -> float:
    """Return last-used timestamp for a SHA-256 cache entry, or 0.0 if unknown."""
    key = f"{_CACHE_USED_PREFIX}{sha256}"
    try:
        val = redis_client.get(key)
        if val:
            return float(val)
    except (redis.RedisError, ValueError):
        pass
    return 0.0


def ensure_free_space_cache(
    needed_bytes: int,
    config: WorkerConfig,
    redis_client: redis.Redis,
    task_id: str = "",
) -> None:
    """Ensure at least *needed_bytes* of free space by evicting cold cache entries.

    Scans /workspace/cache/ for SHA-256 prefixed directories and evicts
    oldest-used entries until enough space is available.
    Protected paths (/workspace/houdini, /workspace/tasks) are never touched.
    """
    cache_dir = "/workspace/cache"
    usage = shutil.disk_usage("/workspace")
    free = usage.total - usage.used
    threshold_bytes = int(usage.total * config.nv_cache_max_pct / 100.0)
    used_after = usage.used + needed_bytes

    if used_after < threshold_bytes and free > needed_bytes:
        return

    logger.info(
        "Cache space: %.1f%% used, need %d MB, threshold %d%%",
        (usage.used / usage.total) * 100,
        needed_bytes // (1024 * 1024),
        config.nv_cache_max_pct,
    )

    entries = _enumerate_cache_entries(cache_dir)
    scored: list[tuple[float, str]] = []
    for entry in entries:
        # Use the sha256 hash (second component) for Redis lookup
        sha256 = entry.split("/", 1)[1] if "/" in entry else entry
        last_used = _get_cache_last_used(sha256, redis_client)
        scored.append((last_used, entry))

    scored.sort(key=lambda x: x[0])  # oldest first

    freed = 0
    for last_used, entry_path in scored:
        age_days = (time.time() - last_used) / 86400 if last_used > 0 else float("inf")
        if age_days < config.nv_cache_cold_days:
            logger.debug("Skipping hot cache entry %s (%.1f days old)", entry_path, age_days)
            continue

        full = os.path.join(cache_dir, entry_path)
        if _is_protected(full):
            continue

        size = _dir_size_bytes(full)
        try:
            shutil.rmtree(full)
            sha256 = entry_path.split("/", 1)[1] if "/" in entry_path else entry_path
            try:
                redis_client.delete(f"{_CACHE_USED_PREFIX}{sha256}")
            except redis.RedisError:
                pass
            freed += size
            log_msg = f"[cache] Evicted {entry_path}, freed {freed // (1024 * 1024)} MB total"
            logger.info(log_msg)
            if task_id:
                try:
                    redis_client.rpush(f"rp:logs:{task_id}", log_msg)
                except redis.RedisError:
                    pass
        except OSError as exc:
            logger.error("Failed to evict %s: %s", full, exc)
            continue

        current_usage = shutil.disk_usage("/workspace")
        current_free = current_usage.total - current_usage.used
        if current_free > needed_bytes:
            break

    if freed > 0:
        logger.info("Cache eviction freed %d MB total", freed // (1024 * 1024))
