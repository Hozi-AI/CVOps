import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.annotations import AnnotationRevision
from tests.db.conftest import make_ontology, make_project, make_sample


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_payload() -> list:
    return [
        {
            "class_key": "car",
            "geometry": {"type": "bbox", "coords": [0.5, 0.3, 0.1, 0.1]},
            "confidence": 0.9,
            "track_id": None,
        }
    ]


def _base_provenance() -> dict:
    return {"source": "model", "review_status": "unreviewed"}


async def _make_revision(
    session: AsyncSession,
    project_id: uuid.UUID,
    sample_id: uuid.UUID,
    ontology_id: uuid.UUID,
    *,
    revision_no: int = 1,
    parent_revision_id: uuid.UUID | None = None,
    payload: list | None = None,
    provenance: dict | None = None,
) -> AnnotationRevision:
    rev = AnnotationRevision(
        project_id=project_id,
        sample_id=sample_id,
        ontology_id=ontology_id,
        ontology_version=1,
        revision_no=revision_no,
        parent_revision_id=parent_revision_id,
        payload=payload if payload is not None else _base_payload(),
        provenance=provenance if provenance is not None else _base_provenance(),
    )
    session.add(rev)
    return rev


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_annotation_revision_create(session: AsyncSession):
    """Creating a valid AnnotationRevision flushes successfully and stores revision_no."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev = await _make_revision(session, project.id, sample.id, ontology.id, revision_no=1)
    await session.flush()

    assert rev.id is not None
    assert rev.revision_no == 1


async def test_annotation_revision_no_deleted_at(session: AsyncSession):
    """AnnotationRevision is append-only and must not have a deleted_at attribute."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev = await _make_revision(session, project.id, sample.id, ontology.id)
    await session.flush()

    assert not hasattr(rev, "deleted_at")


async def test_annotation_revision_no_updated_at(session: AsyncSession):
    """AnnotationRevision is append-only and must not have an updated_at attribute."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev = await _make_revision(session, project.id, sample.id, ontology.id)
    await session.flush()

    assert not hasattr(rev, "updated_at")


async def test_annotation_revision_payload_jsonb(session: AsyncSession):
    """Payload is stored and retrieved as JSONB with correct nested values."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    payload = [
        {
            "class_key": "car",
            "geometry": {"type": "bbox", "coords": [0.5, 0.3, 0.1, 0.1]},
            "confidence": 0.92,
            "track_id": None,
        }
    ]
    rev = await _make_revision(session, project.id, sample.id, ontology.id, payload=payload)
    await session.flush()

    result = await session.execute(
        select(AnnotationRevision).where(AnnotationRevision.id == rev.id)
    )
    rev = result.scalar_one()

    assert rev.payload[0]["class_key"] == "car"


async def test_annotation_revision_provenance_jsonb(session: AsyncSession):
    """Provenance is stored and retrieved as JSONB with correct field values."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    provenance = {"source": "model", "review_status": "unreviewed"}
    rev = await _make_revision(session, project.id, sample.id, ontology.id, provenance=provenance)
    await session.flush()

    result = await session.execute(
        select(AnnotationRevision).where(AnnotationRevision.id == rev.id)
    )
    rev = result.scalar_one()

    assert rev.provenance["source"] == "model"


async def test_annotation_revision_incremental_revision_no(session: AsyncSession):
    """Two revisions with revision_no=1 and revision_no=2 for the same sample both succeed."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev1 = await _make_revision(session, project.id, sample.id, ontology.id, revision_no=1)
    await session.flush()

    rev2 = await _make_revision(session, project.id, sample.id, ontology.id, revision_no=2)
    await session.flush()

    assert rev1.revision_no == 1
    assert rev2.revision_no == 2
    assert rev1.id != rev2.id


async def test_annotation_revision_parent_revision_self_fk(session: AsyncSession):
    """A revision can reference an earlier revision via the self-referential FK."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev1 = await _make_revision(session, project.id, sample.id, ontology.id, revision_no=1)
    await session.flush()

    rev2 = await _make_revision(
        session,
        project.id,
        sample.id,
        ontology.id,
        revision_no=2,
        parent_revision_id=rev1.id,
    )
    await session.flush()

    assert rev1.parent_revision_id is None
    assert rev2.parent_revision_id == rev1.id


async def test_annotation_revision_invalid_parent_fk(session: AsyncSession):
    """A non-existent parent_revision_id raises IntegrityError."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    _rev = await _make_revision(
        session,
        project.id,
        sample.id,
        ontology.id,
        parent_revision_id=uuid.uuid4(),
    )

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


async def test_annotation_revision_sample_fk(session: AsyncSession):
    """A non-existent sample_id raises IntegrityError."""
    project = await make_project(session)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev = AnnotationRevision(
        project_id=project.id,
        sample_id=uuid.uuid4(),
        ontology_id=ontology.id,
        ontology_version=1,
        revision_no=1,
        payload=_base_payload(),
        provenance=_base_provenance(),
    )
    session.add(rev)

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


async def test_annotation_revision_created_at_auto(session: AsyncSession):
    """created_at is populated by the server default when not supplied explicitly."""
    project = await make_project(session)
    sample = await make_sample(session, project_id=project.id)
    ontology = await make_ontology(session, org_id=project.org_id)

    rev = await _make_revision(session, project.id, sample.id, ontology.id)
    await session.flush()

    result = await session.execute(
        select(AnnotationRevision).where(AnnotationRevision.id == rev.id)
    )
    rev = result.scalar_one()

    assert rev.created_at is not None
