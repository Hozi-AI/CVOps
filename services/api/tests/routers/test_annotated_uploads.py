"""Router tests for the YOLO annotated-upload confirm flow.

`POST /projects/{id}/annotated-uploads/confirm` lands pre-labeled images as
`Sample` rows each carrying one `AnnotationRevision` (the inverse of the
`export_yolo` step). Same harness as test_data_sources.py: a minimal app
mounting only the data_sources router over the testcontainers Postgres.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.ontologies import LabelClass, Ontology
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import Sample
from cvops_api.routers import data_sources


def _unique_hash() -> str:
    return "sha256:" + uuid.uuid4().hex.ljust(64, "0")


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(data_sources.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(
    factory,
    *,
    class_keys: tuple[str, ...] = ("vehicle.car", "vehicle.truck"),
    ontology_version: int = 3,
    set_default: bool = True,
) -> tuple[User, Project, Ontology]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(project)
        await s.flush()
        ont = Ontology(org_id=project.org_id, name=f"ont-{suffix}", version=ontology_version)
        s.add(ont)
        await s.flush()
        for i, key in enumerate(class_keys):
            s.add(LabelClass(ontology_id=ont.id, class_key=key, display_name=key, sort_order=i))
        if set_default:
            project.default_ontology_id = ont.id
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(ont)
        return user, project, ont


def _url(project: Project) -> str:
    return f"/projects/{project.id}/annotated-uploads/confirm"


async def test_import_creates_sample_and_revision(factory) -> None:
    user, project, ont = await _seed(factory)
    blob = _unique_hash()
    payload = {
        "class_names": ["vehicle.car", "vehicle.truck"],
        "items": [
            {
                "blob_hash": blob,
                "width": 640,
                "height": 480,
                "boxes": [
                    {"class_id": 1, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.3, "confidence": 0.9}
                ],
            }
        ],
    }
    async with _client(factory, user) as c:
        res = await c.post(_url(project), json=payload)

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["created"] == 1
    assert body["annotated"] == 1
    sid = uuid.UUID(body["sample_ids"][0])

    async with factory() as s:
        rev = (
            await s.execute(select(AnnotationRevision).where(AnnotationRevision.sample_id == sid))
        ).scalar_one()
        # revision 1 is the latest, so it's what `from-samples` would pin.
        assert rev.revision_no == 1
        assert rev.ontology_version == 3
        assert rev.ontology_id == ont.id
        assert rev.payload == [
            {
                "class_key": "vehicle.truck",
                "geometry": {"coords": [0.5, 0.5, 0.2, 0.3]},
                "confidence": 0.9,
            }
        ]
        assert rev.provenance["source"] == "import:yolo"


async def test_unknown_class_is_rejected_and_persists_nothing(factory) -> None:
    user, project, ont = await _seed(factory)
    blob = _unique_hash()
    payload = {
        # "airplane" is not a class_key in the ontology.
        "class_names": ["vehicle.car", "airplane"],
        "items": [
            {
                "blob_hash": blob,
                "width": 10,
                "height": 10,
                "boxes": [{"class_id": 1, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}],
            }
        ],
    }
    async with _client(factory, user) as c:
        res = await c.post(_url(project), json=payload)

    assert res.status_code == 422
    assert "airplane" in res.text
    async with factory() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(Sample).where(Sample.blob_hash == blob)
            )
        ).scalar_one()
        assert n == 0


async def test_reupload_does_not_stack_a_second_revision(factory) -> None:
    user, project, ont = await _seed(factory)
    blob = _unique_hash()
    payload = {
        "class_names": ["vehicle.car"],
        "items": [
            {
                "blob_hash": blob,
                "width": 10,
                "height": 10,
                "boxes": [{"class_id": 0, "cx": 0.1, "cy": 0.1, "w": 0.1, "h": 0.1}],
            }
        ],
    }
    async with _client(factory, user) as c:
        r1 = await c.post(_url(project), json=payload)
        assert r1.status_code == 201
        r2 = await c.post(_url(project), json=payload)
        assert r2.status_code == 201
        assert r2.json()["annotated"] == 0

    sid = uuid.UUID(r1.json()["sample_ids"][0])
    async with factory() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(AnnotationRevision)
                .where(AnnotationRevision.sample_id == sid)
            )
        ).scalar_one()
        assert count == 1


async def test_out_of_range_coordinate_is_rejected(factory) -> None:
    user, project, ont = await _seed(factory)
    payload = {
        "class_names": ["vehicle.car"],
        "items": [
            {
                "blob_hash": _unique_hash(),
                "width": 10,
                "height": 10,
                "boxes": [{"class_id": 0, "cx": 1.5, "cy": 0.1, "w": 0.1, "h": 0.1}],
            }
        ],
    }
    async with _client(factory, user) as c:
        res = await c.post(_url(project), json=payload)
    assert res.status_code == 422


async def test_missing_ontology_is_rejected(factory) -> None:
    # No default ontology on the project and no ontology_id in the request.
    user, project, ont = await _seed(factory, set_default=False)
    payload = {
        "class_names": ["vehicle.car"],
        "items": [{"blob_hash": _unique_hash(), "width": 10, "height": 10, "boxes": []}],
    }
    async with _client(factory, user) as c:
        res = await c.post(_url(project), json=payload)
    assert res.status_code == 422
