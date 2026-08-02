from __future__ import annotations

import uuid
from datetime import datetime, UTC

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.schemas.projects import ProjectCreate, ProjectUpdate, ProjectOut

# main.py mounts this with prefix="/projects"; don't repeat it here or paths
# double up to /projects/projects (matches the auth/orgs routers).
router = APIRouter()


async def _get_project(
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


@router.get("/", response_model=list[ProjectOut])
async def list_projects(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectOut]:
    r = await session.execute(
        select(Project).where(
            Project.org_id == current_user.org_id,
            Project.deleted_at == None,  # noqa: E711
        )
    )
    return [ProjectOut.model_validate(p) for p in r.scalars().all()]


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = Project(
        org_id=current_user.org_id,
        name=body.name,
        task_type=body.task_type,
        modality=body.modality,
        settings=body.settings,
    )
    session.add(project)
    await session.flush()
    await session.commit()
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    return ProjectOut.model_validate(await _get_project(project_id, current_user, session))


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    proj = await _get_project(project_id, current_user, session)
    if body.name is not None:
        proj.name = body.name
    if body.task_type is not None:
        proj.task_type = body.task_type
    if body.modality is not None:
        proj.modality = body.modality
    if body.default_ontology_id is not None:
        proj.default_ontology_id = body.default_ontology_id
    # Distinguish "omitted" from "explicit null" so the field can be cleared:
    # an explicit null in the PATCH body unsets the default ingest workflow.
    if "default_ingest_workflow_id" in body.model_fields_set:
        proj.default_ingest_workflow_id = body.default_ingest_workflow_id
    if body.settings is not None:
        proj.settings = body.settings
    await session.flush()
    await session.commit()
    return ProjectOut.model_validate(proj)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    proj = await _get_project(project_id, current_user, session)
    proj.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
