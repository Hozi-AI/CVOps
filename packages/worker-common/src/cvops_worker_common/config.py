from __future__ import annotations

import os


class WorkerSettings:
    """Worker-specific env vars. Shared infra vars (DATABASE_URL, S3_*, REDIS_URL)
    are read via cvops_api.config.settings — no duplication."""

    REDIS_STREAM: str = os.environ.get("REDIS_STREAM", "")
    WORKER_TOKEN: str = os.environ.get("WORKER_TOKEN", "")
    API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://api:8000")
    WORKER_CONCURRENCY: int = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    ORPHAN_RECOVERY_INTERVAL: int = int(os.environ.get("ORPHAN_RECOVERY_INTERVAL", "60"))
    ORPHAN_PENDING_AGE_SECONDS: int = int(os.environ.get("ORPHAN_PENDING_AGE_SECONDS", "30"))
    # A message whose handler raised is retried this many times before it is
    # parked on the ``<stream>:dlq`` stream.
    WORKER_MAX_RETRIES: int = int(os.environ.get("WORKER_MAX_RETRIES", "3"))
    # Seconds to wait before retry 1, 2, 3, …; the last value repeats.
    WORKER_RETRY_DELAYS: tuple[float, ...] = tuple(
        float(s) for s in os.environ.get("WORKER_RETRY_DELAYS", "1,5,30").split(",") if s.strip()
    )


worker_settings = WorkerSettings()
