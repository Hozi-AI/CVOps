from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import StorageBackend, get_storage, public_s3_endpoint
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.models import ModelVersion
from cvops_api.db.models.projects import Project
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.schemas.models import ModelVersionCreate, ModelVersionOut, ModelVersionPatch

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


async def _get_model_version(
    mv_id: uuid.UUID,
    current_user: User,
    session: AsyncSession,
) -> ModelVersion:
    r = await session.execute(select(ModelVersion).where(ModelVersion.id == mv_id))
    mv = r.scalar_one_or_none()
    if mv is None:
        raise HTTPException(status_code=404, detail="Not found")
    proj = await session.get(Project, mv.project_id)
    if proj is None or proj.org_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Not found")
    return mv


# ── Upload slot ───────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/models/upload-url")
async def get_model_upload_url(
    project_id: uuid.UUID,
    blob_hash: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    await _get_project(project_id, current_user, session)
    url = await get_storage().get_presigned_put(
        blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"upload_url": url}


# ── Create (manual upload confirm) ───────────────────────────────────────────


@router.post("/projects/{project_id}/models", response_model=ModelVersionOut, status_code=201)
async def create_model_version(
    project_id: uuid.UUID,
    body: ModelVersionCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    await _get_project(project_id, current_user, session)
    await session.execute(
        pg_insert(Blob)
        .values(
            hash=body.blob_hash,
            storage_backend=settings.S3_BACKEND,
            storage_key=StorageBackend._bucket_key(body.blob_hash),
            size_bytes=body.size_bytes,
            media_type=body.media_type,
        )
        .on_conflict_do_nothing(index_elements=["hash"])
    )
    mv = ModelVersion(
        project_id=project_id,
        blob_hash=body.blob_hash,
        trained_on_commit_id=body.trained_on_commit_id,
        name=body.name,
        description=body.description,
        base_model=body.base_model,
        mlflow_run_id=body.mlflow_run_id,
        hyperparams=body.hyperparams,
        metrics=body.metrics,
        created_by=current_user.id,
    )
    session.add(mv)
    await session.commit()
    await session.refresh(mv)
    return ModelVersionOut.model_validate(mv)


# ── Read ──────────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/models", response_model=list[ModelVersionOut])
async def list_models(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ModelVersionOut]:
    await _get_project(project_id, current_user, session)
    r = await session.execute(select(ModelVersion).where(ModelVersion.project_id == project_id))
    return [ModelVersionOut.model_validate(mv) for mv in r.scalars().all()]


@router.get("/models/{id}", response_model=ModelVersionOut)
async def get_model(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    return ModelVersionOut.model_validate(await _get_model_version(id, current_user, session))


@router.get("/models/{id}/weights-url")
async def get_weights_url(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    mv = await _get_model_version(id, current_user, session)
    url = await get_storage().get_presigned_get(
        mv.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"url": url}


# ── Update ────────────────────────────────────────────────────────────────────


@router.patch("/models/{id}", response_model=ModelVersionOut)
async def patch_model_version(
    id: uuid.UUID,
    body: ModelVersionPatch,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    mv = await _get_model_version(id, current_user, session)
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(mv, key, val)
    await session.commit()
    await session.refresh(mv)
    return ModelVersionOut.model_validate(mv)
