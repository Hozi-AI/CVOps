"""Router tests for the server-rendered frame viewer at `/dataset`.

Harness mirrors `test_data_sources.py`: a minimal FastAPI app mounting only the
viewer router, with `get_session` overridden onto the testcontainers Postgres
and `get_storage` faked to return deterministic presigned URLs. Unlike the
data-sources tests, auth is *not* overridden — the viewer's `get_viewer_user`
dependency is exercised for real (token via `?token=` or `Authorization`
header), so these tests need the `fake_redis` fixture for the blacklist check.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from unittest.mock import patch

from cvops_api.core.auth import create_access_token
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.routers import viewer


def _signed(blob_hash: str) -> str:
    """Deterministic stand-in for a presigned URL, keyed by the blob hash."""
    return f"https://signed.example/{blob_hash}"


class _FakeStorage:
    async def get_presigned_get(
        self, blob_hash: str, ttl_seconds: int = 900, endpoint: str | None = None
    ) -> str:
        return _signed(blob_hash)


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
def patched_storage():
    with patch.object(viewer, "get_storage", lambda: _FakeStorage()):
        yield


def _client(factory) -> AsyncClient:
    app = FastAPI()
    app.include_router(viewer.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_project(factory, *, samples: int, with_thumbs: bool = True):
    """Create org + user + project + one data source with `samples` frames."""
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
        ds = DataSource(project_id=project.id, type="video", status="uploaded")
        s.add(ds)
        await s.flush()
        sample_rows = []
        for i in range(samples):
            blob = f"sha256:{suffix}blob{i:056d}"
            thumb = f"sha256:{suffix}thmb{i:056d}" if with_thumbs else None
            for h in (blob, thumb):
                if h is not None:
                    s.add(
                        Blob(
                            hash=h,
                            storage_backend="s3",
                            storage_key=f"blobs/{h}",
                            size_bytes=1,
                            media_type="image/jpeg",
                        )
                    )
            await s.flush()
            row = Sample(
                project_id=project.id,
                source_id=ds.id,
                blob_hash=blob,
                thumbnail_hash=thumb,
                width=640,
                height=480,
                frame_index=i,
            )
            s.add(row)
            sample_rows.append(row)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(ds)
        for row in sample_rows:
            await s.refresh(row)
        return user, project, ds, sample_rows


def _token(user: User) -> str:
    return create_access_token(str(user.id))


async def test_renders_frames(factory, fake_redis) -> None:
    user, project, ds, samples = await _seed_project(factory, samples=3)
    tok = _token(user)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?project_id={project.id}&token={tok}")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    text = res.text
    for s in samples:
        assert _signed(s.thumbnail_hash) in text  # grid thumbnail
        assert _signed(s.blob_hash) in text  # full-image link target
    assert ds.type in text
    assert str(ds.id)[:8] in text
    assert "3 frame(s)" in text


async def test_thumbnail_fallback_to_blob(factory, fake_redis) -> None:
    user, project, _ds, samples = await _seed_project(factory, samples=1, with_thumbs=False)
    tok = _token(user)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?project_id={project.id}&token={tok}")

    assert res.status_code == 200
    # img src falls back to the blob hash URL when thumbnail_hash is null.
    assert f"<img src='{_signed(samples[0].blob_hash)}'" in res.text


async def test_missing_token_is_401(factory, fake_redis) -> None:
    _user, project, _ds, _samples = await _seed_project(factory, samples=1)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?project_id={project.id}")

    assert res.status_code == 401


async def test_token_via_authorization_header(factory, fake_redis) -> None:
    user, project, _ds, _samples = await _seed_project(factory, samples=1)
    tok = _token(user)

    async with _client(factory) as c:
        res = await c.get(
            f"/dataset?project_id={project.id}",
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert res.status_code == 200


async def test_cross_org_project_is_404(factory, fake_redis) -> None:
    _owner, project, _ds, _samples = await _seed_project(factory, samples=1)
    other, _p2, _ds2, _s2 = await _seed_project(factory, samples=1)
    tok = _token(other)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?project_id={project.id}&token={tok}")

    assert res.status_code == 404


async def test_empty_project_shows_empty_state(factory, fake_redis) -> None:
    user, project, _ds, _samples = await _seed_project(factory, samples=0)
    tok = _token(user)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?project_id={project.id}&token={tok}")

    assert res.status_code == 200
    assert "No frames extracted" in res.text
    assert "<img" not in res.text


async def test_landing_lists_org_projects(factory, fake_redis) -> None:
    user, project, _ds, _samples = await _seed_project(factory, samples=1)
    tok = _token(user)

    async with _client(factory) as c:
        res = await c.get(f"/dataset?token={tok}")

    assert res.status_code == 200
    assert project.name in res.text
    assert f"/dataset?project_id={project.id}" in res.text
