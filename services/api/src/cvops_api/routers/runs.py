from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.pagination import decode_cursor_parts
from cvops_api.core.redis_client import get_redis
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.workflows import Workflow
from cvops_api.db.models.runs import Run, Event
from cvops_api.engine.coordinator import advance_workflow
from cvops_api.engine.dispatch import create_workflow_run
from cvops_api.schemas.runs import RunCreate, RunOut, RunDetail, EventOut, GateResolve
from cvops_api.schemas.samples import CursorPage

router = APIRouter()


async def _check_project(
    project_id: uuid.UUID,
    current_user: User,
    session: AsyncSession,
) -> Project:
    r = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.org_id == current_user.org_id,
            Project.deleted_at == None,  # noqa: E711
        )
    )
    proj = r.scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


@router.get("/projects/{project_id}/runs", response_model=CursorPage[RunOut])
async def list_runs(
    project_id: uuid.UUID,
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[RunOut]:
    """List the project's parent workflow runs, newest-first.

    `Run.id` is a random UUID, so the repo's standard id-ascending cursor is not
    chronological. We use a keyset cursor on `(created_at, id)` ordered DESC;
    the cursor encodes `base64("<created_at_iso>|<id>")`.
    """
    await _check_project(project_id, current_user, session)

    q = select(Run).where(
        Run.project_id == project_id,
        Run.parent_run_id == None,  # noqa: E711
    )
    if status is not None:
        q = q.where(Run.status == status)
    if cursor is not None:
        ts_str, id_str = decode_cursor_parts(cursor)
        try:
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = uuid.UUID(id_str)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
        q = q.where(tuple_(Run.created_at, Run.id) < (cursor_ts, cursor_id))
    q = q.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)

    result = await session.execute(q)
    items = list(result.scalars().all())

    next_cursor: str | None = None
    if len(items) == limit + 1:
        last = items[limit - 1]
        next_cursor = base64.b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
        items = items[:limit]

    return CursorPage(
        items=[RunOut.model_validate(r) for r in items],
        next_cursor=next_cursor,
    )


@router.post("/workflows/{workflow_id}/runs", response_model=RunOut, status_code=201)
async def create_run(
    workflow_id: uuid.UUID,
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> RunOut:
    r = await session.execute(select(Workflow).where(Workflow.id == workflow_id))
    wf = r.scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await _check_project(wf.project_id, current_user, session)

    run = await create_workflow_run(session, wf, body.params, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    await session.refresh(run)
    return RunOut.model_validate(run)


@router.get("/runs/{id}", response_model=RunDetail)
async def get_run(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunDetail:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    r_steps = await session.execute(select(Run).where(Run.parent_run_id == run.id))
    steps = list(r_steps.scalars().all())

    return RunDetail(
        run=RunOut.model_validate(run),
        steps=[RunOut.model_validate(s) for s in steps],
    )


@router.get("/runs/{id}/events", response_model=list[EventOut])
async def list_run_events(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EventOut]:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    # Collect child run ids
    r_children = await session.execute(select(Run.id).where(Run.parent_run_id == run.id))
    child_ids = [row for row in r_children.scalars().all()]
    entity_ids = [run.id, *child_ids]

    r_events = await session.execute(
        select(Event)
        .where(
            Event.entity_type == "run",
            Event.entity_id.in_(entity_ids),
        )
        .order_by(Event.created_at)
    )
    return [EventOut.model_validate(ev) for ev in r_events.scalars().all()]


@router.get("/runs/{id}/events/stream")
async def stream_run_events(
    id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    proj = await session.get(Project, run.project_id)
    if proj is None or proj.org_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Run not found")

    async def generate() -> AsyncIterator[str]:
        from cvops_api.db.session import async_session_factory

        seen_ids: set[uuid.UUID] = set()
        terminal = {"succeeded", "failed", "cancelled"}

        async with async_session_factory() as s:
            while True:
                result = await s.execute(
                    select(Event)
                    .where(
                        Event.entity_type == "run",
                        Event.entity_id == id,
                    )
                    .order_by(Event.created_at)
                )
                for ev in result.scalars().all():
                    if ev.id not in seen_ids:
                        seen_ids.add(ev.id)
                        data = EventOut.model_validate(ev).model_dump_json()
                        yield f"data: {data}\n\n"

                run_result = await s.execute(select(Run).where(Run.id == id))
                current_run = run_result.scalar_one_or_none()
                if current_run and current_run.status in terminal:
                    yield "event: done\ndata: {}\n\n"
                    return

                await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/runs/{id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_run(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    if run.status in {"pending", "running"}:
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
        await session.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/runs/{id}/retry", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def retry_run(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    # A fresh parent run with no children: advance_workflow re-creates every
    # step child from scratch and enqueues the ready ones. Idempotency reuse on
    # _idem_key means unchanged, already-succeeded steps are copied, not re-run.
    new_run = Run(
        project_id=run.project_id,
        workflow_id=run.workflow_id,
        kind=run.kind,
        status="pending",
        attempt=run.attempt + 1,
        input_refs=run.input_refs,
        output_refs={},
        config=run.config,
    )
    session.add(new_run)
    await session.flush()
    await session.commit()

    await advance_workflow(session, new_run.id, current_user.id)
    await session.refresh(new_run)
    return RunOut.model_validate(new_run)


@router.post("/runs/{id}/gates/{step_id}/resolve")
async def resolve_gate(
    id: uuid.UUID,
    step_id: str,
    body: GateResolve,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    r_child = await session.execute(
        select(Run).where(
            Run.parent_run_id == run.id,
            Run.step_id == step_id,
            Run.status == "waiting",
        )
    )
    child = r_child.scalar_one_or_none()
    if child is None:
        raise HTTPException(status_code=404, detail="Waiting gate step not found")

    # Carry forward annotation_revision_ids written by cvat_sync (if it ran before
    # the manual approval) so the downstream commit step receives them even when
    # the user clicks Approve rather than waiting for the sync to call advance_workflow.
    lj_row = await session.execute(
        text(
            "SELECT annotation_revision_ids_out FROM labeling_jobs "
            "WHERE run_id = CAST(:rid AS uuid) AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        ),
        {"rid": str(child.id)},
    )
    lj = lj_row.first()
    annotation_revision_ids = list(lj[0]) if lj and lj[0] else []

    child.output_refs = {"resolution": body.resolution, "annotation_revision_ids": annotation_revision_ids}
    child.status = "succeeded"
    child.finished_at = datetime.now(UTC)
    await session.commit()

    # Gate cleared — enqueue whatever became ready downstream.
    await advance_workflow(session, run.id, current_user.id)
    return {"status": "resumed"}


# The cvat-queue stream worker-cvat consumes (same name as internal.py's bridge).
CVAT_STREAM = "cvat"


@router.post("/runs/{id}/gates/{step_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_gate(
    id: uuid.UUID,
    step_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Manually pull reviewed annotations from CVAT for a waiting human-review gate.

    Replaces the CVAT-version-incompatible completion webhook: emits the same
    ``cvat_sync`` doorbell the webhook bridge would, so worker-cvat pulls the
    reviewed annotations into ``annotation_revisions``, marks the labeling job
    complete, and resumes the run. Idempotent downstream (the worker short-circuits
    on an already-completed job).
    """
    r = await session.execute(select(Run).where(Run.id == id))
    run = r.scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await _check_project(run.project_id, current_user, session)

    r_child = await session.execute(
        select(Run).where(
            Run.parent_run_id == run.id,
            Run.step_id == step_id,
            Run.status == "waiting",
        )
    )
    child = r_child.scalar_one_or_none()
    if child is None:
        raise HTTPException(status_code=404, detail="Waiting gate step not found")

    lj = (
        await session.execute(
            text(
                "SELECT cvat_task_id FROM labeling_jobs WHERE run_id = CAST(:rid AS uuid) "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"rid": str(child.id)},
        )
    ).first()
    if lj is None or lj[0] is None:
        raise HTTPException(status_code=409, detail="No CVAT task recorded for this gate")

    await get_redis().xadd(CVAT_STREAM, {"kind": "cvat_sync", "cvat_task_id": str(lj[0])})
    return {"status": "syncing", "cvat_task_id": int(lj[0])}
