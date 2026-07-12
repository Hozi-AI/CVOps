from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.audit import emit_event
from cvops_api.core.pagination import decode_cursor_uuid
from cvops_api.core.sample_view import build_sample_outs
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import Sample
from cvops_api.db.models.collections import Collection, CollectionSample
from cvops_api.schemas.collections import CollectionCreate, CollectionOut, CollectionUpdate
from cvops_api.schemas.samples import BulkResult, CursorPage, SampleIdList, SampleOut

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


async def _load_collection(id: uuid.UUID, session: AsyncSession) -> Collection:
    r = await session.execute(
        select(Collection).where(Collection.id == id, Collection.deleted_at.is_(None))
    )
    c = r.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return c


@router.get("/projects/{project_id}/collections", response_model=CursorPage[CollectionOut])
async def list_collections(
    project_id: uuid.UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[CollectionOut]:
    await _check_project(project_id, current_user, session)

    q = select(Collection).where(
        Collection.project_id == project_id,
        Collection.deleted_at.is_(None),
    )
    if cursor is not None:
        cursor_uuid = decode_cursor_uuid(cursor)
        q = q.where(Collection.id > cursor_uuid)
    q = q.order_by(Collection.id).limit(limit + 1)

    r = await session.execute(q)
    items = list(r.scalars().all())

    next_cursor: str | None = None
    if len(items) == limit + 1:
        next_cursor = base64.b64encode(str(items[-1].id).encode()).decode()
        items = items[:limit]

    return CursorPage(
        items=[CollectionOut.model_validate(c) for c in items],
        next_cursor=next_cursor,
    )


@router.post(
    "/projects/{project_id}/collections",
    response_model=CollectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    project_id: uuid.UUID,
    body: CollectionCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    await _check_project(project_id, current_user, session)
    coll = Collection(project_id=project_id, name=body.name, description=body.description)
    session.add(coll)
    await session.flush()  # assign coll.id before the event references it
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="collection",
        entity_id=coll.id,
        action="collection.created",
        org_id=current_user.org_id,
    )
    await session.commit()
    await session.refresh(coll)
    return CollectionOut.model_validate(coll)


@router.get("/collections/{id}", response_model=CollectionOut)
async def get_collection(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)
    count = await session.execute(
        select(func.count())
        .select_from(CollectionSample)
        .where(CollectionSample.collection_id == id)
    )
    out = CollectionOut.model_validate(coll)
    out.sample_count = int(count.scalar_one())
    return out


@router.patch("/collections/{id}", response_model=CollectionOut)
async def update_collection(
    id: uuid.UUID,
    body: CollectionUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)
    if body.name is not None:
        coll.name = body.name
    if body.description is not None:
        coll.description = body.description
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="collection",
        entity_id=coll.id,
        action="collection.updated",
        org_id=current_user.org_id,
    )
    await session.commit()
    await session.refresh(coll)
    return CollectionOut.model_validate(coll)


@router.delete("/collections/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)
    coll.deleted_at = datetime.now(UTC)
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="collection",
        entity_id=coll.id,
        action="collection.deleted",
        org_id=current_user.org_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/collections/{id}/samples", response_model=BulkResult)
async def add_collection_samples(
    id: uuid.UUID,
    body: SampleIdList,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResult:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)

    r = await session.execute(
        select(Sample.id).where(
            Sample.id.in_(body.sample_ids),
            Sample.project_id == coll.project_id,
            Sample.deleted_at.is_(None),
        )
    )
    valid = set(r.scalars().all())
    skipped = [sid for sid in body.sample_ids if sid not in valid]

    affected = 0
    if valid:
        rows = [{"collection_id": id, "sample_id": sid} for sid in valid]
        res = cast(
            CursorResult[Any],
            await session.execute(
                pg_insert(CollectionSample).values(rows).on_conflict_do_nothing()
            ),
        )
        affected = res.rowcount
        await emit_event(
            session,
            actor_id=str(current_user.id),
            actor_type="user",
            entity_type="collection",
            entity_id=id,
            action="collection.samples_added",
            payload={"count": len(valid)},
            org_id=current_user.org_id,
        )
        await session.commit()

    return BulkResult(matched=len(valid), affected=affected, skipped_ids=skipped)


@router.delete("/collections/{id}/samples", response_model=BulkResult)
async def remove_collection_samples(
    id: uuid.UUID,
    body: SampleIdList,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResult:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)

    res = cast(
        CursorResult[Any],
        await session.execute(
            delete(CollectionSample).where(
                CollectionSample.collection_id == id,
                CollectionSample.sample_id.in_(body.sample_ids),
            )
        ),
    )
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="collection",
        entity_id=id,
        action="collection.samples_removed",
        payload={"count": res.rowcount},
        org_id=current_user.org_id,
    )
    await session.commit()
    return BulkResult(matched=len(body.sample_ids), affected=res.rowcount, skipped_ids=[])


@router.get("/collections/{id}/samples", response_model=CursorPage[SampleOut])
async def list_collection_samples(
    id: uuid.UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[SampleOut]:
    coll = await _load_collection(id, session)
    await _check_project(coll.project_id, current_user, session)

    q = (
        select(Sample)
        .join(CollectionSample, CollectionSample.sample_id == Sample.id)
        .where(CollectionSample.collection_id == id, Sample.deleted_at.is_(None))
    )
    if cursor is not None:
        cursor_uuid = decode_cursor_uuid(cursor)
        q = q.where(Sample.id > cursor_uuid)
    q = q.order_by(Sample.id).limit(limit + 1)

    r = await session.execute(q)
    items = list(r.scalars().all())

    next_cursor: str | None = None
    if len(items) == limit + 1:
        next_cursor = base64.b64encode(str(items[-1].id).encode()).decode()
        items = items[:limit]

    return CursorPage(
        items=await build_sample_outs(session, items),
        next_cursor=next_cursor,
    )
