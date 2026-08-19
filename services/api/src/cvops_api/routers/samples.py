from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.audit import emit_event
from cvops_api.core.pagination import decode_cursor_uuid
from cvops_api.core.sample_view import build_sample_outs
from cvops_api.core.storage import get_storage, public_s3_endpoint
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import Sample
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.collections import Collection, CollectionSample
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.tags import SampleTag, Tag
from cvops_api.schemas.samples import (
    SampleOut,
    SampleUpdate,
    SampleBulkAction,
    BulkResult,
    AnnotationRevisionOut,
    AnnotationCreate,
    CursorPage,
)

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


async def _load_sample(id: uuid.UUID, session: AsyncSession) -> Sample:
    r = await session.execute(select(Sample).where(Sample.id == id, Sample.deleted_at.is_(None)))
    s = r.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return s


async def _validate_tags_in_project(
    tag_ids: list[uuid.UUID], project_id: uuid.UUID, session: AsyncSession
) -> None:
    if not tag_ids:
        return
    r = await session.execute(
        select(Tag.id).where(
            Tag.id.in_(tag_ids),
            Tag.project_id == project_id,
            Tag.deleted_at.is_(None),
        )
    )
    found = set(r.scalars().all())
    missing = [t for t in tag_ids if t not in found]
    if missing:
        raise HTTPException(status_code=422, detail=f"tags not in project: {missing}")


@router.get(
    "/projects/{project_id}/samples",
    response_model=CursorPage[SampleOut],
)
async def list_samples(
    project_id: uuid.UUID,
    cursor: str | None = Query(None),
    source_id: uuid.UUID | None = Query(None),
    review_status: str | None = Query(None),
    has_annotations: bool | None = Query(None),
    annotation_class: str | None = Query(None),
    collection_id: uuid.UUID | None = Query(None),
    tag_id: uuid.UUID | None = Query(None),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[SampleOut]:
    await _check_project(project_id, current_user, session)

    q = select(Sample).where(
        Sample.project_id == project_id,
        Sample.deleted_at.is_(None),
    )

    if source_id is not None:
        q = q.where(Sample.source_id == source_id)
    if review_status is not None:
        q = q.where(Sample.review_status == review_status)
    if created_after is not None:
        q = q.where(Sample.created_at >= created_after)
    if created_before is not None:
        q = q.where(Sample.created_at < created_before)
    if collection_id is not None:
        q = q.where(
            exists().where(
                CollectionSample.sample_id == Sample.id,
                CollectionSample.collection_id == collection_id,
            )
        )
    if tag_id is not None:
        q = q.where(exists().where(SampleTag.sample_id == Sample.id, SampleTag.tag_id == tag_id))
    if has_annotations is not None:
        sub = exists().where(AnnotationRevision.sample_id == Sample.id)
        q = q.where(sub if has_annotations else ~sub)
    if annotation_class is not None:
        # The class must appear in the LATEST revision's payload array. Correlated
        # EXISTS keeps this a single WHERE on the samples row (keyset stays intact).
        q = q.where(
            text(
                """EXISTS (
                    SELECT 1 FROM annotation_revisions ar
                    WHERE ar.sample_id = samples.id
                      AND ar.revision_no = (
                          SELECT MAX(ar2.revision_no) FROM annotation_revisions ar2
                          WHERE ar2.sample_id = samples.id)
                      AND EXISTS (
                          SELECT 1 FROM jsonb_array_elements(ar.payload) e
                          WHERE e->>'class_key' = :annotation_class))"""
            ).bindparams(annotation_class=annotation_class)
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


@router.get("/samples/{id}", response_model=SampleOut)
async def get_sample(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SampleOut:
    s = await _load_sample(id, session)
    await _check_project(s.project_id, current_user, session)
    return (await build_sample_outs(session, [s]))[0]


@router.patch("/samples/{id}", response_model=SampleOut)
async def update_sample(
    id: uuid.UUID,
    body: SampleUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SampleOut:
    s = await _load_sample(id, session)
    await _check_project(s.project_id, current_user, session)

    if body.metadata is not None:
        if body.metadata_mode == "merge":
            s.metadata_ = {**(s.metadata_ or {}), **body.metadata}
        else:
            s.metadata_ = body.metadata

    if body.tag_ids is not None:
        await _validate_tags_in_project(body.tag_ids, s.project_id, session)
        await session.execute(delete(SampleTag).where(SampleTag.sample_id == s.id))
        if body.tag_ids:
            await session.execute(
                pg_insert(SampleTag)
                .values([{"sample_id": s.id, "tag_id": tid} for tid in set(body.tag_ids)])
                .on_conflict_do_nothing()
            )

    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="sample",
        entity_id=s.id,
        action="sample.updated",
        org_id=current_user.org_id,
    )
    await session.commit()
    await session.refresh(s)
    return (await build_sample_outs(session, [s]))[0]


@router.delete("/samples/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sample(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    s = await _load_sample(id, session)
    await _check_project(s.project_id, current_user, session)
    s.deleted_at = datetime.now(UTC)
    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="sample",
        entity_id=s.id,
        action="sample.deleted",
        org_id=current_user.org_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/samples/bulk", response_model=BulkResult)
async def bulk_sample_action(
    project_id: uuid.UUID,
    body: SampleBulkAction,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BulkResult:
    await _check_project(project_id, current_user, session)

    r = await session.execute(
        select(Sample.id).where(
            Sample.id.in_(body.sample_ids),
            Sample.project_id == project_id,
            Sample.deleted_at.is_(None),
        )
    )
    valid = set(r.scalars().all())
    skipped = [sid for sid in body.sample_ids if sid not in valid]

    affected = 0
    if valid:
        if body.action == "delete":
            res = cast(
                CursorResult[Any],
                await session.execute(
                    update(Sample).where(Sample.id.in_(valid)).values(deleted_at=datetime.now(UTC))
                ),
            )
            affected = res.rowcount
        elif body.action == "set_review_status":
            res = cast(
                CursorResult[Any],
                await session.execute(
                    update(Sample)
                    .where(Sample.id.in_(valid))
                    .values(review_status=body.review_status)
                ),
            )
            affected = res.rowcount
        elif body.action == "add_tags":
            assert body.tag_ids is not None
            await _validate_tags_in_project(body.tag_ids, project_id, session)
            rows = [{"sample_id": sid, "tag_id": tid} for sid in valid for tid in set(body.tag_ids)]
            res = cast(
                CursorResult[Any],
                await session.execute(pg_insert(SampleTag).values(rows).on_conflict_do_nothing()),
            )
            affected = res.rowcount
        elif body.action == "remove_tags":
            assert body.tag_ids is not None
            res = cast(
                CursorResult[Any],
                await session.execute(
                    delete(SampleTag).where(
                        SampleTag.sample_id.in_(valid),
                        SampleTag.tag_id.in_(body.tag_ids),
                    )
                ),
            )
            affected = res.rowcount
        elif body.action == "add_to_collection":
            assert body.collection_id is not None
            coll = await session.execute(
                select(Collection.id).where(
                    Collection.id == body.collection_id,
                    Collection.project_id == project_id,
                    Collection.deleted_at.is_(None),
                )
            )
            if coll.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Collection not found")
            crows = [{"collection_id": body.collection_id, "sample_id": sid} for sid in valid]
            res = cast(
                CursorResult[Any],
                await session.execute(
                    pg_insert(CollectionSample).values(crows).on_conflict_do_nothing()
                ),
            )
            affected = res.rowcount

        await emit_event(
            session,
            actor_id=str(current_user.id),
            actor_type="user",
            entity_type="sample",
            entity_id=project_id,
            action=f"sample.bulk.{body.action}",
            payload={"count": len(valid)},
            org_id=current_user.org_id,
        )
        await session.commit()

    return BulkResult(matched=len(valid), affected=affected, skipped_ids=skipped)


@router.get("/samples/{id}/image-url")
async def get_image_url(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = await _load_sample(id, session)
    await _check_project(s.project_id, current_user, session)
    url = await get_storage().get_presigned_get(
        s.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"url": url}


@router.get("/samples/{id}/thumbnail-url")
async def get_thumbnail_url(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = await _load_sample(id, session)
    await _check_project(s.project_id, current_user, session)
    if s.thumbnail_hash is None:
        raise HTTPException(status_code=404, detail="No thumbnail")
    url = await get_storage().get_presigned_get(
        s.thumbnail_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"url": url}


@router.get("/samples/{id}/annotations", response_model=list[AnnotationRevisionOut])
async def list_annotations(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AnnotationRevisionOut]:
    sample = await _load_sample(id, session)
    await _check_project(sample.project_id, current_user, session)

    r2 = await session.execute(
        select(AnnotationRevision)
        .where(AnnotationRevision.sample_id == sample.id)
        .order_by(AnnotationRevision.revision_no)
    )
    return [AnnotationRevisionOut.model_validate(rev) for rev in r2.scalars().all()]


@router.post(
    "/samples/{id}/annotations",
    response_model=AnnotationRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_annotation(
    id: uuid.UUID,
    body: AnnotationCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AnnotationRevisionOut:
    sample = await _load_sample(id, session)
    await _check_project(sample.project_id, current_user, session)

    r_max = await session.execute(
        select(func.max(AnnotationRevision.revision_no)).where(
            AnnotationRevision.sample_id == sample.id
        )
    )
    max_no: int = r_max.scalar() or 0

    r_ont = await session.execute(select(Ontology.version).where(Ontology.id == body.ontology_id))
    ontology_version = r_ont.scalar_one_or_none()
    if ontology_version is None:
        raise HTTPException(status_code=404, detail="Ontology not found")

    rev = AnnotationRevision(
        sample_id=sample.id,
        project_id=sample.project_id,
        ontology_id=body.ontology_id,
        ontology_version=ontology_version,
        revision_no=max_no + 1,
        payload=body.payload,
        provenance=body.provenance,
    )
    session.add(rev)
    await session.commit()
    return AnnotationRevisionOut.model_validate(rev)
