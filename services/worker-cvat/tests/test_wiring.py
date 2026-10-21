"""Wiring / message-routing tests for worker-cvat — no DB needed.

worker-cvat consumes the ``cvat`` Redis stream through worker-common's
``ConsumerLoop`` (shared retries, DLQ and orphan recovery). Two doorbell kinds
arrive:
  * run doorbells ``{job_id, step_type}`` — normal step execution; covers
    ``step.human_review`` (push flow, parks at the gate) and ``step.deploy_model``;
  * ``{kind: "cvat_sync", cvat_task_id}`` — the webhook bridge signalling a CVAT
    task is done; routed to the pull handler which resumes the gate.

The generic ConsumerLoop contract (dispatch, retries, DLQ, background tasks) is
covered in ``packages/worker-common/tests``; here we assert only the
worker-cvat-specific wiring.
"""

from __future__ import annotations

import sys

from cvops_worker_common import ConsumerLoop, run_job

from worker_cvat import worker


def test_stream_is_cvat():
    assert worker.STREAM == "cvat"


def test_sync_handler_is_wired_into_worker():
    # worker.py folds the CVAT pull handler in alongside run-doorbell handling.
    from worker_cvat.sync import handle_cvat_sync

    assert worker.handle_cvat_sync is handle_cvat_sync


def test_build_loop_uses_the_shared_consumer_with_the_sync_handler():
    from worker_cvat.sync import handle_cvat_sync

    loop = worker.build_loop()
    assert isinstance(loop, ConsumerLoop)
    assert loop.stream == "cvat"
    assert loop.group == "worker-cvat"
    assert loop.handler is run_job
    # cvat_sync doorbells go to the pull handler — and so get retries, not a
    # straight trip to the DLQ for lacking a job_id.
    assert loop.sync_handler is handle_cvat_sync
    assert loop._reject_reason({"kind": "cvat_sync", "cvat_task_id": "7"}) is None
    assert loop.max_retries >= 1
    # The :8001 model-deployment API runs as a background task of the loop.
    assert loop.background == (worker._http_server_loop,)


def test_consumes_review_and_deploy_steps_only():
    worker.setup_registry()
    steps = worker.build_loop().my_step_types()
    assert {"step.human_review", "step.deploy_model"} <= set(steps)
    assert "step.extract_frames" not in steps  # preprocessing queue
    assert "step.train" not in steps  # training queue


def test_setup_registry_survives_missing_steps_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "cvops_steps", None)  # import → ImportError
    worker.setup_registry()  # logs a warning, still registers deploy_model
    from cvops_api.core.registry import registry

    assert registry.resolve("step.deploy_model").type_key == "step.deploy_model"


def test_human_review_step_routes_to_cvat_queue_and_is_a_gate():
    # The push step's queue is why its run doorbells land on worker-cvat's stream.
    from cvops_steps.human_review import HumanReviewStep

    assert HumanReviewStep.queue == "cvat"
    assert HumanReviewStep.is_gate is True
    assert HumanReviewStep.type_key == "step.human_review"


def test_deploy_model_step_routes_to_cvat_queue():
    # DeployModelStep is registered locally by worker-cvat; same stream as review.
    from worker_cvat.deploy_step import DeployModelStep

    assert DeployModelStep.queue == "cvat"
    assert DeployModelStep.type_key == "step.deploy_model"
