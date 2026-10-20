"""Round-trip: auto_label (YOLO runner geometry) → annotation_revisions → export_yolo.

Guards the canonical bbox format (#184): the runner's pixel corners must land in the
revision payload as normalised ``[cx, cy, w, h]`` so the exported YOLO label matches.
Only inference is faked; the coordinate conversion is the real runner helper.
"""

from __future__ import annotations

import io
import uuid

from moto import mock_aws
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.engine.step import StepContext
from cvops_steps.auto_label import AutoLabelStep
from cvops_steps.export_yolo import ExportYoloStep
from cvops_steps.model_runners import _registry, register_runner
from cvops_steps.model_runners.base import ModelRunner
from cvops_steps.model_runners.yolo import xyxy_to_norm_cxcywh
from tests.steps.test_export_yolo import _emit, _mocked_backend, _moto_settings, _untar

IMG_W, IMG_H = 100, 50
# Pixel corners as ultralytics' ``box.xyxy`` would report them.
PIXEL_BOX = (10.0, 10.0, 50.0, 40.0)


class _PixelBoxRunner(ModelRunner):
    name = "yolo"

    async def predict(self, sample_id, blob_hash, modality, model_bytes, config, storage):
        return [
            {
                "class_key": "dog",
                "geometry": {
                    "type": "bbox",
                    "coords": xyxy_to_norm_cxcywh(*PIXEL_BOX, IMG_W, IMG_H),
                },
                "confidence": 0.9,
                "attributes": {},
            }
        ]


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (IMG_W, IMG_H), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


async def _insert_blob(session: AsyncSession, blob_hash: str, media_type: str) -> None:
    await session.execute(
        text(
            "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, media_type) "
            "VALUES (:h, 'garage', :k, 10, :m) ON CONFLICT (hash) DO NOTHING"
        ),
        {"h": blob_hash, "k": f"blobs/{blob_hash[7:9]}", "m": media_type},
    )


async def test_auto_label_output_exports_correct_yolo_label(session: AsyncSession) -> None:
    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        backend = _mocked_backend()

        org_id, proj_id, ont_id, ds_id, src_id, sample_id, mv_id, commit_id = (
            uuid.uuid4() for _ in range(8)
        )
        await session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
            {"i": org_id, "n": f"org-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
            {"i": proj_id, "o": org_id, "n": f"proj-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
            {"i": ont_id, "o": org_id},
        )
        for key, order in [("cat", 0), ("dog", 1)]:
            await session.execute(
                text(
                    "INSERT INTO label_classes (id, ontology_id, class_key, display_name, "
                    "sort_order) VALUES (:i, :o, :k, :k, :s)"
                ),
                {"i": uuid.uuid4(), "o": ont_id, "k": key, "s": order},
            )
        await session.execute(
            text("INSERT INTO datasets (id, project_id, name) VALUES (:i, :p, 'ds')"),
            {"i": ds_id, "p": proj_id},
        )
        await session.execute(
            text(
                "INSERT INTO data_sources (id, project_id, type, status) "
                "VALUES (:i, :p, 'video', 'ingested')"
            ),
            {"i": src_id, "p": proj_id},
        )

        image_hash = await backend.save_bytes(_jpeg(), "image/jpeg")
        weights_hash = await backend.save_bytes(b"fake-weights", "application/octet-stream")
        await _insert_blob(session, image_hash, "image/jpeg")
        await _insert_blob(session, weights_hash, "application/octet-stream")
        await session.execute(
            text(
                "INSERT INTO samples (id, project_id, blob_hash, source_id, width, height, "
                "frame_index) VALUES (:i, :p, :b, :s, :w, :h, 0)"
            ),
            {"i": sample_id, "p": proj_id, "b": image_hash, "s": src_id, "w": IMG_W, "h": IMG_H},
        )
        await session.execute(
            text("INSERT INTO model_versions (id, project_id, blob_hash) VALUES (:i, :p, :b)"),
            {"i": mv_id, "p": proj_id, "b": weights_hash},
        )

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=str(proj_id),
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=_emit,
        )

        register_runner(_PixelBoxRunner())
        try:
            labelled = await AutoLabelStep().run(
                ctx,
                config={
                    "model_version_id": str(mv_id),
                    "ontology_id": str(ont_id),
                    "confidence_threshold": 0.3,
                },
                inputs={"sample_ids": [str(sample_id)]},
            )
        finally:
            _registry.pop("yolo", None)

        [rev_id] = labelled["annotation_revision_ids"]
        provenance = (
            await session.execute(
                text("SELECT provenance FROM annotation_revisions WHERE id = CAST(:i AS uuid)"),
                {"i": rev_id},
            )
        ).scalar_one()
        assert provenance["model_version_id"] == str(mv_id)
        assert provenance["threshold"] == 0.3

        # Commit the auto-labelled revision and export it.
        await session.execute(
            text(
                "INSERT INTO commits (id, project_id, dataset_id, ontology_id, "
                "ontology_version, message) VALUES (:i, :p, :d, :o, 1, 'c')"
            ),
            {"i": commit_id, "p": proj_id, "d": ds_id, "o": ont_id},
        )
        await session.execute(
            text(
                "INSERT INTO commit_samples (commit_id, sample_id, annotation_revision_id, "
                "split) VALUES (:c, :s, CAST(:r AS uuid), 'train')"
            ),
            {"c": commit_id, "s": sample_id, "r": rev_id},
        )
        await session.flush()

        result = await ExportYoloStep().run(ctx, {}, {"commit_id": str(commit_id)})
        members = _untar(await backend.get_bytes(result["export_blob_hash"]))

    label = members[f"labels/train/{sample_id}.txt"].decode().split()
    # "dog" is class 1; corners (10,10)-(50,40) on 100x50 → centre (0.3, 0.5), size (0.4, 0.6).
    assert label == ["1", "0.300000", "0.500000", "0.400000", "0.600000"]
