"""worker-training registers every step routed to the ``training`` queue (#183).

``step.auto_label`` declares ``queue = "training"`` but the worker used to
register only ``TrainStep``, so every auto_label job failed with "Unknown step
type" and the orphan sweep never covered it.
"""

from __future__ import annotations

import pytest

from cvops_api.core.registry import registry
from cvops_api.engine.coordinator import queue_for

from cvops_steps.model_runners import get_runner
from worker_training.main import STREAM, setup_registry


@pytest.fixture
def step_types() -> list[str]:
    return setup_registry()


def test_consumes_train_and_auto_label(step_types: list[str]) -> None:
    assert "step.train" in step_types
    assert "step.auto_label" in step_types


def test_step_list_is_exactly_the_training_queue(step_types: list[str]) -> None:
    """Derived from each step's queue, not hand-kept: every registered step
    routed to ``training`` is consumed (and orphan-swept), and nothing else."""
    expected = sorted(r.type_key for r in registry.all() if queue_for(r.impl) == STREAM)
    assert step_types == expected
    assert "step.extract_frames" not in step_types  # a preprocessing step


def test_auto_label_resolves_with_its_model_runners(step_types: list[str]) -> None:
    """Registering AutoLabelStep alone would fail at run time with
    "Unknown model runner: 'yolo'"; register_all also registers the runners.
    get_runner only looks the runner up, so ultralytics is never imported."""
    assert registry.resolve("step.auto_label").impl.queue == STREAM
    assert get_runner("yolo").name == "yolo"
