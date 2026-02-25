"""Main worker daemon -- listens for tasks on a Redis queue and executes them."""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from types import FrameType
from typing import Optional

import redis

from .config import WorkerConfig
from .executor import execute_task
from .heartbeat import HeartbeatThread

logger = logging.getLogger(__name__)

# How long BRPOP blocks before returning None (seconds).
_BRPOP_TIMEOUT = 5


def _connect_redis(config: WorkerConfig) -> redis.Redis:
    """Connect to Redis with exponential backoff.

    Retries up to ``config.max_retries`` times before giving up.
    """
    backoff = 1.0
    last_err: Optional[Exception] = None

    for attempt in range(1, config.max_retries + 1):
        try:
            client = redis.Redis.from_url(
                config.redis_url,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_timeout=30,
            )
            client.ping()
            logger.info("Connected to Redis (attempt %d)", attempt)
            return client
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            last_err = exc
            logger.warning(
                "Redis connection attempt %d/%d failed: %s -- retrying in %.0fs",
                attempt,
                config.max_retries,
                exc,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    logger.error("Could not connect to Redis after %d attempts", config.max_retries)
    raise SystemExit(1) from last_err


def run() -> None:
    """Entry point for the worker daemon."""

    # ---- Configuration -------------------------------------------------
    config = WorkerConfig.from_env()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Re-log after logging is fully configured so the lines are visible.
    logger.info("RunPodFarm worker starting (pod_id=%s)", config.pod_id)

    # ---- Redis connection ----------------------------------------------
    rc = _connect_redis(config)

    # ---- Heartbeat -----------------------------------------------------
    heartbeat = HeartbeatThread(config, rc)
    heartbeat.start()

    # ---- Graceful shutdown ---------------------------------------------
    shutdown_flag = False

    def _handle_signal(signum: int, _frame: Optional[FrameType]) -> None:
        nonlocal shutdown_flag
        sig_name = signal.Signals(signum).name
        logger.info("Received %s -- initiating graceful shutdown", sig_name)
        shutdown_flag = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ---- Main loop -----------------------------------------------------
    queue_key = f"rp:tasks:{config.project_id}:{config.user_id}"
    logger.info("Listening on queue: %s", queue_key)

    try:
        while not shutdown_flag:
            try:
                result = rc.brpop(queue_key, timeout=_BRPOP_TIMEOUT)
            except redis.ConnectionError as exc:
                logger.warning("Redis connection lost: %s -- reconnecting", exc)
                rc = _connect_redis(config)
                heartbeat._redis = rc  # update reference in heartbeat thread
                continue

            if result is None:
                # BRPOP timed out -- no task available, loop again.
                continue

            _key, raw_task = result

            try:
                task: dict = json.loads(raw_task)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error("Invalid task payload, skipping: %s", exc)
                continue

            task_id = task.get("task_id", "unknown")
            logger.info("Received task %s from queue", task_id)

            # Mark busy
            heartbeat.set_busy(task_id)

            # Execute
            task_result = execute_task(task, config, rc)

            # Push result
            result_key = f"rp:results:{task_id}"
            try:
                rc.rpush(
                    result_key,
                    json.dumps(task_result.to_dict()),
                )
                rc.expire(result_key, 86400)  # expire after 24 h
                logger.info(
                    "Result for task %s pushed to %s: %s",
                    task_id,
                    result_key,
                    task_result.status,
                )
            except redis.RedisError as exc:
                logger.error(
                    "Failed to push result for task %s: %s", task_id, exc
                )

            # Mark idle
            heartbeat.set_idle()

    except Exception:
        logger.exception("Unhandled exception in main loop")
    finally:
        logger.info("Shutting down worker")
        heartbeat.stop()
        rc.close()
        logger.info("Worker stopped")


# Allow running directly: python -m worker.daemon
if __name__ == "__main__":
    run()
