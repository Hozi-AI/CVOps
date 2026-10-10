"""Test fixtures for worker-cvat — testcontainers Postgres, mirroring the API
suite's conftest. Schema is created with Base.metadata.create_all (no Alembic),
so worker-cvat must be installed alongside cvops-api (its dependency).

Requires Docker.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from cvops_api.db.base import Base
import cvops_api.db.models  # noqa: F401 — populate Base.metadata with all tables


@pytest.fixture(scope="session")
def postgres_url() -> str:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url()
        url = url.replace("+psycopg2", "+asyncpg").replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        yield url


@pytest.fixture(scope="session", autouse=True)
def create_test_schema(postgres_url: str) -> None:
    async def _setup() -> None:
        engine = create_async_engine(postgres_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


@pytest.fixture
async def session_factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


# ── Shared seeding + CVAT-client stub (used by the pull-flow tests) ──────────

CVAT_TASK_ID = 4242
MODEL_PRELABEL = [
    {"class_key": "person", "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.2]}}
]
HUMAN_REVIEWED = [
    {"class_key": "person", "geometry": {"type": "bbox", "coords": [0.4, 0.4, 0.3, 0.3]}}
]


def _uid() -> str:
    return uuid.uuid4().hex[:8]


_task_seq = CVAT_TASK_ID


def _next_task_id() -> int:
    """Unique cvat_task_id per seeded job — handle_cvat_sync selects by task id
    with .first(), so colliding ids across tests would cross-wire the lookup."""
    global _task_seq
    _task_seq += 1
    return _task_seq


async def seed_review(
    session_factory,
    *,
    cvat_task_id: int | None = None,
    with_prior_revision: bool = True,
    sample_ids_override: list[str] | None = None,
    job_status: str = "pushed",
) -> dict:
    """Seed an org/project/sample plus a pushed labeling_job parked at a gate.

    Knobs let the edge-case tests omit the prior revision (no ontology to
    inherit) or blank out gate sample_ids without copy-pasting the whole graph.
    Returns the (unique) ``cvat_task_id`` so the caller drives the right job.
    """
    if cvat_task_id is None:
        cvat_task_id = _next_task_id()
    from cvops_api.db.models.annotations import AnnotationRevision
    from cvops_api.db.models.auth import Org
    from cvops_api.db.models.blobs import Blob
    from cvops_api.db.models.labeling import LabelingJob
    from cvops_api.db.models.ontologies import Ontology
    from cvops_api.db.models.projects import Project
    from cvops_api.db.models.runs import Run
    from cvops_api.db.models.samples import DataSource, Sample

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
        proj.default_ontology_id = ont.id  # the project ontology pull falls back to
        await s.flush()
        sample = Sample(
            project_id=proj.id, source_id=src.id, blob_hash=blob.hash, width=640, height=480
        )
        s.add(sample)
        await s.flush()

        prior_id = None
        in_rev = []
        if with_prior_revision:
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
            prior_id = str(prior.id)
            in_rev = [prior_id]

        gate_sample_ids = (
            sample_ids_override
            if sample_ids_override is not None
            else [str(sample.id)]
        )

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
                    "sample_ids": gate_sample_ids,
                    "labeling_job_id": str(lj_id),
                    "cvat_task_id": cvat_task_id,
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
                cvat_task_id=cvat_task_id,
                cvat_job_ids=[99],
                status=job_status,
                sample_count=1,
                annotation_revision_ids_in=in_rev,
            )
        )
        await s.commit()
        return {
            "sample_id": str(sample.id),
            "child_id": str(child.id),
            "parent_id": str(parent.id),
            "labeling_job_id": str(lj_id),
            "prior_revision_id": prior_id,
            "cvat_task_id": cvat_task_id,
            "ontology_id": str(ont.id),
        }


@pytest.fixture
def fake_pull():
    """Inject cvops_cvat_client.pull_review_task returning one reviewed frame."""
    mod = types.ModuleType("cvops_cvat_client")

    def pull_review_task(task_id, frame_dims):
        return {0: HUMAN_REVIEWED}  # frame 0 → the single pushed sample

    mod.pull_review_task = pull_review_task
    with patch.dict(sys.modules, {"cvops_cvat_client": mod}):
        yield


@pytest.fixture
def fake_pull_factory():
    """Like fake_pull, but the test supplies the by-frame mapping to return."""

    def _install(by_frame: dict):
        mod = types.ModuleType("cvops_cvat_client")
        mod.pull_review_task = lambda task_id, frame_dims: by_frame
        return patch.dict(sys.modules, {"cvops_cvat_client": mod})

    return _install
