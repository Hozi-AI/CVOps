"""Unit test for AutoLabelStep — mocks storage, DB, and the runner registry."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from cvops_api.engine.step import StepContext
from cvops_steps.model_runners import _registry, register_runner
from cvops_steps.model_runners.base import ModelRunner


class _FakeStorage:
    async def get_bytes(self, blob_hash: str) -> bytes:
        return b"fake-bytes"


async def _emit(**kw):
    return None


def _ctx() -> StepContext:
    return StepContext(
        session=MagicMock(),
        storage=_FakeStorage(),
        project_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        emit_event=_emit,
    )


class _FakeYoloRunner(ModelRunner):
    name = "yolo"

    async def predict(self, sample_id, blob_hash, modality, model_bytes, config, storage):
        return [{"class_key": "car", "confidence": 0.9, "geometry": {"type": "bbox", "coords": [0.1, 0.2, 0.5, 0.6]}}]


async def test_auto_label_writes_annotation_revisions():
    """AutoLabelStep calls the runner and writes one annotation_revision per sample."""
    from cvops_steps.auto_label import AutoLabelStep

    sample_id = str(uuid.uuid4())
    weights_hash = "a" * 64
    img_hash = "b" * 64
    ont_id = str(uuid.uuid4())

    ctx = _ctx()
    ctx.session.execute = AsyncMock(
        side_effect=[
            MagicMock(first=MagicMock(return_value=(weights_hash,))),
            MagicMock(fetchall=MagicMock(return_value=[(sample_id, img_hash, "image")])),
            MagicMock(first=MagicMock(return_value=(ont_id, 1))),
            MagicMock(scalar=MagicMock(return_value=1)),
            MagicMock(),
        ]
    )

    register_runner(_FakeYoloRunner())
    try:
        result = await AutoLabelStep().run(
            ctx,
            config={"model_version_id": str(uuid.uuid4()), "confidence_threshold": 0.3},
            inputs={"sample_ids": [sample_id]},
        )
    finally:
        _registry.pop("yolo", None)

    assert "annotation_revision_ids" in result
    assert len(result["annotation_revision_ids"]) == 1


async def test_auto_label_empty_sample_ids_returns_early():
    """Empty sample_ids returns immediately without touching the DB or runner."""
    from cvops_steps.auto_label import AutoLabelStep

    ctx = _ctx()
    ctx.session.execute = AsyncMock()

    result = await AutoLabelStep().run(
        ctx,
        config={"model_version_id": str(uuid.uuid4())},
        inputs={"sample_ids": []},
    )

    assert result == {"annotation_revision_ids": []}
    ctx.session.execute.assert_not_called()
