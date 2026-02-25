"""Heartbeat thread that reports pod status to Redis."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from typing import Any, Optional

import redis

from .config import WorkerConfig

logger = logging.getLogger(__name__)


def _get_gpu_info() -> list[dict[str, Any]]:
    """Query GPU information via nvidia-smi.

    Returns a list of dicts with name, memory_total_mb, memory_used_mb,
    utilization_pct, and temperature_c for each GPU.  Returns an empty
    list when nvidia-smi is not available.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        gpus: list[dict[str, Any]] = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append(
                    {
                        "name": parts[0],
                        "memory_total_mb": int(parts[1]),
                        "memory_used_mb": int(parts[2]),
                        "utilization_pct": int(parts[3]),
                        "temperature_c": int(parts[4]),
                    }
                )
        return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as exc:
        logger.debug("nvidia-smi unavailable: %s", exc)
        return []


class HeartbeatThread:
    """Daemon thread that periodically pushes status to Redis.

    The key ``rp:heartbeat:{pod_id}`` is set with a TTL of 30 seconds so
    that stale pods are automatically cleaned up if the heartbeat stops.
    """

    def __init__(
        self,
        config: WorkerConfig,
        redis_client: redis.Redis,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._status: str = "idle"
        self._current_task_id: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public helpers to update status from the main thread
    # ------------------------------------------------------------------

    def set_busy(self, task_id: str) -> None:
        """Mark the worker as busy with a specific task."""
        with self._lock:
            self._status = "busy"
            self._current_task_id = task_id
        logger.debug("Heartbeat status -> busy (task=%s)", task_id)

    def set_idle(self) -> None:
        """Mark the worker as idle."""
        with self._lock:
            self._status = "idle"
            self._current_task_id = None
        logger.debug("Heartbeat status -> idle")

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Heartbeat thread is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="heartbeat", daemon=True
        )
        self._thread.start()
        logger.info(
            "Heartbeat thread started (interval=%ds)",
            self._config.heartbeat_interval,
        )

    def stop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._config.heartbeat_interval + 2)
            if self._thread.is_alive():
                logger.warning("Heartbeat thread did not stop in time")
            else:
                logger.info("Heartbeat thread stopped")
        self._thread = None

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _build_payload(self) -> dict[str, Any]:
        with self._lock:
            status = self._status
            task_id = self._current_task_id

        return {
            "pod_id": self._config.pod_id,
            "status": status,
            "current_task_id": task_id,
            "gpu_info": _get_gpu_info(),
            "timestamp": time.time(),
        }

    def _send_heartbeat(self) -> None:
        key = f"rp:heartbeat:{self._config.pod_id}"
        payload = json.dumps(self._build_payload())
        try:
            self._redis.set(key, payload, ex=30)
        except redis.RedisError as exc:
            logger.warning("Failed to send heartbeat: %s", exc)

    def _run(self) -> None:
        """Loop body executed inside the daemon thread."""
        logger.debug("Heartbeat loop entered")
        while not self._stop_event.is_set():
            self._send_heartbeat()
            self._stop_event.wait(timeout=self._config.heartbeat_interval)

        # Send a final heartbeat so the controller sees the latest state
        # before the TTL expires.
        self._send_heartbeat()
        logger.debug("Heartbeat loop exited")
