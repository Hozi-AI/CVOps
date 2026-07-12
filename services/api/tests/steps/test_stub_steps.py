"""Metadata + not-yet-implemented contract for the labeling/train steps.

These tests lock in the parts the engine and registry depend on regardless of
how built-out each step is: their type_keys, that human_review is a gate, that
each carries a config_schema, and that register_all() wires all steps.

Implementation status (dev): ``auto_label`` is the only remaining clean stub —
its run() raises NotImplementedError. ``human_review`` and ``train`` are
implemented (human_review drives an external CVAT client; train clones and runs
a trainer repo), so their run() does real work and is exercised by their own
integration paths, not by a NotImplementedError assertion here.
"""

from __future__ import annotations

import uuid

import pytest

from cvops_api.core.registry import Registry
from cvops_api.engine.step import Step, StepContext
from cvops_steps import register_all
from cvops_steps.auto_label import AutoLabelStep
from cvops_steps.commit_dataset import CommitDatasetStep
from cvops_steps.export_yolo import ExportYoloStep
from cvops_steps.extract_frames import ExtractFramesStep
from cvops_steps.human_review import HumanReviewStep
from cvops_steps.train import TrainStep

# All three labeling/train steps share registration-metadata expectations.
LABELING_TRAIN_STEPS = [AutoLabelStep, HumanReviewStep, TrainStep]
# Only auto_label is still a clean stub whose run() raises NotImplementedError.
NOT_YET_IMPLEMENTED = [AutoLabelStep]


def _ctx() -> StepContext:
    """Minimal context — auto_label raises before touching session/storage."""
    return StepContext(
        session=None,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        project_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        emit_event=lambda *a, **k: None,
    )


# --------------------------------------------------------------------------- #
# type_key / gate / category metadata                                         #
# --------------------------------------------------------------------------- #


def test_stub_type_keys() -> None:
    assert AutoLabelStep.type_key == "step.auto_label"
    assert HumanReviewStep.type_key == "step.human_review"
    assert TrainStep.type_key == "step.train"


def test_human_review_is_a_gate() -> None:
    assert HumanReviewStep.is_gate is True


def test_auto_label_and_train_are_not_gates() -> None:
    assert AutoLabelStep.is_gate is False
    assert TrainStep.is_gate is False


@pytest.mark.parametrize("step_cls", LABELING_TRAIN_STEPS)
def test_stub_has_config_schema(step_cls) -> None:
    schema = step_cls.config_schema
    assert isinstance(schema, dict)
    assert schema, f"{step_cls.__name__} config_schema must not be empty"


def test_human_review_schema_shape() -> None:
    """The hand-written schema (not loaded from a JSON file) keeps its keys."""
    props = HumanReviewStep.config_schema["properties"]
    assert "labeling_backend" in props
    assert "assignees" in props


@pytest.mark.parametrize("step_cls", LABELING_TRAIN_STEPS)
def test_stub_is_a_step_subclass(step_cls) -> None:
    assert issubclass(step_cls, Step)


# --------------------------------------------------------------------------- #
# run() raises NotImplementedError (auto_label only — the remaining stub)      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("step_cls", NOT_YET_IMPLEMENTED)
async def test_stub_run_raises_not_implemented(step_cls) -> None:
    with pytest.raises(NotImplementedError):
        await step_cls().run(_ctx(), {}, {})


# --------------------------------------------------------------------------- #
# register_all() wiring                                                       #
# --------------------------------------------------------------------------- #


def test_register_all_registers_all_steps(monkeypatch) -> None:
    """register_all() populates the shared registry with every step keyed by
    type_key. Patch in a throwaway Registry so the global one is untouched."""
    import cvops_steps as steps_pkg
    from cvops_steps.import_dataset import ImportDatasetStep

    fresh = Registry()
    monkeypatch.setattr(steps_pkg, "registry", fresh)
    register_all()

    expected = {
        ExtractFramesStep.type_key,
        AutoLabelStep.type_key,
        HumanReviewStep.type_key,
        CommitDatasetStep.type_key,
        ExportYoloStep.type_key,
        TrainStep.type_key,
        ImportDatasetStep.type_key,
    }
    assert {r.type_key for r in fresh.all()} == expected


def test_registered_stubs_resolve_to_their_impl(monkeypatch) -> None:
    import cvops_steps as steps_pkg

    fresh = Registry()
    monkeypatch.setattr(steps_pkg, "registry", fresh)
    register_all()

    assert isinstance(fresh.resolve("step.human_review").impl, HumanReviewStep)
    assert fresh.resolve("step.human_review").impl.is_gate is True
    # Stubs register under the default category like every other step.
    assert fresh.resolve("step.auto_label").category == "step"
