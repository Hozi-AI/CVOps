"""Redis-Streams preprocessing worker.

Consumes the ``preprocessing`` stream through worker-common's ``ConsumerLoop``,
the same loop every worker runs: a named consumer group (so replicas don't
double-process), the shared ``process_step`` coordinator via ``run_job``,
retries with back-off, a dead-letter queue (``preprocessing:dlq``), and orphan
recovery. Postgres is the authority for job state; Redis is just the doorbell.

Connects to PostgreSQL and Garage directly through ``cvops_api`` settings — it
never calls the API.
"""

from __future__ import annotations

import asyncio
import logging

from cvops_worker_common import ConsumerLoop
from cvops_worker_common.config import worker_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

STREAM = worker_settings.REDIS_STREAM or "preprocessing"


def setup_registry() -> None:
    """Register the step implementations this worker can run."""
    try:
        from cvops_steps import register_all

        register_all()
    except ImportError:
        logger.warning("cvops_steps not importable — registry is empty")


def build_loop() -> ConsumerLoop:
    # No step list: the loop consumes every registered step routed to this
    # stream (an empty ``queue`` means preprocessing).
    return ConsumerLoop(stream=STREAM)


def main() -> None:
    setup_registry()
    asyncio.run(build_loop().run_forever())


if __name__ == "__main__":
    main()
