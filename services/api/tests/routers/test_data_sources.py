"""Router tests for the data-source upload-confirmation flow.

These exercise `POST /data-sources/{id}/confirm-upload`, the backend-owned
trigger point: it registers the uploaded blob, flips the data source to
`uploaded`, and — when the project names a `default_ingest_workflow_id` — auto
-dispatches a workflow run.

Pattern (first router test besides /internal): a minimal FastAPI app mounting
only the data_sources router, with `get_session`/`get_current_user` overridden
onto the testcontainers Postgres and `get_storage` faked. Dispatch now goes
through `advance_workflow`, which creates a pending child step run and XADDs a
thin doorbell message — asserted against the `fake_redis` client.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from unittest.mock import patch

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.db.models.runs import Run
from cvops_api.db.models.workflows import Workflow
from cvops_api.routers import data_sources

STREAM = "preprocessing"

# Single-step ingest workflow referencing the registered test.echo step (the
# `echo_step` fixture). Keeps the router test free of ffmpeg/S3.
_INGEST_DEF = {
    "steps": [
        {
            "id": "s1",
            "type": "test.echo",
            "config": {},
            "inputs": {"src": "$run.params.source_id"},
        }
    ],
    "edges": [],
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Stand-in for S3Backend. promote_upload returns deterministic metadata
    and the canonical content-addressed key, without touching any store."""

    async def promote_upload(self, upload_id: str, blob_hash: str):
        hex_part = blob_hash.removeprefix("sha256:")
        key = f"blobs/{hex_part[:2]}/{hex_part[2:]}"
        return 4096, "video/mp4", key

    async def get_presigned_get(
        self, blob_hash: str, ttl_seconds: int = 900, endpoint: str | None = None
    ) -> str:
        return f"https://signed.example/{blob_hash}"


_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


def _unique_hash() -> str:
    """Distinct content hash per call — the testcontainers DB is shared across
    the session, so fixed hashes collide on the blobs primary key."""
    return "sha256:" + uuid.uuid4().hex.ljust(64, "0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
def patched_storage():
    """Fake the storage backend so promote_upload never touches a real store."""
    with patch.object(data_sources, "get_storage", lambda: _FakeStorage()):
        yield


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
    with_workflow: bool,
) -> tuple[User, Project, DataSource]:
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
        if with_workflow:
            wf = Workflow(
                project_id=project.id,
                name=f"ingest-{suffix}",
                definition=_INGEST_DEF,
            )
            s.add(wf)
            await s.flush()
            project.default_ingest_workflow_id = wf.id
        ds = DataSource(project_id=project.id, type="video", status="pending")
        s.add(ds)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(ds)
        return user, project, ds


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_confirm_without_default_workflow_registers_blob(factory) -> None:
    user, project, ds = await _seed(factory, with_workflow=False)

    async with _client(factory, user) as c:
        res = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})

    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] is None
    assert body["data_source"]["status"] == "uploaded"
    assert body["data_source"]["blob_hash"] == _HASH_A

    async with factory() as s:
        blob = await s.get(Blob, _HASH_A)
        assert blob is not None
        assert blob.size_bytes == 4096
        assert blob.media_type == "video/mp4"
        # No workflow → no run dispatched (scope to this project; the
        # testcontainers DB is shared across the session).
        runs = (await s.execute(select(Run).where(Run.project_id == project.id))).scalars().all()
        assert runs == []


async def test_confirm_with_default_workflow_dispatches_run(factory, fake_redis, echo_step) -> None:
    user, _project, ds = await _seed(factory, with_workflow=True)

    async with _client(factory, user) as c:
        res = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})

    assert res.status_code == 200
    run_id = res.json()["run_id"]
    assert run_id is not None

    async with factory() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        assert run.status == "pending"
        assert run.kind == "workflow"
        assert run.input_refs == {"params": {"source_id": str(ds.id)}}

        # advance_workflow created exactly one pending child step run.
        children = (await s.execute(select(Run).where(Run.parent_run_id == run.id))).scalars().all()
        assert len(children) == 1
        child = children[0]
        assert child.kind == "step"
        assert child.step_type == "test.echo"
        assert child.status == "pending"

    # Exactly one thin doorbell message, pointing at the child step run.
    assert await fake_redis.xlen(STREAM) == 1
    _msg_id, fields = (await fake_redis.xrange(STREAM))[0]
    assert fields == {
        "job_id": str(child.id),
        "step_type": "test.echo",
        "queue": STREAM,
    }


async def test_confirm_with_explicit_workflow_id_dispatches(factory, fake_redis, echo_step) -> None:
    # Project has no default ingest workflow; the client chooses one at upload
    # time by passing workflow_id.
    user, project, ds = await _seed(factory, with_workflow=False)
    async with factory() as s:
        wf = Workflow(
            project_id=project.id,
            name=f"chosen-{uuid.uuid4().hex[:8]}",
            definition=_INGEST_DEF,
        )
        s.add(wf)
        await s.commit()
        wf_id = wf.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/data-sources/{ds.id}/confirm-upload",
            json={"blob_hash": _HASH_A, "workflow_id": str(wf_id)},
        )

    assert res.status_code == 200
    run_id = res.json()["run_id"]
    assert run_id is not None

    async with factory() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        assert run is not None
        assert run.input_refs == {"params": {"source_id": str(ds.id)}}
    assert await fake_redis.xlen(STREAM) == 1


async def test_confirm_with_unknown_workflow_id_returns_404(factory) -> None:
    user, _project, ds = await _seed(factory, with_workflow=False)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/data-sources/{ds.id}/confirm-upload",
            json={"blob_hash": _HASH_A, "workflow_id": str(uuid.uuid4())},
        )
    assert res.status_code == 404


async def test_confirm_workflow_id_from_other_project_returns_404(factory) -> None:
    # A workflow that exists but belongs to a different project must not be
    # dispatchable here.
    user, _project, ds = await _seed(factory, with_workflow=False)
    _other_user, other_project, _ds2 = await _seed(factory, with_workflow=False)
    async with factory() as s:
        foreign = Workflow(
            project_id=other_project.id,
            name=f"foreign-{uuid.uuid4().hex[:8]}",
            definition=_INGEST_DEF,
        )
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/data-sources/{ds.id}/confirm-upload",
            json={"blob_hash": _HASH_A, "workflow_id": str(foreign_id)},
        )
    assert res.status_code == 404


async def test_confirm_is_idempotent(factory, fake_redis, echo_step) -> None:
    user, project, ds = await _seed(factory, with_workflow=True)

    async with _client(factory, user) as c:
        first = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})
        second = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})

    assert first.status_code == second.status_code == 200
    assert first.json()["run_id"] is not None
    assert second.json()["run_id"] is None  # no re-dispatch

    # Scope to this project — the testcontainers DB is shared across the session.
    async with factory() as s:
        parents = (
            (
                await s.execute(
                    select(Run).where(Run.project_id == project.id, Run.kind == "workflow")
                )
            )
            .scalars()
            .all()
        )
        assert len(parents) == 1  # only one workflow run despite two confirms

    # And only one step was enqueued.
    assert await fake_redis.xlen(STREAM) == 1


async def test_list_includes_latest_run_id(factory, fake_redis, echo_step) -> None:
    # After a default-workflow upload, the list endpoint exposes the dispatched
    # run so the UI can link to it.
    user, project, ds = await _seed(factory, with_workflow=True)

    async with _client(factory, user) as c:
        confirm = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})
        run_id = confirm.json()["run_id"]
        assert run_id is not None

        res = await c.get(f"/projects/{project.id}/data-sources")

    assert res.status_code == 200
    mine = [d for d in res.json() if d["id"] == str(ds.id)]
    assert len(mine) == 1
    assert mine[0]["latest_run_id"] == run_id


async def test_list_latest_run_id_none_without_run(factory) -> None:
    user, project, ds = await _seed(factory, with_workflow=False)
    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/data-sources")
    mine = [d for d in res.json() if d["id"] == str(ds.id)]
    assert mine[0]["latest_run_id"] is None


async def test_confirm_cross_org_returns_404(factory) -> None:
    _owner, _project, ds = await _seed(factory, with_workflow=False)
    # A user from a different org must not be able to confirm this data source.
    other, _p2, _ds2 = await _seed(factory, with_workflow=False)

    async with _client(factory, other) as c:
        res = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": _HASH_A})

    assert res.status_code == 404


async def test_list_data_sources_includes_sample_count(factory) -> None:
    user, project, ds = await _seed(factory, with_workflow=False)

    # Two extracted frames for this source (distinct blobs — samples are unique
    # per (project, blob_hash)).
    hashes = [_unique_hash(), _unique_hash()]
    async with factory() as s:
        for h in hashes:
            s.add(
                Blob(
                    hash=h,
                    storage_backend="s3",
                    storage_key=f"blobs/{h}",
                    size_bytes=10,
                    media_type="image/png",
                )
            )
        await s.flush()
        for i, h in enumerate(hashes):
            s.add(
                Sample(
                    project_id=project.id,
                    source_id=ds.id,
                    blob_hash=h,
                    width=640,
                    height=480,
                    frame_index=i,
                )
            )
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/data-sources")

    assert res.status_code == 200
    mine = [d for d in res.json() if d["id"] == str(ds.id)]
    assert len(mine) == 1
    assert mine[0]["sample_count"] == 2


async def test_data_source_url_returns_presigned(factory) -> None:
    user, _project, ds = await _seed(factory, with_workflow=False)
    blob_hash = _unique_hash()
    async with factory() as s:
        s.add(
            Blob(
                hash=blob_hash,
                storage_backend="s3",
                storage_key=f"blobs/{blob_hash}",
                size_bytes=10,
                media_type="video/mp4",
            )
        )
        await s.flush()
        d = await s.get(DataSource, ds.id)
        assert d is not None
        d.blob_hash = blob_hash
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get(f"/data-sources/{ds.id}/url")

    assert res.status_code == 200
    assert res.json()["url"].endswith(blob_hash)


async def test_data_source_url_404_without_blob(factory) -> None:
    user, _project, ds = await _seed(factory, with_workflow=False)
    async with _client(factory, user) as c:
        res = await c.get(f"/data-sources/{ds.id}/url")
    assert res.status_code == 404


async def test_confirm_duplicate_in_same_project_returns_409(factory) -> None:
    user, project, ds1 = await _seed(factory, with_workflow=False)
    # Second data source in the same project, same content hash.
    async with factory() as s:
        ds2 = DataSource(project_id=project.id, type="video", status="pending")
        s.add(ds2)
        await s.commit()
        ds2_id = ds2.id

    h = _unique_hash()
    async with _client(factory, user) as c:
        r1 = await c.post(f"/data-sources/{ds1.id}/confirm-upload", json={"blob_hash": h})
        # Same content, same project → blocked by uq_data_sources_project_blob.
        r2 = await c.post(f"/data-sources/{ds2_id}/confirm-upload", json={"blob_hash": h})

    assert r1.status_code == 200
    assert r2.status_code == 409


async def test_confirm_same_blob_different_projects_both_succeed(factory) -> None:
    # The same content in two projects of the same org: both confirm, and the
    # blob is stored once (blob-level dedup still works across projects).
    user, p1, ds1 = await _seed(factory, with_workflow=False)
    async with factory() as s:
        org_id = (await s.get(Project, p1.id)).org_id
        p2 = Project(org_id=org_id, name=f"proj2-{uuid.uuid4().hex[:8]}")
        s.add(p2)
        await s.flush()
        ds2 = DataSource(project_id=p2.id, type="video", status="pending")
        s.add(ds2)
        await s.commit()
        ds2_id = ds2.id

    h = _unique_hash()
    async with _client(factory, user) as c:
        r1 = await c.post(f"/data-sources/{ds1.id}/confirm-upload", json={"blob_hash": h})
        r2 = await c.post(f"/data-sources/{ds2_id}/confirm-upload", json={"blob_hash": h})

    assert r1.status_code == r2.status_code == 200

    async with factory() as s:
        blobs = (await s.execute(select(Blob).where(Blob.hash == h))).scalars().all()
        assert len(blobs) == 1  # one Blob row despite two sources


async def test_confirm_reuses_preregistered_blob_without_upload(factory) -> None:
    # "Add without re-uploading": the blob is already registered (a copy exists
    # elsewhere in the org), so confirm must NOT promote a staged upload.
    user, _project, ds = await _seed(factory, with_workflow=False)
    h = _unique_hash()
    async with factory() as s:
        s.add(
            Blob(
                hash=h,
                storage_backend="s3",
                storage_key=f"blobs/{h}",
                size_bytes=4096,
                media_type="video/mp4",
            )
        )
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": h})

    assert res.status_code == 200
    assert res.json()["data_source"]["status"] == "uploaded"

    async with factory() as s:
        blobs = (await s.execute(select(Blob).where(Blob.hash == h))).scalars().all()
        assert len(blobs) == 1  # unchanged — no second blob


async def test_check_no_match(factory) -> None:
    user, project, _ds = await _seed(factory, with_workflow=False)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/data-sources/check",
            json={"blob_hash": _unique_hash()},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is False
    assert body["in_current_project"] is False
    assert body["matches"] == []


async def test_check_match_in_current_project(factory) -> None:
    user, project, ds = await _seed(factory, with_workflow=False)
    h = _unique_hash()
    async with _client(factory, user) as c:
        await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": h})
        res = await c.post(f"/projects/{project.id}/data-sources/check", json={"blob_hash": h})
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is True
    assert body["in_current_project"] is True
    assert len(body["matches"]) == 1
    assert body["matches"][0]["project_id"] == str(project.id)


async def test_check_match_in_other_project_same_org(factory) -> None:
    user, project, ds = await _seed(factory, with_workflow=False)
    h = _unique_hash()
    async with factory() as s:
        org_id = (await s.get(Project, project.id)).org_id
        p2 = Project(org_id=org_id, name=f"proj2-{uuid.uuid4().hex[:8]}")
        s.add(p2)
        await s.flush()
        p2_id = p2.id
        ds2 = DataSource(project_id=p2_id, type="video", status="pending")
        s.add(ds2)
        await s.commit()
        ds2_id = ds2.id

    async with _client(factory, user) as c:
        await c.post(f"/data-sources/{ds2_id}/confirm-upload", json={"blob_hash": h})
        # Probe from the *first* project — the match is in a sibling project.
        res = await c.post(f"/projects/{project.id}/data-sources/check", json={"blob_hash": h})
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is True
    assert body["in_current_project"] is False
    assert len(body["matches"]) == 1
    assert body["matches"][0]["project_id"] == str(p2_id)


async def test_check_cross_org_not_visible(factory) -> None:
    user, project, _ds = await _seed(factory, with_workflow=False)
    # Another org confirms the same content; it must be invisible here.
    other, _p2, other_ds = await _seed(factory, with_workflow=False)
    h = _unique_hash()
    async with _client(factory, other) as c:
        await c.post(f"/data-sources/{other_ds.id}/confirm-upload", json={"blob_hash": h})

    async with _client(factory, user) as c:
        res = await c.post(f"/projects/{project.id}/data-sources/check", json={"blob_hash": h})
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is False
    assert body["matches"] == []


async def test_delete_is_soft_and_frees_reingest(factory) -> None:
    # A confirmed source DELETEd is kept (row + deleted_at) but vanishes from
    # listings and GET-by-id, and the same blob can be re-confirmed into the
    # project because the partial unique index ignores soft-deleted rows.
    user, project, ds = await _seed(factory, with_workflow=False)
    blob = _unique_hash()

    async with _client(factory, user) as c:
        confirm = await c.post(f"/data-sources/{ds.id}/confirm-upload", json={"blob_hash": blob})
        assert confirm.status_code == 200

        deleted = await c.delete(f"/data-sources/{ds.id}")
        assert deleted.status_code == 204

        # No longer fetchable by id.
        got = await c.get(f"/data-sources/{ds.id}")
        assert got.status_code == 404

        # No longer listed.
        listed = await c.get(f"/projects/{project.id}/data-sources")
        assert listed.status_code == 200
        assert all(d["id"] != str(ds.id) for d in listed.json())

    # The row still exists with deleted_at set (soft, not hard).
    async with factory() as s:
        row = await s.get(DataSource, ds.id)
        assert row is not None
        assert row.deleted_at is not None

    # The freed (project, blob) slot accepts a re-ingest of the same content.
    async with factory() as s:
        ds2 = DataSource(project_id=project.id, type="video", status="pending")
        s.add(ds2)
        await s.commit()
        await s.refresh(ds2)
        ds2_id = ds2.id

    async with _client(factory, user) as c:
        reconfirm = await c.post(f"/data-sources/{ds2_id}/confirm-upload", json={"blob_hash": blob})
    assert reconfirm.status_code == 200
    assert reconfirm.json()["data_source"]["status"] == "uploaded"
