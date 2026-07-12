from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.audit import emit_event
from cvops_api.core.sample_view import build_sample_outs
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import Sample
from cvops_api.db.models.tags import SampleTag, Tag
from cvops_api.schemas.samples import SampleOut, TagIdList
from cvops_api.schemas.tags import TagCreate, TagOut, TagUpdate

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


async def _load_tag(id: uuid.UUID, session: AsyncSession) -> Tag:
    r = await session.execute(select(Tag).where(Tag.id == id, Tag.deleted_at.is_(None)))
    tag = r.scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


async def _load_sample(id: uuid.UUID, session: AsyncSession) -> Sample:
    r = await session.execute(select(Sample).where(Sample.id == id, Sample.deleted_at.is_(None)))
    s = r.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return s


@router.get("/projects/{project_id}/tags", response_model=list[TagOut])
async def list_tags(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TagOut]:
    await _check_project(project_id, current_user, session)
    r = await session.execute(
        select(Tag).where(Tag.project_id == project_id, Tag.deleted_at.is_(None)).order_by(Tag.name)
    )
    return [TagOut.model_validate(t) for t in r.scalars().all()]


@router.post(
    "/projects/{project_id}/tags",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tag(
    project_id: uuid.UUID,
    body: TagCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    await _check_project(project_id, current_user, session)
    tag = Tag(project_id=project_id, name=body.name, color=body.color)
    session.add(tag)
    await session.flush()  # assign tag.id before the event references it
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="tag",
        entity_id=tag.id,
        action="tag.created",
        org_id=current_user.org_id,
    )
    await session.commit()
    await session.refresh(tag)
    return TagOut.model_validate(tag)


@router.patch("/tags/{id}", response_model=TagOut)
async def update_tag(
    id: uuid.UUID,
    body: TagUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await _load_tag(id, session)
    await _check_project(tag.project_id, current_user, session)
    if body.name is not None:
        tag.name = body.name
    if body.color is not None:
        tag.color = body.color
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="tag",
        entity_id=tag.id,
        action="tag.updated",
        org_id=current_user.org_id,
    )
    await session.commit()
    await session.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/tags/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tag = await _load_tag(id, session)
    await _check_project(tag.project_id, current_user, session)
    # Soft-delete the tag; sample_tags join rows stay but reads filter on deleted_at.
    tag.deleted_at = datetime.now(UTC)
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="tag",
        entity_id=tag.id,
        action="tag.deleted",
        org_id=current_user.org_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/samples/{id}/tags", response_model=SampleOut)
async def add_sample_tags(
    id: uuid.UUID,
    body: TagIdList,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SampleOut:
    sample = await _load_sample(id, session)
    await _check_project(sample.project_id, current_user, session)

    if body.tag_ids:
        r = await session.execute(
            select(Tag.id).where(
                Tag.id.in_(body.tag_ids),
                Tag.project_id == sample.project_id,
                Tag.deleted_at.is_(None),
            )
        )
        found = set(r.scalars().all())
        missing = [t for t in body.tag_ids if t not in found]
        if missing:
            raise HTTPException(status_code=422, detail=f"tags not in project: {missing}")
        await session.execute(
            pg_insert(SampleTag)
            .values([{"sample_id": sample.id, "tag_id": tid} for tid in set(body.tag_ids)])
            .on_conflict_do_nothing()
        )
        await emit_event(
            session,
            actor_id=str(current_user.id),
            actor_type="user",
            entity_type="sample",
            entity_id=sample.id,
            action="sample.tags_added",
            org_id=current_user.org_id,
        )
        await session.commit()

    return (await build_sample_outs(session, [sample]))[0]


@router.delete(
    "/samples/{id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_sample_tag(
    id: uuid.UUID,
    tag_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    sample = await _load_sample(id, session)
    await _check_project(sample.project_id, current_user, session)
    await session.execute(
        delete(SampleTag).where(SampleTag.sample_id == id, SampleTag.tag_id == tag_id)
    )
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="sample",
        entity_id=sample.id,
        action="sample.tag_removed",
        org_id=current_user.org_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
