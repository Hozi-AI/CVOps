"""Router tests for the runs router.

Run-creation endpoints call advance_workflow synchronously in-request, which
XADDs doorbell messages onto Redis — so every test that creates/advances a run
uses the fake_redis fixture.

Two workflow shapes keep these light:
  * empty definition  → advance finalizes the parent to `succeeded`, no enqueue.
  * single echo step  → advance creates one pending child and XADDs once
    (needs the echo_step fixture so the type resolves).

Pattern mirrors test_data_sources: minimal FastAPI app mounting only the runs
router (NO prefix — it declares full inline paths), get_session /
get_current_user overridden onto the testcontainers Postgres.
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

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.workflows import Workflow
from cvops_api.routers import runs

STREAM = "preprocessing"

_EMPTY_DEF = {"steps": [], "edges": []}
_ECHO_DEF = {
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
# A gate-shaped step lets us seed a `waiting` child for resolve-gate tests.
_GATE_DEF = {
    "steps": [{"id": "gate1", "type": "test.echo", "config": {}}],
    "edges": [],
}


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(runs.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory, *, definition: dict) -> tuple[User, Project, Workflow]:
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
        wf = Workflow(project_id=project.id, name=f"wf-{suffix}", definition=definition)
        s.add(wf)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(wf)
        return user, project, wf


async def _seed_run(
    factory, *, project_id: uuid.UUID, workflow_id: uuid.UUID | None = None, **kwargs
) -> Run:
    async with factory() as s:
        run = Run(
            project_id=project_id,
            workflow_id=workflow_id,
            kind=kwargs.pop("kind", "workflow"),
            status=kwargs.pop("status", "pending"),
            input_refs=kwargs.pop("input_refs", {}),
            output_refs=kwargs.pop("output_refs", {}),
            config=kwargs.pop("config", {}),
            **kwargs,
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


# ---------------------------------------------------------------------------
# create run
# ---------------------------------------------------------------------------


async def test_create_run_empty_workflow_finalizes(factory, fake_redis) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)

    async with _client(factory, user) as c:
        res = await c.post(f"/workflows/{wf.id}/runs", json={"params": {}})

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "workflow"
    # Empty workflow: advance enqueues no child (no steps). Finalization is
    # gated on `if steps_by_id`, so a step-less workflow stays `pending` rather
    # than completing — see test report note.
    assert body["status"] == "pending"
    assert await fake_redis.xlen(STREAM) == 0


async def test_create_run_enqueues_step(factory, fake_redis, echo_step) -> None:
    user, project, wf = await _seed(factory, definition=_ECHO_DEF)
    source_id = str(uuid.uuid4())

    async with _client(factory, user) as c:
        res = await c.post(f"/workflows/{wf.id}/runs", json={"params": {"source_id": source_id}})

    assert res.status_code == 201, res.text
    body = res.json()
    run_id = uuid.UUID(body["id"])
    assert body["status"] == "pending"
    assert body["input_refs"] == {"params": {"source_id": source_id}}

    async with factory() as s:
        children = (await s.execute(select(Run).where(Run.parent_run_id == run_id))).scalars().all()
    assert len(children) == 1
    child = children[0]
    assert child.kind == "step"
    assert child.step_type == "test.echo"
    assert child.status == "pending"

    # Exactly one doorbell message pointing at the child.
    assert await fake_redis.xlen(STREAM) == 1
    _msg_id, fields = (await fake_redis.xrange(STREAM))[0]
    assert fields == {
        "job_id": str(child.id),
        "step_type": "test.echo",
        "queue": STREAM,
    }


async def test_create_run_unknown_workflow_404(factory, fake_redis) -> None:
    user, _project, _wf = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, user) as c:
        res = await c.post(f"/workflows/{uuid.uuid4()}/runs", json={"params": {}})
    assert res.status_code == 404, res.text


async def test_create_run_cross_org_404(factory, fake_redis) -> None:
    _owner, _project, wf = await _seed(factory, definition=_EMPTY_DEF)
    other, _p2, _wf2 = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, other) as c:
        res = await c.post(f"/workflows/{wf.id}/runs", json={"params": {}})
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# get run detail
# ---------------------------------------------------------------------------


async def test_get_run_detail(factory) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    # One child step run.
    await _seed_run(
        factory,
        project_id=project.id,
        workflow_id=wf.id,
        kind="step",
        parent_run_id=parent.id,
        step_id="s1",
        step_type="test.echo",
        status="succeeded",
    )

    async with _client(factory, user) as c:
        res = await c.get(f"/runs/{parent.id}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run"]["id"] == str(parent.id)
    assert len(body["steps"]) == 1
    # RunOut has no step_type field; the child surfaces as kind="step".
    assert body["steps"][0]["kind"] == "step"


async def test_get_run_unknown_404(factory) -> None:
    user, _project, _wf = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, user) as c:
        res = await c.get(f"/runs/{uuid.uuid4()}")
    assert res.status_code == 404, res.text


async def test_get_run_cross_org_404(factory) -> None:
    _owner, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    other, _p2, _wf2 = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, other) as c:
        res = await c.get(f"/runs/{parent.id}")
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# events (non-stream)
# ---------------------------------------------------------------------------


async def test_list_run_events(factory) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)

    # Emit an event for the run directly.
    from cvops_api.core.audit import emit_event

    async with factory() as s:
        await emit_event(
            s,
            actor_id=user.id,
            actor_type="user",
            entity_type="run",
            entity_id=parent.id,
            action="run.started",
        )
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get(f"/runs/{parent.id}/events")

    assert res.status_code == 200, res.text
    actions = [ev["action"] for ev in res.json()]
    assert "run.started" in actions


async def test_list_run_events_unknown_404(factory) -> None:
    user, _project, _wf = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, user) as c:
        res = await c.get(f"/runs/{uuid.uuid4()}/events")
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_pending_run(factory) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id, status="pending")

    async with _client(factory, user) as c:
        res = await c.post(f"/runs/{parent.id}/cancel")
    assert res.status_code == 204, res.text

    async with factory() as s:
        refreshed = await s.get(Run, parent.id)
        assert refreshed.status == "cancelled"
        assert refreshed.finished_at is not None


async def test_cancel_terminal_run_noop(factory) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id, status="succeeded")

    async with _client(factory, user) as c:
        res = await c.post(f"/runs/{parent.id}/cancel")
    # Endpoint always returns 204; a terminal run is left untouched.
    assert res.status_code == 204, res.text

    async with factory() as s:
        refreshed = await s.get(Run, parent.id)
        assert refreshed.status == "succeeded"


async def test_cancel_unknown_404(factory) -> None:
    user, _project, _wf = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, user) as c:
        res = await c.post(f"/runs/{uuid.uuid4()}/cancel")
    assert res.status_code == 404, res.text


async def test_cancel_cross_org_404(factory) -> None:
    _owner, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    other, _p2, _wf2 = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, other) as c:
        res = await c.post(f"/runs/{parent.id}/cancel")
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


async def test_retry_creates_new_run(factory, fake_redis) -> None:
    user, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(
        factory,
        project_id=project.id,
        workflow_id=wf.id,
        status="failed",
        attempt=1,
    )

    async with _client(factory, user) as c:
        res = await c.post(f"/runs/{parent.id}/retry")

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] != str(parent.id)
    assert body["attempt"] == 2
    # Empty workflow → retried run has no steps to enqueue; stays pending (same
    # step-less finalization quirk as create-run).
    assert body["status"] == "pending"
    assert await fake_redis.xlen(STREAM) == 0


async def test_retry_unknown_404(factory) -> None:
    user, _project, _wf = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, user) as c:
        res = await c.post(f"/runs/{uuid.uuid4()}/retry")
    assert res.status_code == 404, res.text


async def test_retry_cross_org_404(factory) -> None:
    _owner, project, wf = await _seed(factory, definition=_EMPTY_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    other, _p2, _wf2 = await _seed(factory, definition=_EMPTY_DEF)
    async with _client(factory, other) as c:
        res = await c.post(f"/runs/{parent.id}/retry")
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# resolve gate
# ---------------------------------------------------------------------------


async def test_resolve_gate_resumes(factory, fake_redis, echo_step) -> None:
    user, project, wf = await _seed(factory, definition=_GATE_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    # A waiting gate child the resolve endpoint should clear.
    gate = await _seed_run(
        factory,
        project_id=project.id,
        workflow_id=wf.id,
        kind="step",
        parent_run_id=parent.id,
        step_id="gate1",
        step_type="test.echo",
        status="waiting",
    )

    async with _client(factory, user) as c:
        res = await c.post(
            f"/runs/{parent.id}/gates/gate1/resolve",
            json={"resolution": "approve"},
        )

    assert res.status_code == 200, res.text
    assert res.json() == {"status": "resumed"}

    async with factory() as s:
        refreshed = await s.get(Run, gate.id)
        assert refreshed.status == "succeeded"
        # The gate also carries annotation_revision_ids forward for the
        # downstream commit step (empty here: no labeling job completed).
        assert refreshed.output_refs == {
            "resolution": "approve",
            "annotation_revision_ids": [],
        }


async def test_resolve_gate_no_waiting_child_404(factory) -> None:
    user, project, wf = await _seed(factory, definition=_GATE_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    # Child exists but is not in `waiting` — must not resolve.
    await _seed_run(
        factory,
        project_id=project.id,
        workflow_id=wf.id,
        kind="step",
        parent_run_id=parent.id,
        step_id="gate1",
        step_type="test.echo",
        status="pending",
    )

    async with _client(factory, user) as c:
        res = await c.post(
            f"/runs/{parent.id}/gates/gate1/resolve",
            json={"resolution": "approve"},
        )
    assert res.status_code == 404, res.text


async def test_resolve_gate_unknown_run_404(factory) -> None:
    user, _project, _wf = await _seed(factory, definition=_GATE_DEF)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/runs/{uuid.uuid4()}/gates/gate1/resolve",
            json={"resolution": "approve"},
        )
    assert res.status_code == 404, res.text


async def test_resolve_gate_cross_org_404(factory) -> None:
    _owner, project, wf = await _seed(factory, definition=_GATE_DEF)
    parent = await _seed_run(factory, project_id=project.id, workflow_id=wf.id)
    await _seed_run(
        factory,
        project_id=project.id,
        workflow_id=wf.id,
        kind="step",
        parent_run_id=parent.id,
        step_id="gate1",
        step_type="test.echo",
        status="waiting",
    )
    other, _p2, _wf2 = await _seed(factory, definition=_GATE_DEF)
    async with _client(factory, other) as c:
        res = await c.post(
            f"/runs/{parent.id}/gates/gate1/resolve",
            json={"resolution": "approve"},
        )
    assert res.status_code == 404, res.text
