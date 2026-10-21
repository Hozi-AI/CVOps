"""Router tests for the datasets router.

Covers datasets (list/create/get incl. 404 + cross-org), commits (list/get/
samples/create), refs (list/create/delete), the diff endpoint, and dataset-links
(create/patch/delete). The "from-samples" commit is already covered in
test_samples_curation.py and is not duplicated here.

Same minimal-app pattern as the other router tests: get_session and
get_current_user are overridden onto the testcontainers Postgres, and the router
(which defines full inline paths) is mounted with NO prefix. The shared db
factories are imported from tests/db/conftest.py — they flush, so each call site
commits when post-commit visibility is needed.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.versioning import (
    CommitSample,
    ProjectDatasetLink,
    Ref,
)
from cvops_api.routers import datasets
from tests.db.conftest import (
    make_commit,
    make_data_source,
    make_dataset,
    make_ontology,
    make_ref,
    make_sample,
)


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(datasets.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Project]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(project)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        return user, project


# ── datasets ────────────────────────────────────────────────────────────────


async def test_list_datasets(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{project.id}/datasets")
    assert res.status_code == 200, res.text
    assert [d["id"] for d in res.json()] == [str(ds_id)]


async def test_list_datasets_project_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/projects/{uuid.uuid4()}/datasets")
    assert res.status_code == 404, res.text


async def test_create_dataset(factory) -> None:
    user, project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(f"/projects/{project.id}/datasets", json={"name": "ds1"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "ds1"
    assert body["project_id"] == str(project.id)


async def test_create_dataset_cross_org_404(factory) -> None:
    _owner, project = await _seed(factory)
    other, _p2 = await _seed(factory)
    async with _client(factory, other) as c:
        res = await c.post(f"/projects/{project.id}/datasets", json={"name": "ds1"})
    assert res.status_code == 404, res.text


async def test_get_dataset(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == str(ds_id)


async def test_get_dataset_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{uuid.uuid4()}")
    assert res.status_code == 404, res.text


async def test_get_dataset_cross_org_404(factory) -> None:
    _owner, project = await _seed(factory)
    other, _p2 = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, other) as c:
        res = await c.get(f"/datasets/{ds_id}")
    assert res.status_code == 404, res.text


# ── commits ─────────────────────────────────────────────────────────────────


async def test_list_commits(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        commit = await make_commit(
            s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id, message="c1"
        )
        await s.commit()
        ds_id, commit_id = ds.id, commit.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/commits")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["next_cursor"] is None
    assert [c["id"] for c in body["items"]] == [str(commit_id)]
    assert body["items"][0]["message"] == "c1"


async def test_list_commits_dataset_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{uuid.uuid4()}/commits")
    assert res.status_code == 404, res.text


async def test_list_commits_pagination(factory) -> None:
    user, project = await _seed(factory)
    created_ids: list[str] = []
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        for i in range(3):
            commit = await make_commit(
                s,
                project_id=project.id,
                dataset_id=ds.id,
                ontology_id=ont.id,
                message=f"c{i}",
            )
            created_ids.append(str(commit.id))
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        first = await c.get(f"/datasets/{ds_id}/commits?limit=2")
        assert first.status_code == 200, first.text
        body = first.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None
        # next_cursor is base64 of "<iso_created_at>|<uuid>" (created_at DESC, id DESC).
        decoded = base64.b64decode(body["next_cursor"]).decode()
        iso_created_at, _, cursor_id = decoded.partition("|")
        datetime.fromisoformat(iso_created_at)
        assert uuid.UUID(cursor_id)
        assert cursor_id == body["items"][-1]["id"]

        # Page 2 returns the remaining commit (the boundary row is NOT skipped —
        # next_cursor is derived from the last returned item with strict `<`).
        first_ids = [item["id"] for item in body["items"]]
        second = await c.get(f"/datasets/{ds_id}/commits?limit=2&cursor={body['next_cursor']}")
        assert second.status_code == 200, second.text
        body2 = second.json()
        assert len(body2["items"]) == 1
        assert body2["next_cursor"] is None
        # All three commits are returned across the two pages, none duplicated.
        all_ids = first_ids + [item["id"] for item in body2["items"]]
        assert set(all_ids) == set(created_ids)
        assert len(all_ids) == 3


async def test_get_commit(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        commit = await make_commit(
            s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id, message="c1"
        )
        await s.commit()
        ds_id, commit_id = ds.id, commit.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/commits/{commit_id}")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == str(commit_id)
    assert res.headers["cache-control"] == "immutable, max-age=31536000"


async def test_get_commit_404(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/commits/{uuid.uuid4()}")
    assert res.status_code == 404, res.text


async def test_get_commit_cross_org_404(factory) -> None:
    _owner, project = await _seed(factory)
    other, _p2 = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id)
        await s.commit()
        ds_id, commit_id = ds.id, commit.id

    async with _client(factory, other) as c:
        res = await c.get(f"/datasets/{ds_id}/commits/{commit_id}")
    assert res.status_code == 404, res.text


async def test_get_commit_samples(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id)
        sample = await make_sample(s, project_id=project.id)
        s.add(
            CommitSample(
                commit_id=commit.id,
                sample_id=sample.id,
                annotation_revision_id=None,
                split="train",
            )
        )
        await s.commit()
        ds_id, commit_id, sample_id = ds.id, commit.id, sample.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/commits/{commit_id}/samples")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [s["id"] for s in body["items"]] == [str(sample_id)]
    assert body["next_cursor"] is None


async def test_create_commit_creates_branch_ref(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        sample = await make_sample(s, project_id=project.id)
        rev = AnnotationRevision(
            project_id=project.id,
            sample_id=sample.id,
            ontology_id=ont.id,
            ontology_version=1,
            revision_no=1,
            payload={"annotations": []},
            provenance={},
        )
        s.add(rev)
        await s.commit()
        ds_id, ont_id, sample_id, rev_id = ds.id, ont.id, sample.id, rev.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits",
            json={
                "message": "init",
                "sample_ids": [str(sample_id)],
                "annotation_revision_ids": [str(rev_id)],
                "ontology_id": str(ont_id),
                "branch_name": "main",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["stats"]["sample_count"] == 1
    assert body["ontology_id"] == str(ont_id)
    commit_id = body["id"]

    # A branch ref "main" was created pointing at the new commit.
    async with factory() as s:
        from sqlalchemy import select

        ref = (
            await s.execute(select(Ref).where(Ref.dataset_id == ds_id, Ref.name == "main"))
        ).scalar_one()
        assert str(ref.target_commit_id) == commit_id


async def test_create_commit_ontology_404(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits",
            json={
                "message": "init",
                "sample_ids": [],
                "annotation_revision_ids": [],
                "ontology_id": str(uuid.uuid4()),
            },
        )
    assert res.status_code == 404, res.text


async def test_create_commit_dataset_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{uuid.uuid4()}/commits",
            json={
                "message": "init",
                "sample_ids": [],
                "annotation_revision_ids": [],
                "ontology_id": str(uuid.uuid4()),
            },
        )
    assert res.status_code == 404, res.text


@pytest.mark.parametrize(
    "split_strategy",
    [
        {"train_ratio": 0.8, "val_ratio": 0.5},  # sum exceeds 1.0
        {"train_ratio": -0.1},  # negative
        {"val_ratio": 1.5},  # above 1.0
        {"train_ratio": "lots"},  # non-numeric
    ],
)
async def test_create_commit_rejects_bad_split_ratios(factory, split_strategy) -> None:
    """Invalid split ratios are rejected at the schema boundary with 422.

    The check runs before the handler, so no dataset needs to exist — a bad
    `split_strategy` never reaches the commit logic that would otherwise compute
    garbage train/val counts.
    """
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{uuid.uuid4()}/commits",
            json={
                "message": "init",
                "sample_ids": [],
                "annotation_revision_ids": [],
                "ontology_id": str(uuid.uuid4()),
                "split_strategy": split_strategy,
            },
        )
    assert res.status_code == 422, res.text


# ── refs ────────────────────────────────────────────────────────────────────


async def test_list_refs(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        ref = await make_ref(s, dataset_id=ds.id, target_commit_id=commit.id)
        await s.commit()
        ds_id, ref_id = ds.id, ref.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/refs")
    assert res.status_code == 200, res.text
    assert [r["id"] for r in res.json()] == [str(ref_id)]


async def test_create_ref(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        await s.commit()
        ds_id, commit_id = ds.id, commit.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/refs",
            json={
                "ref_type": "tag",
                "name": "v1.0",
                "target_commit_id": str(commit_id),
                "is_mutable": False,
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "v1.0"
    assert body["ref_type"] == "tag"
    assert body["is_mutable"] is False
    assert body["target_commit_id"] == str(commit_id)


async def test_create_ref_dataset_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{uuid.uuid4()}/refs",
            json={"name": "v1", "target_commit_id": str(uuid.uuid4())},
        )
    assert res.status_code == 404, res.text


async def test_delete_ref(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        ref = await make_ref(s, dataset_id=ds.id, target_commit_id=commit.id)
        await s.commit()
        ds_id, ref_id = ds.id, ref.id

    async with _client(factory, user) as c:
        res = await c.delete(f"/datasets/{ds_id}/refs/{ref_id}")
    assert res.status_code == 204, res.text

    async with factory() as s:
        assert await s.get(Ref, ref_id) is None


async def test_delete_ref_404(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.delete(f"/datasets/{ds_id}/refs/{uuid.uuid4()}")
    assert res.status_code == 404, res.text


# ── diff ────────────────────────────────────────────────────────────────────


async def test_diff_commits(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        c_from = await make_commit(s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id)
        c_to = await make_commit(s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id)
        # shared sample in both, one only in from (removed), one only in to (added)
        shared = await make_sample(s, project_id=project.id)
        only_from = await make_sample(s, project_id=project.id)
        only_to = await make_sample(s, project_id=project.id)
        s.add_all(
            [
                CommitSample(commit_id=c_from.id, sample_id=shared.id, split="train"),
                CommitSample(commit_id=c_from.id, sample_id=only_from.id, split="train"),
                CommitSample(commit_id=c_to.id, sample_id=shared.id, split="train"),
                CommitSample(commit_id=c_to.id, sample_id=only_to.id, split="train"),
            ]
        )
        await s.commit()
        ds_id, from_id, to_id = ds.id, c_from.id, c_to.id
        added_id, removed_id = only_to.id, only_from.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/diff?from={from_id}&to={to_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["added"] == [str(added_id)]
    assert body["removed"] == [str(removed_id)]
    assert body["changed"] == []


async def test_diff_from_commit_not_in_dataset_404(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        c_to = await make_commit(s, project_id=project.id, dataset_id=ds.id, ontology_id=ont.id)
        await s.commit()
        ds_id, to_id = ds.id, c_to.id

    async with _client(factory, user) as c:
        res = await c.get(f"/datasets/{ds_id}/diff?from={uuid.uuid4()}&to={to_id}")
    assert res.status_code == 404, res.text


# ── dataset links ───────────────────────────────────────────────────────────


async def test_create_dataset_link_pinned(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        await s.commit()
        ds_id, commit_id = ds.id, commit.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/dataset-links",
            json={"dataset_id": str(ds_id), "pinned_commit_id": str(commit_id)},
        )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["pinned_commit_id"] == str(commit_id)
    assert body["ref_id"] is None


async def test_create_dataset_link_requires_exactly_one_target(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        # neither target → 422
        res = await c.post(
            f"/projects/{project.id}/dataset-links",
            json={"dataset_id": str(ds_id)},
        )
    assert res.status_code == 422, res.text


async def test_patch_dataset_link_switches_to_ref(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        ref = await make_ref(s, dataset_id=ds.id, target_commit_id=commit.id)
        link = ProjectDatasetLink(
            project_id=project.id, dataset_id=ds.id, pinned_commit_id=commit.id
        )
        s.add(link)
        await s.commit()
        link_id, ref_id = link.id, ref.id

    async with _client(factory, user) as c:
        res = await c.patch(f"/dataset-links/{link_id}", json={"ref_id": str(ref_id)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ref_id"] == str(ref_id)
    assert body["pinned_commit_id"] is None


async def test_patch_dataset_link_404(factory) -> None:
    user, _project = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.patch(f"/dataset-links/{uuid.uuid4()}", json={"ref_id": str(uuid.uuid4())})
    assert res.status_code == 404, res.text


async def test_delete_dataset_link(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        link = ProjectDatasetLink(
            project_id=project.id, dataset_id=ds.id, pinned_commit_id=commit.id
        )
        s.add(link)
        await s.commit()
        link_id = link.id

    async with _client(factory, user) as c:
        res = await c.delete(f"/dataset-links/{link_id}")
    assert res.status_code == 204, res.text

    async with factory() as s:
        assert await s.get(ProjectDatasetLink, link_id) is None


async def test_delete_dataset_link_cross_org_404(factory) -> None:
    _owner, project = await _seed(factory)
    other, _p2 = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        commit = await make_commit(s, project_id=project.id, dataset_id=ds.id)
        link = ProjectDatasetLink(
            project_id=project.id, dataset_id=ds.id, pinned_commit_id=commit.id
        )
        s.add(link)
        await s.commit()
        link_id = link.id

    async with _client(factory, other) as c:
        res = await c.delete(f"/dataset-links/{link_id}")
    assert res.status_code == 404, res.text


# ── split assignment: one data source never spans splits ────────────────────


async def _seed_sources(factory, project_id: uuid.UUID, n_sources: int, per_source: int):
    """Create `n_sources` data sources with `per_source` samples each.

    Returns the sample ids interleaved across sources (so a position-based split
    would cut every source across splits) and {sample_id: source_id}.
    """
    async with factory() as s:
        sources = [await make_data_source(s, project_id=project_id) for _ in range(n_sources)]
        per: list[list[uuid.UUID]] = []
        source_of: dict[uuid.UUID, uuid.UUID] = {}
        for src in sources:
            ids = []
            for frame in range(per_source):
                sample = await make_sample(
                    s, project_id=project_id, source_id=src.id, frame_index=frame
                )
                ids.append(sample.id)
                source_of[sample.id] = src.id
            per.append(ids)
        await s.commit()
    interleaved = [per[j][i] for i in range(per_source) for j in range(n_sources)]
    return interleaved, source_of


async def _splits_by_source(factory, commit_id: str, source_of) -> dict[uuid.UUID, set[str]]:
    from sqlalchemy import select

    async with factory() as s:
        rows = (
            await s.execute(
                select(CommitSample.sample_id, CommitSample.split).where(
                    CommitSample.commit_id == uuid.UUID(commit_id)
                )
            )
        ).all()
    out: dict[uuid.UUID, set[str]] = {}
    for sample_id, split in rows:
        out.setdefault(source_of[sample_id], set()).add(split)
    return out


async def test_from_samples_keeps_each_source_in_one_split(factory) -> None:
    user, project = await _seed(factory)
    sample_ids, source_of = await _seed_sources(factory, project.id, n_sources=7, per_source=10)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "sample_ids": [str(i) for i in sample_ids],
                "split_strategy": {"train_ratio": 0.5, "val_ratio": 0.25},
            },
        )
    assert res.status_code == 201, res.text

    by_source = await _splits_by_source(factory, res.json()["commit_id"], source_of)
    assert len(by_source) == 7
    assert all(len(splits) == 1 for splits in by_source.values()), by_source


async def test_from_samples_split_is_independent_of_request_order(factory) -> None:
    user, project = await _seed(factory)
    sample_ids, source_of = await _seed_sources(factory, project.id, n_sources=6, per_source=5)
    async with factory() as s:
        ds_a = await make_dataset(s, project_id=project.id)
        ds_b = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_a_id, ds_b_id = ds_a.id, ds_b.id

    strategy = {"train_ratio": 0.5, "val_ratio": 0.25}
    async with _client(factory, user) as c:
        res_a = await c.post(
            f"/datasets/{ds_a_id}/commits/from-samples",
            json={"sample_ids": [str(i) for i in sample_ids], "split_strategy": strategy},
        )
        res_b = await c.post(
            f"/datasets/{ds_b_id}/commits/from-samples",
            json={"sample_ids": [str(i) for i in reversed(sample_ids)], "split_strategy": strategy},
        )
    assert res_a.status_code == 201 and res_b.status_code == 201

    a = await _splits_by_source(factory, res_a.json()["commit_id"], source_of)
    b = await _splits_by_source(factory, res_b.json()["commit_id"], source_of)
    assert a == b


async def test_create_commit_keeps_each_source_in_one_split(factory) -> None:
    user, project = await _seed(factory)
    sample_ids, source_of = await _seed_sources(factory, project.id, n_sources=8, per_source=10)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        revs = []
        for sid in sample_ids:
            rev = AnnotationRevision(
                project_id=project.id,
                sample_id=sid,
                ontology_id=ont.id,
                ontology_version=1,
                revision_no=1,
                payload={"annotations": []},
                provenance={},
            )
            s.add(rev)
            revs.append(rev)
        await s.commit()
        ds_id, ont_id = ds.id, ont.id
        rev_ids = [r.id for r in revs]

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits",
            json={
                "message": "split",
                "sample_ids": [str(i) for i in sample_ids],
                "annotation_revision_ids": [str(i) for i in rev_ids],
                "ontology_id": str(ont_id),
                "split_strategy": {"train_ratio": 0.5, "val_ratio": 0.25},
            },
        )
    assert res.status_code == 201, res.text

    by_source = await _splits_by_source(factory, res.json()["id"], source_of)
    assert len(by_source) == 8
    assert all(len(splits) == 1 for splits in by_source.values()), by_source


async def test_create_commit_rejects_samples_outside_project(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        ont = await make_ontology(s, org_id=project.org_id)
        await s.commit()
        ds_id, ont_id = ds.id, ont.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits",
            json={
                "message": "bad",
                "sample_ids": [str(uuid.uuid4())],
                "annotation_revision_ids": [str(uuid.uuid4())],
                "ontology_id": str(ont_id),
            },
        )
    assert res.status_code == 422, res.text


async def test_from_samples_reuses_parent_split_for_known_sources(factory) -> None:
    user, project = await _seed(factory)
    first_ids, source_of = await _seed_sources(factory, project.id, n_sources=12, per_source=2)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        # A second batch of frames from the same sources.
        second_ids: list[uuid.UUID] = []
        for src in set(source_of.values()):
            sample = await make_sample(s, project_id=project.id, source_id=src, frame_index=99)
            second_ids.append(sample.id)
            source_of[sample.id] = src
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res1 = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "sample_ids": [str(i) for i in first_ids],
                "split_strategy": {"train_ratio": 0.9, "val_ratio": 0.05},
            },
        )
        # Very different ratios: re-hashing alone would move most sources.
        res2 = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "sample_ids": [str(i) for i in second_ids],
                "split_strategy": {"train_ratio": 0.1, "val_ratio": 0.1},
            },
        )
    assert res1.status_code == 201 and res2.status_code == 201, (res1.text, res2.text)

    by_source = await _splits_by_source(factory, res2.json()["commit_id"], source_of)
    assert len(by_source) == 12
    assert all(len(splits) == 1 for splits in by_source.values()), by_source


async def test_from_samples_import_batch_does_not_collapse_into_one_split(factory) -> None:
    user, project = await _seed(factory)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        # An import: ONE data source, images without a frame_index.
        src = await make_data_source(s, project_id=project.id)
        ids = [
            (await make_sample(s, project_id=project.id, source_id=src.id)).id for _ in range(60)
        ]
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "sample_ids": [str(i) for i in ids],
                "split_strategy": {"train_ratio": 0.7, "val_ratio": 0.15},
            },
        )
    assert res.status_code == 201, res.text
    assert res.json()["warnings"] == []

    from sqlalchemy import func, select

    async with factory() as s:
        counts = dict(
            (
                await s.execute(
                    select(CommitSample.split, func.count())
                    .where(CommitSample.commit_id == uuid.UUID(res.json()["commit_id"]))
                    .group_by(CommitSample.split)
                )
            ).all()
        )
    assert set(counts) == {"train", "val", "test"}


async def test_from_samples_lone_video_returns_empty_split_warnings(factory) -> None:
    user, project = await _seed(factory)
    sample_ids, _ = await _seed_sources(factory, project.id, n_sources=1, per_source=20)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{ds_id}/commits/from-samples",
            json={
                "sample_ids": [str(i) for i in sample_ids],
                "split_strategy": {"train_ratio": 0.7, "val_ratio": 0.15},
            },
        )
    assert res.status_code == 201, res.text
    assert len(res.json()["warnings"]) == 2


async def test_from_samples_later_single_video_commits_fill_val_and_test(factory) -> None:
    user, project = await _seed(factory)
    sample_ids, source_of = await _seed_sources(factory, project.id, n_sources=10, per_source=10)
    by_video: dict[uuid.UUID, list[uuid.UUID]] = {}
    for sid in sample_ids:
        by_video.setdefault(source_of[sid], []).append(sid)
    async with factory() as s:
        ds = await make_dataset(s, project_id=project.id)
        await s.commit()
        ds_id = ds.id

    commit_id = ""
    async with _client(factory, user) as c:
        for ids in by_video.values():  # one video per commit
            res = await c.post(
                f"/datasets/{ds_id}/commits/from-samples",
                json={
                    "sample_ids": [str(i) for i in ids],
                    "split_strategy": {"train_ratio": 0.7, "val_ratio": 0.15},
                },
            )
            assert res.status_code == 201, res.text
            commit_id = res.json()["commit_id"]

    by_source = await _splits_by_source(factory, commit_id, source_of)
    assert all(len(splits) == 1 for splits in by_source.values())
    assert {next(iter(v)) for v in by_source.values()} == {"train", "val", "test"}
