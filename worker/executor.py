"""Task executor -- runs Houdini commands inside the pod."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any, Optional

import redis

from .config import WorkerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class TaskResult:
    """Outcome of a single task execution."""

    __slots__ = ("task_id", "status", "exit_code", "duration_seconds", "error")

    def __init__(
        self,
        task_id: str,
        status: str,
        exit_code: int,
        duration_seconds: float,
        error: Optional[str] = None,
    ) -> None:
        self.task_id = task_id
        self.status = status
        self.exit_code = exit_code
        self.duration_seconds = duration_seconds
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
        }
        if self.error:
            d["error"] = self.error
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_juicefs_warmup(
    paths: list[str],
    redis_client: redis.Redis,
    task_id: str,
) -> None:
    """Pre-fetch JuiceFS paths so that I/O during the render is fast."""
    for path in paths:
        logger.info("JuiceFS warmup: %s", path)
        _push_log(redis_client, task_id, f"[warmup] juicefs warmup {path}")
        try:
            result = subprocess.run(
                ["juicefs", "warmup", path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.stdout.strip():
                _push_log(redis_client, task_id, f"[warmup] {result.stdout.strip()}")
            if result.returncode != 0 and result.stderr.strip():
                logger.warning("juicefs warmup failed for %s: %s", path, result.stderr.strip())
                _push_log(redis_client, task_id, f"[warmup] WARNING: {result.stderr.strip()}")
        except FileNotFoundError:
            logger.warning("juicefs binary not found, skipping warmup")
            _push_log(redis_client, task_id, "[warmup] juicefs binary not found, skipping")
            break
        except subprocess.TimeoutExpired:
            logger.warning("juicefs warmup timed out for %s", path)
            _push_log(redis_client, task_id, f"[warmup] TIMEOUT for {path}")


def _push_log(
    redis_client: redis.Redis,
    task_id: str,
    line: str,
) -> None:
    """Append a log line to the task's log list in Redis."""
    key = f"rp:logs:{task_id}"
    try:
        redis_client.rpush(key, line)
        # Expire the log key after 24 hours so we don't leak memory.
        redis_client.expire(key, 86400)
    except redis.RedisError as exc:
        logger.debug("Failed to push log to Redis: %s", exc)


def _build_env(config: WorkerConfig, task_env: dict[str, str]) -> dict[str, str]:
    """Construct the subprocess environment.

    Priority (highest wins):
      1. Explicit task env overrides
      2. Houdini-specific variables
      3. Inherited host environment
    """
    env = os.environ.copy()

    # Houdini baseline
    env["HOUDINI_PATH"] = config.houdini_path
    env["HFS"] = config.houdini_path
    env["JUICEFS_MOUNT"] = config.juicefs_mount

    # Ensure Houdini binaries are on PATH
    houdini_bin = os.path.join(config.houdini_path, "bin")
    env["PATH"] = f"{houdini_bin}:{env.get('PATH', '/usr/bin:/bin')}"

    # Task-level overrides
    env.update(task_env)

    return env


def _build_shell_command(config: WorkerConfig, command: str) -> str:
    """Wrap the user command so Houdini environment is sourced first."""
    setup_script = os.path.join(config.houdini_path, "houdini_setup_bash")
    # Use `source` inside bash; the command runs in the same shell so
    # it inherits all Houdini env vars that houdini_setup_bash exports.
    return f'source "{setup_script}" && {command}'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_task(
    task: dict[str, Any],
    config: WorkerConfig,
    redis_client: redis.Redis,
) -> TaskResult:
    """Execute a single task and return the result.

    Parameters
    ----------
    task:
        Deserialized task dict with at least ``task_id`` and ``command``.
    config:
        Worker configuration.
    redis_client:
        Connected Redis client for streaming logs.

    Returns
    -------
    TaskResult
        The outcome including status, exit code, and duration.
    """
    task_id: str = task["task_id"]
    command: str = task["command"]
    task_env: dict[str, str] = task.get("env", {})
    warmup_paths: list[str] = task.get("warmup_paths", [])
    work_item_id = task.get("work_item_id")

    logger.info(
        "Executing task %s (work_item=%s): %s",
        task_id,
        work_item_id,
        command,
    )
    _push_log(redis_client, task_id, f"[worker] Starting task {task_id}")
    _push_log(redis_client, task_id, f"[worker] Command: {command}")

    # ---- JuiceFS warmup ------------------------------------------------
    if warmup_paths:
        _run_juicefs_warmup(warmup_paths, redis_client, task_id)

    # ---- Prepare environment -------------------------------------------
    env = _build_env(config, task_env)
    shell_cmd = _build_shell_command(config, command)

    # ---- Run the command -----------------------------------------------
    start_time = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["bash", "-c", shell_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            bufsize=1,  # line-buffered
        )

        # Stream output line by line
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            logger.debug("[task %s] %s", task_id, line)
            _push_log(redis_client, task_id, line)

        proc.wait(timeout=config.task_timeout)
        duration = time.monotonic() - start_time

        if proc.returncode == 0:
            logger.info(
                "Task %s succeeded in %.1fs", task_id, duration
            )
            _push_log(
                redis_client,
                task_id,
                f"[worker] Task succeeded in {duration:.1f}s",
            )
            return TaskResult(
                task_id=task_id,
                status="succeeded",
                exit_code=0,
                duration_seconds=duration,
            )
        else:
            error_msg = f"Process exited with code {proc.returncode}"
            logger.error(
                "Task %s failed (exit_code=%d) in %.1fs",
                task_id,
                proc.returncode,
                duration,
            )
            _push_log(
                redis_client,
                task_id,
                f"[worker] Task failed: {error_msg}",
            )
            return TaskResult(
                task_id=task_id,
                status="failed",
                exit_code=proc.returncode,
                duration_seconds=duration,
                error=error_msg,
            )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start_time
        logger.error(
            "Task %s timed out after %ds", task_id, config.task_timeout
        )
        _push_log(
            redis_client,
            task_id,
            f"[worker] Task timed out after {config.task_timeout}s",
        )
        # Kill the process tree
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        return TaskResult(
            task_id=task_id,
            status="failed",
            exit_code=-9,
            duration_seconds=duration,
            error=f"Timed out after {config.task_timeout}s",
        )

    except Exception as exc:
        duration = time.monotonic() - start_time
        logger.exception("Task %s raised an unexpected error", task_id)
        _push_log(
            redis_client,
            task_id,
            f"[worker] Internal error: {exc}",
        )
        return TaskResult(
            task_id=task_id,
            status="failed",
            exit_code=-1,
            duration_seconds=duration,
            error=str(exc),
        )
