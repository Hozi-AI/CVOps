"""Wiring tests for the preprocessing worker.

The consume loop itself (dispatch, retries, DLQ, concurrency, orphan recovery)
is worker-common's ``ConsumerLoop`` and is tested there; the claim path against
a real Postgres lives in ``services/api/tests/worker/test_worker_loop.py``.
Here we only check that this worker runs that loop on the right stream with
the right steps.
"""

from __future__ import annotations

import sys

from cvops_worker_common import ConsumerLoop, run_job

import cvops_worker.worker as worker


def test_stream_defaults_to_preprocessing() -> None:
    assert worker.STREAM == "preprocessing"


def test_build_loop_uses_the_shared_consumer_with_retries() -> None:
    loop = worker.build_loop()
    assert isinstance(loop, ConsumerLoop)
    assert loop.stream == "preprocessing"
    assert loop.group == "worker-preprocessing"
    assert loop.handler is run_job
    assert loop.max_retries >= 1
    # Derived from the registry when the loop starts, not hand-kept here.
    assert loop.step_types is None


def test_consumes_the_preprocessing_steps_only() -> None:
    worker.setup_registry()
    steps = worker.build_loop().my_step_types()
    assert {"step.extract_frames", "step.commit_dataset", "step.export_yolo"} <= set(steps)
    assert "step.train" not in steps  # training queue
    assert "step.auto_label" not in steps  # training queue
    assert "step.human_review" not in steps  # cvat queue


def test_setup_registry_survives_missing_steps_package(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "cvops_steps", None)  # import → ImportError
    worker.setup_registry()  # logs a warning, doesn't raise
