"""Worker configuration loaded from environment variables."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _resolve_pod_id() -> str:
    """Resolve the pod ID from RUNPOD_POD_ID env var, falling back to hostname."""
    pod_id = os.environ.get("RUNPOD_POD_ID")
    if pod_id:
        return pod_id
    hostname = socket.gethostname()
    logger.warning(
        "RUNPOD_POD_ID not set, using hostname as pod_id: %s", hostname
    )
    return hostname


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable worker configuration."""

    redis_url: str
    project_id: str
    user_id: str
    pod_id: str
    houdini_path: str = "/workspace/houdini"
    project_dir: str = "/workspace/projects"
    heartbeat_interval: int = 10
    log_level: str = "INFO"
    max_retries: int = 3
    task_timeout: int = 3600

    @classmethod
    def from_env(cls) -> WorkerConfig:
        """Build configuration from environment variables.

        Raises:
            SystemExit: If required environment variables are missing.
        """
        missing: list[str] = []

        redis_url = os.environ.get("REDIS_URL", "")
        if not redis_url:
            missing.append("REDIS_URL")

        if missing:
            logger.error(
                "Required environment variables are not set: %s",
                ", ".join(missing),
            )
            raise SystemExit(1)

        # PROJECT_ID and USER_ID are set by the HDA when launching pods.
        # Default to "default" for manual testing / template validation.
        project_id = os.environ.get("PROJECT_ID", "default")
        user_id = os.environ.get("USER_ID", "default")

        config = cls(
            redis_url=redis_url,
            project_id=project_id,
            user_id=user_id,
            pod_id=_resolve_pod_id(),
            houdini_path=os.environ.get("HOUDINI_PATH", "/workspace/houdini"),
            project_dir=os.environ.get("PROJECT_DIR", "/workspace/projects"),
            heartbeat_interval=int(
                os.environ.get("HEARTBEAT_INTERVAL", "10")
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            max_retries=int(os.environ.get("MAX_RETRIES", "3")),
            task_timeout=int(os.environ.get("TASK_TIMEOUT", "3600")),
        )

        logger.info("Worker configuration loaded:")
        logger.info("  pod_id          = %s", config.pod_id)
        logger.info("  project_id      = %s", config.project_id)
        logger.info("  user_id         = %s", config.user_id)
        logger.info("  houdini_path    = %s", config.houdini_path)
        logger.info("  project_dir     = %s", config.project_dir)
        logger.info("  heartbeat_interval = %ds", config.heartbeat_interval)
        logger.info("  task_timeout    = %ds", config.task_timeout)
        logger.info("  max_retries     = %d", config.max_retries)
        logger.info("  log_level       = %s", config.log_level)

        return config
