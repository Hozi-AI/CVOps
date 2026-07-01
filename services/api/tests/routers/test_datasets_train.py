"""Router test for the ad-hoc "Train this commit" trigger.

`POST /datasets/{id}/commits/{commit_id}/train` bakes a git_url/entry_point/
hyperparams trainer into an ephemeral export_yolo → train run scoped to a commit
— no saved Workflow and no pre-registered TrainingContainer. We assert the
parent `Run` carries the inline DAG on `config.definition` and that
`advance_workflow` creates + enqueues the first (export) child.

Same minimal-app pattern as test_data_sources: mount only the datasets router,
override session/current_user onto testcontainers Postgres, and register the
real export_yolo/train steps so config validation + queue routing run for real
(their `run()` bodies are never invoked — advance only creates the pending row).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cvops_api.core.auth import get_current_user
from cvops_api.core.registry import registry
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.versioning import Dataset, Commit
from cvops_api.db.models.models import TrainingContainer
from cvops_api.db.models.runs import Run
from cvops_api.routers import datasets

EXPORT_STREAM = "preprocessing"


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def real_steps():
    """Register the real export_yolo + train steps for config validation and
    queue routing; restore the registry afterwards."""
    from cvops_steps.export_yolo import ExportYoloStep
    from cvops_steps.train import TrainStep

    steps = [ExportYoloStep(), TrainStep()]
    for s in steps:
        registry.register(s)
    yield
    for s in steps:
        registry._store.pop(s.type_key, None)


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(datasets.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Dataset, Commit, TrainingContainer]:
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
        ontology = Ontology(org_id=project.org_id, name=f"ont-{suffix}", version=1)
        s.add(ontology)
        dataset = Dataset(project_id=project.id, name=f"ds-{suffix}")
        s.add(dataset)
        tc = TrainingContainer(
            project_id=project.id,
            name=f"tc-{suffix}",
            image="ghcr.io/example/trainer:latest",
            icd_config={"inputs": {"epochs": {"env": "EPOCHS"}}},
        )
        s.add(tc)
        await s.flush()
        commit = Commit(
            dataset_id=dataset.id,
            project_id=project.id,
            ontology_id=ontology.id,
            ontology_version=1,
            message="seed",
            stats={"sample_count": 0},
        )
        s.add(commit)
        await s.commit()
        await s.refresh(user)
        await s.refresh(dataset)
        await s.refresh(commit)
        await s.refresh(tc)
        return user, dataset, commit, tc


async def test_train_commit_creates_adhoc_run_and_enqueues_export(
    factory, fake_redis, real_steps
) -> None:
    user, dataset, commit, _tc = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{dataset.id}/commits/{commit.id}/train",
            json={
                "git_url": "https://github.com/example/trainer.git",
                "entry_point": "train.py",
                "hyperparams": {"epochs": 5},
            },
        )

    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    async with factory() as s:
        parent = await s.get(Run, uuid.UUID(run_id))
        assert parent is not None
        assert parent.workflow_id is None
        assert parent.kind == "workflow"
        assert parent.status == "pending"
        # Inline DAG frozen onto the parent's config.
        definition = parent.config["definition"]
        step_types = {st["id"]: st["type"] for st in definition["steps"]}
        assert step_types == {"export": "step.export_yolo", "train": "step.train"}
        assert definition["edges"] == [{"from": "export", "to": "train"}]
        train_cfg = next(st for st in definition["steps"] if st["id"] == "train")["config"]
        assert train_cfg["git_url"] == "https://github.com/example/trainer.git"
        assert train_cfg["hyperparams"] == {"epochs": 5}
        # No container baked in — that's the whole point of the ad-hoc path.
        assert "training_container_id" not in train_cfg

        # advance_workflow created the export child (train waits on it).
        children = (
            (await s.execute(select(Run).where(Run.parent_run_id == parent.id)))
            .scalars()
            .all()
        )
        assert len(children) == 1
        child = children[0]
        assert child.step_type == "step.export_yolo"
        assert child.status == "pending"
        assert child.input_refs == {"commit_id": str(commit.id)}

    # One doorbell message for the export step on the preprocessing stream.
    assert await fake_redis.xlen(EXPORT_STREAM) == 1


async def test_train_commit_unknown_commit_404(factory, real_steps) -> None:
    user, dataset, _commit, _tc = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{dataset.id}/commits/{uuid.uuid4()}/train",
            json={"git_url": "https://github.com/example/trainer.git"},
        )

    assert res.status_code == 404


async def test_train_commit_threads_training_container_id(
    factory, fake_redis, real_steps
) -> None:
    user, dataset, commit, tc = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{dataset.id}/commits/{commit.id}/train",
            json={
                "git_url": "https://github.com/example/trainer.git",
                "hyperparams": {"epochs": 5},
                "training_container_id": str(tc.id),
            },
        )

    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    async with factory() as s:
        parent = await s.get(Run, uuid.UUID(run_id))
        assert parent is not None
        definition = parent.config["definition"]
        train_cfg = next(st for st in definition["steps"] if st["id"] == "train")["config"]
        assert train_cfg["training_container_id"] == str(tc.id)


async def test_train_commit_unknown_container_404(factory, real_steps) -> None:
    user, dataset, commit, _tc = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{dataset.id}/commits/{commit.id}/train",
            json={
                "git_url": "https://github.com/example/trainer.git",
                "training_container_id": str(uuid.uuid4()),
            },
        )

    assert res.status_code == 404


async def test_train_commit_cross_project_container_404(factory, real_steps) -> None:
    """A container from another project must be unreachable even within the org."""
    user, dataset, commit, _tc = await _seed(factory)
    # Seed a second project + container under the same user's org.
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        other_project = Project(org_id=user.org_id, name=f"proj2-{suffix}")
        s.add(other_project)
        await s.flush()
        other_tc = TrainingContainer(
            project_id=other_project.id,
            name=f"tc2-{suffix}",
            image="ghcr.io/example/other:latest",
            icd_config={},
        )
        s.add(other_tc)
        await s.commit()
        await s.refresh(other_tc)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/datasets/{dataset.id}/commits/{commit.id}/train",
            json={
                "git_url": "https://github.com/example/trainer.git",
                "training_container_id": str(other_tc.id),
            },
        )

    assert res.status_code == 404
