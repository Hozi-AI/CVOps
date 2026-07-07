from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.audit import emit_event
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import StorageBackend, get_storage, public_s3_endpoint
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.db.models.workflows import Workflow
from cvops_api.engine.coordinator import advance_workflow
from cvops_api.engine.dispatch import create_workflow_run
from cvops_api.core.annotation_import import import_annotated_images
from cvops_api.schemas.data_sources import (
    AnnotatedImageConfirmRequest,
    AnnotatedImageConfirmResponse,
    ConfirmResponse,
    DataSourceCheck,
    DataSourceCheckResponse,
    DataSourceCreate,
    DataSourceConfirm,
    DataSourceMatch,
    DataSourceOut,
    ImageConfirmRequest,
    ImageConfirmResponse,
    ImagePresignOut,
    ImagePresignRequest,
    ImagePresignResponse,
    UploadResponse,
)

# Per-project shared data source that holds manually uploaded images.
UPLOADS_SOURCE_TYPE = "image_folder"

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


@router.get("/projects/{project_id}/data-sources", response_model=list[DataSourceOut])
async def list_data_sources(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DataSourceOut]:
    await _check_project(project_id, current_user, session)
    r = await session.execute(
        select(DataSource)
        # Show videos and the Uploads folder; legacy per-image sources are hidden
        # (their images live under the shared Uploads folder now). Soft-deleted
        # sources are excluded.
        .where(
            DataSource.project_id == project_id,
            DataSource.type != "image",
            DataSource.deleted_at == None,  # noqa: E711
        )
        .order_by(DataSource.created_at.desc())
    )
    sources = list(r.scalars().all())

    # One grouped count instead of a per-source query, so the UI can show how
    # many frames each source has produced (and detect "still processing").
    counts_r = await session.execute(
        select(Sample.source_id, func.count())
        .where(Sample.project_id == project_id)
        .group_by(Sample.source_id)
    )
    counts = {row[0]: row[1] for row in counts_r.all()}

    # Latest workflow run per source (DISTINCT ON the source_id stashed in
    # input_refs), so each card can link to its ingest run. One query for the
    # whole page, same as the counts above.
    source_id_expr = Run.input_refs["params"]["source_id"].astext
    runs_r = await session.execute(
        select(source_id_expr, Run.id)
        .where(Run.project_id == project_id, Run.kind == "workflow")
        .order_by(source_id_expr, Run.created_at.desc())
        .distinct(source_id_expr)
    )
    latest_run = {row[0]: row[1] for row in runs_r.all()}

    out: list[DataSourceOut] = []
    for ds in sources:
        item = DataSourceOut.model_validate(ds)
        item.sample_count = counts.get(ds.id, 0)
        item.latest_run_id = latest_run.get(str(ds.id))
        out.append(item)
    return out


@router.post(
    "/projects/{project_id}/data-sources",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_source(
    project_id: uuid.UUID,
    body: DataSourceCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    await _check_project(project_id, current_user, session)

    ds = DataSource(
        project_id=project_id,
        type=body.type,
        external_uri=body.external_uri,
        metadata_=body.metadata,
        status="pending",
    )
    session.add(ds)
    await session.flush()  # get ds.id

    put_url: str | None = None
    if body.type != "external_uri":
        # Sign against the host the browser used so the direct upload is reachable.
        endpoint = public_s3_endpoint(request.url.hostname)
        put_url = await get_storage().get_presigned_put_for_upload(str(ds.id), endpoint=endpoint)

    await session.commit()
    return UploadResponse(
        data_source=DataSourceOut.model_validate(ds),
        presigned_put_url=put_url,
    )


@router.post(
    "/projects/{project_id}/data-sources/check",
    response_model=DataSourceCheckResponse,
)
async def check_data_source(
    project_id: uuid.UUID,
    body: DataSourceCheck,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DataSourceCheckResponse:
    """Pre-upload dedup probe: has this exact content already been ingested
    anywhere in the user's org? Lets the client skip pushing a duplicate video
    over the wire and offer "add without re-uploading" instead.

    Scoped to the org via the Project join, so a copy in another org is
    invisible — no cross-tenant leak.
    """
    await _check_project(project_id, current_user, session)
    r = await session.execute(
        select(DataSource, Project.name)
        .join(Project, Project.id == DataSource.project_id)
        .where(
            Project.org_id == current_user.org_id,
            Project.deleted_at == None,  # noqa: E711
            DataSource.blob_hash == body.blob_hash,
            DataSource.deleted_at == None,  # noqa: E711
        )
    )
    matches = [
        DataSourceMatch(
            data_source_id=ds.id,
            project_id=ds.project_id,
            project_name=project_name,
            type=ds.type,
        )
        for ds, project_name in r.all()
    ]
    return DataSourceCheckResponse(
        exists=bool(matches),
        in_current_project=any(m.project_id == project_id for m in matches),
        matches=matches,
    )


@router.post("/data-sources/{id}/confirm-upload", response_model=ConfirmResponse)
async def confirm_upload(
    id: uuid.UUID,
    body: DataSourceConfirm,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConfirmResponse:
    r = await session.execute(select(DataSource).where(DataSource.id == id))
    ds = r.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="DataSource not found")
    proj = await _check_project(ds.project_id, current_user, session)

    # Idempotent: a re-sent confirm must not register the blob twice or dispatch
    # a duplicate run.
    if ds.status != "pending":
        return ConfirmResponse(data_source=DataSourceOut.model_validate(ds))

    # If the content is already registered (e.g. an "add without re-uploading"
    # flow, where the client uploaded no bytes because a copy exists elsewhere
    # in the org), reuse the existing blob — nothing was staged to promote.
    # Otherwise promote the finished upload to its content-addressed location
    # (server-side copy — no bytes through the API) and register the blob row.
    existing_blob = await session.get(Blob, body.blob_hash)
    if existing_blob is None:
        size_bytes, media_type, storage_key = await get_storage().promote_upload(
            str(ds.id), body.blob_hash
        )
        await session.execute(
            pg_insert(Blob)
            .values(
                hash=body.blob_hash,
                storage_backend=settings.S3_BACKEND,
                storage_key=storage_key,
                size_bytes=size_bytes,
                media_type=media_type,
            )
            .on_conflict_do_nothing(index_elements=["hash"])
        )

    ds.blob_hash = body.blob_hash
    ds.status = "uploaded"
    try:
        await session.commit()
    except IntegrityError:
        # uq_data_sources_project_blob: this exact content is already a live
        # data source in this project. Backstop behind the UI's pre-upload
        # check (covers races / direct API use).
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This video is already a data source in this project",
        )

    # Backend-owned trigger: pick the workflow to run on this upload. A
    # client-supplied workflow_id takes precedence (chosen at upload time);
    # otherwise fall back to the project's configured default. None of either
    # means "just store, no processing".
    target_wf_id = body.workflow_id or proj.default_ingest_workflow_id
    run_id: uuid.UUID | None = None
    # Only videos go through the workflow (frame extraction). Manual images are
    # ingested directly via the image-upload endpoints, never here.
    if target_wf_id is not None and ds.type == "video":
        wf_r = await session.execute(
            select(Workflow).where(
                Workflow.id == target_wf_id,
                Workflow.project_id == ds.project_id,
                Workflow.deleted_at == None,  # noqa: E711
            )
        )
        wf = wf_r.scalar_one_or_none()
        # An explicit, client-chosen workflow that doesn't resolve is a bad
        # request; a stale project default is silently skipped (don't fail the
        # upload over a misconfigured setting).
        if wf is None and body.workflow_id is not None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if wf is not None:
            run = await create_workflow_run(session, wf, {"source_id": str(ds.id)}, current_user.id)
            run_id = run.id
            # Synchronous, fast: creates the first child step runs and rings
            # their Redis queues. A worker picks them up out-of-process.
            await advance_workflow(session, run.id, current_user.id)

    return ConfirmResponse(data_source=DataSourceOut.model_validate(ds), run_id=run_id)


# ── Direct image upload → samples (no workflow) ──────────────────────────────


async def _get_or_create_uploads_source(project_id: uuid.UUID, session: AsyncSession) -> DataSource:
    """The per-project shared 'Uploads' folder that holds manual images.

    Query-then-insert (data_sources has no unique on (project_id, type)); a rare
    duplicate from a concurrent first upload is harmless since samples carry
    their own upload-group metadata.
    """
    r = await session.execute(
        select(DataSource)
        .where(
            DataSource.project_id == project_id,
            DataSource.type == UPLOADS_SOURCE_TYPE,
        )
        .limit(1)
    )
    src = r.scalar_one_or_none()
    if src is not None:
        return src
    src = DataSource(
        project_id=project_id,
        type=UPLOADS_SOURCE_TYPE,
        status="ready",
        metadata_={"name": "Uploads"},
    )
    session.add(src)
    await session.flush()
    return src


@router.post(
    "/projects/{project_id}/image-uploads/presign",
    response_model=ImagePresignResponse,
)
async def presign_images(
    project_id: uuid.UUID,
    body: ImagePresignRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ImagePresignResponse:
    await _check_project(project_id, current_user, session)
    endpoint = public_s3_endpoint(request.url.hostname)
    storage = get_storage()
    items = [
        ImagePresignOut(
            filename=it.filename,
            blob_hash=it.sha256,
            put_url=await storage.get_presigned_put(it.sha256, endpoint=endpoint),
        )
        for it in body.items
    ]
    return ImagePresignResponse(items=items)


@router.post(
    "/projects/{project_id}/image-uploads/confirm",
    response_model=ImageConfirmResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_images(
    project_id: uuid.UUID,
    body: ImageConfirmRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ImageConfirmResponse:
    await _check_project(project_id, current_user, session)
    src = await _get_or_create_uploads_source(project_id, session)

    now_iso = datetime.now(UTC).isoformat()
    group = body.group or f"Upload {now_iso}"
    meta = {"group": group, "uploaded_at": now_iso}

    sample_ids: list[uuid.UUID] = []
    for it in body.items:
        await session.execute(
            pg_insert(Blob)
            .values(
                hash=it.blob_hash,
                storage_backend=settings.S3_BACKEND,
                storage_key=StorageBackend._bucket_key(it.blob_hash),
                size_bytes=it.size_bytes or 0,
                media_type=it.content_type or "image/jpeg",
            )
            .on_conflict_do_nothing(index_elements=["hash"])
        )
        sid = uuid.uuid4()
        res = await session.execute(
            pg_insert(Sample)
            .values(
                id=sid,
                project_id=project_id,
                blob_hash=it.blob_hash,
                source_id=src.id,
                width=it.width,
                height=it.height,
                frame_index=None,
                thumbnail_hash=it.blob_hash,
                metadata_=meta,
            )
            .on_conflict_do_nothing(index_elements=["project_id", "blob_hash"])
            .returning(Sample.id)
        )
        row = res.first()
        if row is not None:
            sample_ids.append(row[0])
        else:
            existing = (
                await session.execute(
                    select(Sample.id).where(
                        Sample.project_id == project_id,
                        Sample.blob_hash == it.blob_hash,
                    )
                )
            ).first()
            if existing is not None:
                sample_ids.append(existing[0])

    if sample_ids:
        await emit_event(
            session,
            actor_id=str(current_user.id),
            actor_type="user",
            entity_type="data_source",
            entity_id=src.id,
            action="images.uploaded",
            payload={"count": len(sample_ids), "group": group},
            org_id=current_user.org_id,
        )
    await session.commit()

    return ImageConfirmResponse(source_id=src.id, created=len(sample_ids), sample_ids=sample_ids)


@router.post(
    "/projects/{project_id}/annotated-uploads/confirm",
    response_model=AnnotatedImageConfirmResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_annotated_images(
    project_id: uuid.UUID,
    body: AnnotatedImageConfirmRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AnnotatedImageConfirmResponse:
    """Land pre-labeled YOLO images as samples + annotation revisions.

    Image bytes are uploaded via the format-agnostic `/image-uploads/presign`
    path first; this confirm step records each image as a Sample and writes one
    AnnotationRevision per new sample (the box payload mirrors what export_yolo
    reads). The committed samples are then ready for the existing
    `from-samples` commit flow, which auto-resolves the latest revision."""
    project = await _check_project(project_id, current_user, session)
    src = await _get_or_create_uploads_source(project_id, session)
    return await import_annotated_images(
        session,
        project=project,
        source=src,
        body=body,
        actor_id=current_user.id,
    )


@router.get("/data-sources/{id}", response_model=DataSourceOut)
async def get_data_source(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DataSourceOut:
    r = await session.execute(
        select(DataSource).where(
            DataSource.id == id,
            DataSource.deleted_at == None,  # noqa: E711
        )
    )
    ds = r.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="DataSource not found")
    await _check_project(ds.project_id, current_user, session)
    return DataSourceOut.model_validate(ds)


@router.get("/data-sources/{id}/url")
async def get_data_source_url(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Presigned GET URL for the source's raw blob so the browser can preview
    the original video/image directly from object storage (no bytes via API)."""
    r = await session.execute(
        select(DataSource).where(
            DataSource.id == id,
            DataSource.deleted_at == None,  # noqa: E711
        )
    )
    ds = r.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="DataSource not found")
    await _check_project(ds.project_id, current_user, session)
    if ds.blob_hash is None:
        raise HTTPException(status_code=404, detail="Data source has no uploaded blob")
    url = await get_storage().get_presigned_get(
        ds.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"url": url}


@router.delete("/data-sources/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_data_source(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(
        select(DataSource).where(
            DataSource.id == id,
            DataSource.deleted_at == None,  # noqa: E711
        )
    )
    ds = r.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="DataSource not found")
    await _check_project(ds.project_id, current_user, session)
    # Soft-delete: keep the row (and its extracted samples / ingest history)
    # but drop it from listings. The partial unique index
    # uq_data_sources_project_blob is scoped to `deleted_at IS NULL`, so this
    # frees the (project, blob) slot for re-ingest and the confirm-upload 409
    # path correctly ignores the dead source.
    ds.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
