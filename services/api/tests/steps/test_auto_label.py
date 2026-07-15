"""Unit test for AutoLabelStep — mocks storage, DB, and YOLO; no real inference.

Heavy ML deps (ultralytics, PIL, numpy) are not installed in the API test env
so they are injected into sys.modules as lightweight fakes before the step's
lazy imports resolve.
"""

from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cvops_api.engine.step import StepContext


class _FakeStorage:
    async def get_bytes(self, blob_hash: str) -> bytes:
        return b"\xff\xd8\xff\xe0fake-bytes"


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


async def test_auto_label_writes_annotation_revisions():
    """AutoLabelStep calls YOLO and writes one annotation_revision per sample."""
    from cvops_steps.auto_label import AutoLabelStep

    sample_id = str(uuid.uuid4())
    weights_hash = "a" * 64
    img_hash = "b" * 64
    ont_id = str(uuid.uuid4())

    ctx = _ctx()

    # Five sequential execute() calls: mv lookup, samples, ontology, rev_no, INSERT
    ctx.session.execute = AsyncMock(
        side_effect=[
            MagicMock(first=MagicMock(return_value=(weights_hash,))),
            MagicMock(all=MagicMock(return_value=[(sample_id, img_hash, 640, 480)])),
            MagicMock(first=MagicMock(return_value=(ont_id, 1))),
            MagicMock(scalar=MagicMock(return_value=1)),
            MagicMock(),
        ]
    )

    # Fake YOLO: one detection per image
    fake_box = MagicMock()
    fake_box.xyxy = [MagicMock(tolist=MagicMock(return_value=[10.0, 20.0, 100.0, 80.0]))]
    fake_box.conf = [0.9]
    fake_box.cls = [0]
    fake_results = MagicMock()
    fake_results.boxes = [fake_box]
    fake_model = MagicMock()
    fake_model.names = {0: "car"}
    fake_model.return_value = [fake_results]

    mock_ultralytics = types.ModuleType("ultralytics")
    mock_ultralytics.YOLO = MagicMock(return_value=fake_model)

    mock_pil_image = MagicMock()
    mock_pil = types.ModuleType("PIL")
    mock_pil.Image = mock_pil_image

    mock_numpy = types.ModuleType("numpy")
    mock_numpy.array = MagicMock(return_value=MagicMock())

    with patch.dict(
        sys.modules,
        {
            "ultralytics": mock_ultralytics,
            "PIL": mock_pil,
            "PIL.Image": mock_pil_image,
            "numpy": mock_numpy,
        },
    ):
        step = AutoLabelStep()
        result = await step.run(
            ctx,
            config={"model_version_id": str(uuid.uuid4()), "confidence_threshold": 0.3},
            inputs={"sample_ids": [sample_id]},
        )

    assert "annotation_revision_ids" in result
    assert len(result["annotation_revision_ids"]) == 1


async def test_auto_label_empty_sample_ids_returns_early():
    """Empty sample_ids returns immediately without touching the DB."""
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
