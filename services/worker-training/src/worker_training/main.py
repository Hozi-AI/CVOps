from __future__ import annotations

import asyncio
import logging

from cvops_api.core.registry import registry
from cvops_api.engine.coordinator import queue_for
from cvops_worker_common import ConsumerLoop

from cvops_steps import register_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

STREAM = "training"


def setup_registry() -> list[str]:
    """Register every step (and its model runners) and return this worker's share.

    ``register_all()`` also registers the model runners that ``step.auto_label``
    looks up by name, so registering a step class alone is not enough. Which
    steps this worker consumes is derived from each step's ``queue`` rather than
    a hand-kept list, so a step routed to ``training`` later is picked up (and
    covered by the orphan sweep) automatically.
    """
    register_all()
    return sorted(reg.type_key for reg in registry.all() if queue_for(reg.impl) == STREAM)


def main() -> None:
    step_types = setup_registry()
    logger.info("worker-training consuming stream=%r steps=%s", STREAM, step_types)
    loop = ConsumerLoop(stream=STREAM, step_types=step_types)
    asyncio.run(loop.run_forever())


if __name__ == "__main__":
    main()
