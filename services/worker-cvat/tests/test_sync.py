"""Pull-flow test for worker-cvat: a CVAT completion sync writes human-reviewed
annotation_revisions, completes the labeling_job, and resumes the gated run.

The CVAT REST call is faked (sys.modules injection of cvops_cvat_client), the
handler's session factory is pointed at the testcontainers DB, and
advance_workflow is mocked so we assert the chain is triggered without running
the coordinator. Requires Docker.
"""

from __future__ import annotations

import sys
import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.auth import Org
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.labeling import LabelingJob
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.samples import DataSource, Sample

import worker_cvat.sync as sync

MODEL_PRELABEL = [{"class_key": "person", "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.2]}}]
HUMAN_REVIEWED = [{"class_key": "person", "geometry": {"type": "bbox", "coords": [0.4, 0.4, 0.3, 0.3]}}]
CVAT_TASK_ID = 4242


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _seed(session_factory) -> dict:
    async with session_factory() as s:
        org = Org(name=f"org-{_uid()}")
        s.add(org)
        await s.flush()
        proj = Project(org_id=org.id, name=f"p-{_uid()}")
        s.add(proj)
        await s.flush()
        blob = Blob(
            hash=f"sha256:{'b' * 56}{_uid()}",
            storage_backend="garage",
            storage_key=f"blobs/bb/{_uid()}",
            size_bytes=10,
            media_type="image/jpeg",
        )
        src = DataSource(project_id=proj.id, type="video")
        ont = Ontology(org_id=org.id, name=f"o-{_uid()}")  # ontologies are org-scoped
        s.add_all([blob, src, ont])
        await s.flush()
        sample = Sample(
            project_id=proj.id, source_id=src.id, blob_hash=blob.hash, width=640, height=480
        )
        s.add(sample)
        await s.flush()
        # Prior model revision → ontology context the human revision inherits.
        prior = AnnotationRevision(
            project_id=proj.id,
            sample_id=sample.id,
            ontology_id=ont.id,
            ontology_version=1,
            revision_no=1,
            payload=MODEL_PRELABEL,
            provenance={"source": "model", "review_status": "pending"},
        )
        s.add(prior)
        await s.flush()

        parent = Run(project_id=proj.id, kind="workflow", status="running")
        s.add(parent)
        await s.flush()
        lj_id = uuid.uuid4()
        child = Run(
            project_id=proj.id,
            parent_run_id=parent.id,
            kind="step",
            status="waiting",
            step_id="review_node",
            step_type="step.human_review",
            output_refs={
                "gate_data": {
                    "sample_ids": [str(sample.id)],
                    "labeling_job_id": str(lj_id),
                    "cvat_task_id": CVAT_TASK_ID,
                }
            },
        )
        s.add(child)
        await s.flush()
        s.add(
            LabelingJob(
                id=lj_id,
                project_id=proj.id,
                run_id=child.id,
                step_id="review_node",
                cvat_task_id=CVAT_TASK_ID,
                cvat_job_ids=[99],
                status="pushed",
                sample_count=1,
                annotation_revision_ids_in=[str(prior.id)],
            )
        )
        await s.commit()
        return {
            "sample_id": str(sample.id),
            "child_id": str(child.id),
            "parent_id": str(parent.id),
            "labeling_job_id": str(lj_id),
        }


@pytest.fixture
def fake_pull():
    """Inject cvops_cvat_client.pull_review_task returning one reviewed frame."""
    mod = types.ModuleType("cvops_cvat_client")

    def pull_review_task(task_id, frame_dims):
        assert task_id == CVAT_TASK_ID
        return {0: HUMAN_REVIEWED}  # frame 0 → the single pushed sample

    mod.pull_review_task = pull_review_task
    with patch.dict(sys.modules, {"cvops_cvat_client": mod}):
        yield


async def test_pull_writes_revision_completes_job_and_resumes(
    session_factory, fake_pull, monkeypatch
):
    ids = await _seed(session_factory)

    advance = AsyncMock()
    monkeypatch.setattr(sync, "async_session_factory", session_factory)
    monkeypatch.setattr(sync, "advance_workflow", advance)

    await sync.handle_cvat_sync({"kind": "cvat_sync", "cvat_task_id": str(CVAT_TASK_ID)})

    async with session_factory() as s:
        # A human/accepted revision was appended (revision_no 2, parent = prior).
        rev = (
            await s.execute(
                text(
                    "SELECT revision_no, payload, provenance FROM annotation_revisions "
                    "WHERE sample_id = CAST(:s AS uuid) AND provenance->>'source' = 'human'"
                ),
                {"s": ids["sample_id"]},
            )
        ).first()
        assert rev is not None
        assert rev[0] == 2
        assert rev[1] == HUMAN_REVIEWED
        assert rev[2]["review_status"] == "accepted"

        lj = (
            await s.execute(
                text(
                    "SELECT status, annotation_revision_ids_out FROM labeling_jobs "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": ids["labeling_job_id"]},
            )
        ).first()
        assert lj[0] == "completed"
        assert len(lj[1]) == 1

        child = (
            await s.execute(
                text("SELECT status, output_refs FROM runs WHERE id = CAST(:id AS uuid)"),
                {"id": ids["child_id"]},
            )
        ).first()
        assert child[0] == "succeeded"
        assert child[1]["resolution"] == "approved"
        assert len(child[1]["annotation_revision_ids"]) == 1

    # The workflow was advanced from the parent so downstream steps enqueue.
    advance.assert_awaited_once()
    assert str(advance.await_args.args[1]) == ids["parent_id"]


async def test_pull_is_idempotent_when_already_completed(session_factory, fake_pull, monkeypatch):
    ids = await _seed(session_factory)
    async with session_factory() as s:
        await s.execute(
            text("UPDATE labeling_jobs SET status='completed' WHERE id = CAST(:id AS uuid)"),
            {"id": ids["labeling_job_id"]},
        )
        await s.commit()

    advance = AsyncMock()
    monkeypatch.setattr(sync, "async_session_factory", session_factory)
    monkeypatch.setattr(sync, "advance_workflow", advance)

    await sync.handle_cvat_sync({"kind": "cvat_sync", "cvat_task_id": str(CVAT_TASK_ID)})

    # Short-circuited: no new revisions, no advance.
    advance.assert_not_awaited()
    async with session_factory() as s:
        n = (
            await s.execute(
                text(
                    "SELECT count(*) FROM annotation_revisions "
                    "WHERE sample_id = CAST(:s AS uuid)"
                ),
                {"s": ids["sample_id"]},
            )
        ).scalar()
        assert n == 1  # only the seeded model revision
