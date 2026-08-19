"""Verify modality fields land in DB and are returned by the API."""
from __future__ import annotations
import uuid
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text
from cvops_api.core.auth import get_current_user
from cvops_api.core.registry import registry
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.routers import projects as projects_router


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(factory):
    org = Org(name=f"org-{uuid.uuid4().hex[:6]}")
    user = User(
        email=f"{uuid.uuid4().hex[:6]}@test.com",
        password_hash="x",
    )
    async with factory() as s:
        s.add(org)
        await s.flush()
        user.org_id = org.id
        s.add(user)
        await s.commit()

    app = FastAPI()
    app.include_router(projects_router.router, prefix="/projects")

    async def override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, user


@pytest.mark.asyncio
async def test_project_create_with_modality(client):
    c, user = client
    r = await c.post("/projects/", json={"name": "nlp-proj", "modality": "text"})
    assert r.status_code == 201
    body = r.json()
    assert body["modality"] == "text"


@pytest.mark.asyncio
async def test_project_default_modality_is_image(client):
    c, user = client
    r = await c.post("/projects/", json={"name": "img-proj"})
    assert r.status_code == 201
    assert r.json()["modality"] == "image"


def test_label_studio_backend_registered():
    import cvops_steps
    cvops_steps.register_all()
    from cvops_steps.labeling_backends import get_backend
    backend = get_backend("label_studio")
    assert backend.name == "label_studio"


def test_annotation_type_text_span_registered():
    import cvops_steps  # noqa: F401 — triggers register_all via import
    cvops_steps.register_all()
    reg = registry.resolve("annotation.text.span")
    assert reg.category == "annotation_type"
    schema = reg.json_schema
    assert "items" in schema
    item_props = schema["items"]["properties"]
    assert "char_start" in item_props
    assert "char_end" in item_props
    assert "class_key" in item_props


def test_annotation_type_sensor_region_registered():
    import cvops_steps  # noqa: F401
    cvops_steps.register_all()
    reg = registry.resolve("annotation.sensor.region")
    assert reg.category == "annotation_type"
    props = reg.json_schema["items"]["properties"]
    assert "time_start_ms" in props
    assert "time_end_ms" in props
    assert "class_key" in props


@pytest.mark.asyncio
async def test_sample_nullable_width_height(postgres_url: str):
    """Insert a sample row with NULL width/height — must not raise."""
    engine = create_async_engine(postgres_url, echo=False)
    async with engine.begin() as conn:
        org_id = str(uuid.uuid4())
        proj_id = str(uuid.uuid4())
        blob_hash = "abc" + uuid.uuid4().hex
        src_id = str(uuid.uuid4())
        sample_id = str(uuid.uuid4())
        await conn.execute(
            text("INSERT INTO orgs (id, name) VALUES (CAST(:id AS uuid), :name)"),
            {"id": org_id, "name": f"org-{uuid.uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, org_id, name, modality, task_type) "
                "VALUES (CAST(:id AS uuid), CAST(:org AS uuid), :name, 'text', 'classification')"
            ),
            {"id": proj_id, "org": org_id, "name": "nlp"},
        )
        await conn.execute(
            text(
                "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, media_type) "
                "VALUES (:h, 'garage', :h, 100, 'text/plain')"
            ),
            {"h": blob_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO data_sources (id, project_id, type, status) "
                "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), 'source.text', 'ready')"
            ),
            {"id": src_id, "pid": proj_id},
        )
        await conn.execute(
            text(
                "INSERT INTO samples (id, project_id, blob_hash, source_id, modality, width, height) "
                "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), :bh, CAST(:src AS uuid), 'text', NULL, NULL)"
            ),
            {"id": sample_id, "pid": proj_id, "bh": blob_hash, "src": src_id},
        )
    await engine.dispose()
