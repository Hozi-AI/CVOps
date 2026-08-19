from __future__ import annotations

import uuid
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.registry import registry
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.workflows import Workflow
from cvops_api.schemas.workflows import WorkflowCreate, WorkflowUpdate, WorkflowOut

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


@router.get("/projects/{project_id}/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[WorkflowOut]:
    await _check_project(project_id, current_user, session)
    r = await session.execute(
        select(Workflow).where(
            Workflow.project_id == project_id,
            Workflow.deleted_at == None,  # noqa: E711
        )
    )
    return [WorkflowOut.model_validate(wf) for wf in r.scalars().all()]


@router.post(
    "/projects/{project_id}/workflows",
    response_model=WorkflowOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    project_id: uuid.UUID,
    body: WorkflowCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkflowOut:
    await _check_project(project_id, current_user, session)

    # Validate step type_keys exist and configs satisfy their schema
    for step in body.definition.get("steps", []):
        try:
            registry.resolve(step["type"])
        except KeyError:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown step type: {step['type']!r}",
            )
        try:
            registry.validate_config(step["type"], step.get("config", {}))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422,
                detail=f"Step '{step.get('id', step['type'])}' config invalid: {exc}",
            )

    wf = Workflow(
        project_id=project_id,
        name=body.name,
        definition=body.definition,
        version=1,
    )
    session.add(wf)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A workflow with that name already exists in this project")
    return WorkflowOut.model_validate(wf)


@router.get("/workflows/{id}", response_model=WorkflowOut)
async def get_workflow(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkflowOut:
    r = await session.execute(select(Workflow).where(Workflow.id == id))
    wf = r.scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await _check_project(wf.project_id, current_user, session)
    return WorkflowOut.model_validate(wf)


@router.patch("/workflows/{id}", response_model=WorkflowOut)
async def update_workflow(
    id: uuid.UUID,
    body: WorkflowUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkflowOut:
    r = await session.execute(select(Workflow).where(Workflow.id == id))
    wf = r.scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await _check_project(wf.project_id, current_user, session)

    if body.name is not None:
        wf.name = body.name
    if body.definition is not None:
        # Validate new step types and configs
        for step in body.definition.get("steps", []):
            try:
                registry.resolve(step["type"])
            except KeyError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown step type: {step['type']!r}",
                )
            try:
                registry.validate_config(step["type"], step.get("config", {}))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422,
                    detail=f"Step '{step.get('id', step['type'])}' config invalid: {exc}",
                )
        wf.definition = body.definition
        wf.version += 1

    await session.commit()
    return WorkflowOut.model_validate(wf)


@router.delete("/workflows/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(select(Workflow).where(Workflow.id == id))
    wf = r.scalar_one_or_none()
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await _check_project(wf.project_id, current_user, session)

    wf.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
