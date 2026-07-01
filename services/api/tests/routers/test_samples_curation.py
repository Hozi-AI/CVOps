"""Router tests for the samples curation surface: filters + soft-delete,
bulk actions (with tenant scoping), PATCH (metadata + tags), collections
membership, tag apply, and the from-samples commit endpoint.

Pattern mirrors test_projects/test_data_sources: a minimal app mounting the
samples/collections/tags/datasets routers with get_session/get_current_user
overridden onto the testcontainers Postgres.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.routers import samples, collections, tags, datasets


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    for r in (samples.router, collections.router, tags.router, datasets.router):
        app.include_router(r)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _hash() -> str:
    return "sha256:" + uuid.uuid4().hex.ljust(64, "0")


async def _seed(factory, *, n_samples: int = 0):
    """Create org/user/project/source and n samples. Returns (user, project, source, [sample_ids])."""
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
        source = DataSource(project_id=project.id, type="video")
        s.add(source)
        await s.flush()
        sample_ids: list[uuid.UUID] = []
        for i in range(n_samples):
            h = _hash()
            s.add(
                Blob(
                    hash=h,
                    storage_backend="s3",
                    storage_key=f"k/{h}",
                    size_bytes=1,
                    media_type="image/jpeg",
                )
            )
            await s.flush()
            smp = Sample(
                project_id=project.id,
                blob_hash=h,
                source_id=source.id,
                width=100,
                height=100,
                frame_index=i,
            )
            s.add(smp)
            await s.flush()
            sample_ids.append(smp.id)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(source)
        return user, project, source, sample_ids


async def test_list_filters_source_and_soft_delete(factory) -> None:
    user, project, source, ids = await _seed(factory, n_samples=3)
    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/samples")
        assert res.status_code == 200
        assert len(res.json()["items"]) == 3

        # Soft-delete one
        d = await c.delete(f"/samples/{ids[0]}")
        assert d.status_code == 204

        res2 = await c.get(f"/projects/{project.id}/samples")
        listed = {item["id"] for item in res2.json()["items"]}
        assert str(ids[0]) not in listed
        assert len(listed) == 2

        # Deleted sample is no longer retrievable
        g = await c.get(f"/samples/{ids[0]}")
        assert g.status_code == 404


async def test_bulk_set_review_status_skips_cross_tenant(factory) -> None:
    user_a, project_a, _, ids_a = await _seed(factory, n_samples=2)
    _, _, _, ids_b = await _seed(factory, n_samples=1)  # different org/project

    async with _client(factory, user_a) as c:
        res = await c.post(
            f"/projects/{project_a.id}/samples/bulk",
            json={
                "action": "set_review_status",
                "sample_ids": [str(i) for i in ids_a] + [str(ids_b[0])],
                "review_status": "accepted",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["affected"] == 2
        assert body["skipped_ids"] == [str(ids_b[0])]

        # Filter by review_status now returns the two accepted samples
        f = await c.get(f"/projects/{project_a.id}/samples?review_status=accepted")
        assert len(f.json()["items"]) == 2


async def test_patch_metadata_merge_replace_and_tags(factory) -> None:
    user, project, _, ids = await _seed(factory, n_samples=1)
    sid = ids[0]
    async with _client(factory, user) as c:
        tag = await c.post(f"/projects/{project.id}/tags", json={"name": "blurry", "color": "#f00"})
        assert tag.status_code == 201
        tag_id = tag.json()["id"]

        r1 = await c.patch(f"/samples/{sid}", json={"metadata": {"a": 1}})
        assert r1.json()["metadata"] == {"a": 1}

        r2 = await c.patch(f"/samples/{sid}", json={"metadata": {"b": 2}, "metadata_mode": "merge"})
        assert r2.json()["metadata"] == {"a": 1, "b": 2}

        r3 = await c.patch(
            f"/samples/{sid}", json={"metadata": {"c": 3}, "metadata_mode": "replace"}
        )
        assert r3.json()["metadata"] == {"c": 3}

        r4 = await c.patch(f"/samples/{sid}", json={"tag_ids": [tag_id]})
        assert [t["id"] for t in r4.json()["tags"]] == [tag_id]

        # Filter by tag
        ft = await c.get(f"/projects/{project.id}/samples?tag_id={tag_id}")
        assert len(ft.json()["items"]) == 1


async def test_collection_membership_and_count(factory) -> None:
    user, project, _, ids = await _seed(factory, n_samples=3)
    async with _client(factory, user) as c:
        coll = await c.post(f"/projects/{project.id}/collections", json={"name": "review-set"})
        assert coll.status_code == 201
        cid = coll.json()["id"]

        add = await c.post(
            f"/collections/{cid}/samples", json={"sample_ids": [str(i) for i in ids[:2]]}
        )
        assert add.status_code == 200
        assert add.json()["affected"] == 2

        got = await c.get(f"/collections/{cid}")
        assert got.json()["sample_count"] == 2

        members = await c.get(f"/collections/{cid}/samples")
        assert len(members.json()["items"]) == 2

        # Samples list filtered by collection
        fc = await c.get(f"/projects/{project.id}/samples?collection_id={cid}")
        assert len(fc.json()["items"]) == 2

        rm = await c.request(
            "DELETE", f"/collections/{cid}/samples", json={"sample_ids": [str(ids[0])]}
        )
        assert rm.status_code == 200
        got2 = await c.get(f"/collections/{cid}")
        assert got2.json()["sample_count"] == 1


async def test_from_samples_commit_includes_unannotated(factory) -> None:
    user, project, source, ids = await _seed(factory, n_samples=2)

    # Annotate exactly one sample + create an ontology + dataset.
    async with factory() as s:
        ont = Ontology(org_id=project.org_id, name="default", version=1)
        s.add(ont)
        await s.flush()
        s.add(
            AnnotationRevision(
                project_id=project.id,
                sample_id=ids[0],
                ontology_id=ont.id,
                ontology_version=1,
                revision_no=1,
                payload={"annotations": [{"class_key": "person"}]},
                provenance={},
            )
        )
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        ds = await c.post(f"/projects/{project.id}/datasets", json={"name": "ds1"})
        assert ds.status_code == 201
        ds_id = ds.json()["id"]

        res = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "message": "init",
                "sample_ids": [str(i) for i in ids],
                "ontology_id": str(ont_id),
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        # All selected samples are committed: the annotated one pins its winning
        # revision, the raw (unannotated) one commits with a null revision — a
        # dataset of raw images is valid, so nothing is skipped.
        assert body["committed_count"] == 2
        assert body["skipped_count"] == 0


async def test_list_annotations_returns_list_payload(factory) -> None:
    """GET /samples/{id}/annotations serializes the list payload (regression: the
    schema typed payload as a dict, so any real list payload 500'd)."""
    user, project, _source, ids = await _seed(factory, n_samples=1)
    async with factory() as s:
        ont = Ontology(org_id=project.org_id, name="default", version=1)
        s.add(ont)
        await s.flush()
        s.add(
            AnnotationRevision(
                project_id=project.id,
                sample_id=ids[0],
                ontology_id=ont.id,
                ontology_version=1,
                revision_no=1,
                payload=[
                    {
                        "class_key": "plane",
                        "geometry": {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.2]},
                    }
                ],
                provenance={"source": "human", "review_status": "accepted"},
            )
        )
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get(f"/samples/{ids[0]}/annotations")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert isinstance(body[0]["payload"], list)
    assert body[0]["payload"][0]["class_key"] == "plane"


async def test_malformed_cursor_returns_400(factory) -> None:
    """A garbage `?cursor=` is a client error (400), not a server crash (500).

    The samples list uses the base64-UUID cursor shape; a non-base64 value must
    be rejected cleanly by the shared cursor decoder rather than raising and
    surfacing as a 500.
    """
    user, project, _source, _ids = await _seed(factory, n_samples=1)

    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/samples", params={"cursor": "!!notbase64!!"})

    assert res.status_code == 400
    assert res.json()["detail"] == "Invalid pagination cursor"
