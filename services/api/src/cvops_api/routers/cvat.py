from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.models import CvatDeployment, ModelVersion
from cvops_api.db.models.projects import Project

router = APIRouter()

DEPLOYER_URL = settings.MODEL_DEPLOYER_URL
_DEPLOYER_HEADERS = {"Authorization": f"Bearer {settings.WORKER_TOKEN}"}


def _function_name(model_name: str) -> str:
    """Nuclio function name the deployer will give ``model_name``.

    Mirrors ``_slugify`` in services/model-deployer/deployer.py. Needed up front
    so a name already owned by another org is refused *before* the deployer
    overwrites that org's function.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", model_name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"pth-custom-{slug}"


async def _get_model_version(mv_id: uuid.UUID, user: User, session: AsyncSession) -> ModelVersion:
    r = await session.execute(select(ModelVersion).where(ModelVersion.id == mv_id))
    mv = r.scalar_one_or_none()
    if mv is None:
        raise HTTPException(404, "Model not found")
    proj = await session.get(Project, mv.project_id)
    if proj is None or proj.org_id != user.org_id:
        raise HTTPException(404, "Model not found")
    return mv


async def _check_project(project_id: uuid.UUID, user: User, session: AsyncSession) -> None:
    r = await session.execute(
        select(Project.id).where(
            Project.id == project_id,
            Project.org_id == user.org_id,
            Project.deleted_at.is_(None),
        )
    )
    if r.scalar_one_or_none() is None:
        raise HTTPException(404, "Project not found")


async def _owned_function_names(user: User, session: AsyncSession) -> set[str]:
    r = await session.execute(
        select(CvatDeployment.function_name).where(
            CvatDeployment.org_id == user.org_id,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    return set(r.scalars().all())


async def _get_deployment(function_id: str, user: User, session: AsyncSession) -> CvatDeployment:
    r = await session.execute(
        select(CvatDeployment).where(
            CvatDeployment.org_id == user.org_id,
            CvatDeployment.function_name == function_id,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    dep = r.scalar_one_or_none()
    if dep is None:
        raise HTTPException(404, "Deployed model not found")
    return dep


async def _check_name_available(function_name: str, user: User, session: AsyncSession) -> None:
    """Refuse a deploy that would overwrite a function another org owns."""
    r = await session.execute(
        select(CvatDeployment.id)
        .where(
            CvatDeployment.function_name == function_name,
            CvatDeployment.org_id != user.org_id,
            CvatDeployment.deleted_at.is_(None),
        )
        .limit(1)
    )
    if r.first() is not None:
        raise HTTPException(409, "A deployed model with this name already exists")


async def _claim_deployment(
    function_name: str,
    model_name: str,
    model_version_id: uuid.UUID | None,
    user: User,
    session: AsyncSession,
) -> None:
    """Record the caller's org as an owner of the function (idempotent)."""
    r = await session.execute(
        select(CvatDeployment).where(
            CvatDeployment.org_id == user.org_id,
            CvatDeployment.function_name == function_name,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    dep = r.scalar_one_or_none()
    if dep is None:
        session.add(
            CvatDeployment(
                org_id=user.org_id,
                function_name=function_name,
                model_name=model_name,
                model_version_id=model_version_id,
                created_by=user.id,
            )
        )
    else:
        dep.model_name = model_name
        dep.model_version_id = model_version_id
    await session.commit()


# ── Deploy a stored model to CVAT ────────────────────────────────────────────


@router.post("/models/{id}/cvat-deploy")
async def cvat_deploy_model(
    id: uuid.UUID,
    model_name: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Download model weights from MinIO and deploy to CVAT via Nuclio."""
    mv = await _get_model_version(id, current_user, session)
    await _check_name_available(_function_name(model_name), current_user, session)

    presigned_url = await get_storage().get_presigned_get(mv.blob_hash)

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            weights_resp = await http.get(presigned_url)
            if weights_resp.status_code != 200:
                raise HTTPException(502, "Could not fetch model weights from storage")

            deploy_resp = await http.post(
                f"{DEPLOYER_URL}/deploy",
                headers=_DEPLOYER_HEADERS,
                data={"model_name": model_name},
                files={
                    "file": (f"{model_name}.pt", weights_resp.content, "application/octet-stream")
                },
                timeout=300,
            )
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")

    if deploy_resp.status_code != 200:
        raise HTTPException(502, f"Deployer error: {deploy_resp.text}")

    result = cast(dict[str, Any], deploy_resp.json())
    await _claim_deployment(
        result.get("function_name") or _function_name(model_name),
        model_name,
        mv.id,
        current_user,
        session,
    )
    return result


# ── List models available in CVAT ─────────────────────────────────────────────


@router.get("/cvat/models")
async def list_cvat_models(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Return the models deployed in CVAT that belong to the caller's org."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"{DEPLOYER_URL}/models", headers=_DEPLOYER_HEADERS)
    except httpx.ConnectError:
        return []
    if resp.status_code != 200:
        raise HTTPException(502, f"Deployer error: {resp.text}")
    owned = await _owned_function_names(current_user, session)
    return [m for m in cast(list[dict[str, Any]], resp.json()) if m.get("id") in owned]


# ── Upload a .pt file and deploy it to CVAT ───────────────────────────────────


@router.post("/cvat/deploy")
async def cvat_deploy_file(
    model_name: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Accept a .pt upload and forward it to the CVAT worker for deployment."""
    await _check_name_available(_function_name(model_name), current_user, session)
    contents = await file.read()
    try:
        async with httpx.AsyncClient(timeout=300) as http:
            resp = await http.post(
                f"{DEPLOYER_URL}/deploy",
                headers=_DEPLOYER_HEADERS,
                data={"model_name": model_name},
                files={"file": (file.filename or "model.pt", contents, "application/octet-stream")},
            )
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")
    if resp.status_code != 200:
        raise HTTPException(502, f"Deploy error: {resp.text}")
    result = cast(dict[str, Any], resp.json())
    await _claim_deployment(
        result.get("function_name") or _function_name(model_name),
        model_name,
        None,
        current_user,
        session,
    )
    return result


# ── Delete a deployed model from CVAT ────────────────────────────────────────


@router.delete("/cvat/models/{function_id}")
async def cvat_delete_model(
    function_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Release the caller's org's claim on a deployed model.

    The Nuclio function itself is only removed from CVAT once no other org
    still owns it.
    """
    dep = await _get_deployment(function_id, current_user, session)

    r = await session.execute(
        select(func.count())
        .select_from(CvatDeployment)
        .where(
            CvatDeployment.function_name == function_id,
            CvatDeployment.org_id != current_user.org_id,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    result: dict[str, Any] = {"deleted": function_id}
    if r.scalar_one() == 0:
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.delete(
                    f"{DEPLOYER_URL}/models/{function_id}", headers=_DEPLOYER_HEADERS
                )
        except httpx.ConnectError:
            raise HTTPException(503, "CVAT deployer is not available")
        if resp.status_code != 200:
            raise HTTPException(502, f"Delete error: {resp.text}")
        result = cast(dict[str, Any], resp.json())

    dep.deleted_at = datetime.now(UTC)
    await session.commit()
    return result


# ── Trigger auto-annotation ───────────────────────────────────────────────────


@router.post("/projects/{project_id}/cvat-annotate")
async def cvat_annotate(
    project_id: uuid.UUID,
    task_name: str = Form(...),
    function_id: str = Form(...),
    threshold: float = Form(0.3),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Upload images and trigger auto-annotation in CVAT with the selected model."""
    await _check_project(project_id, current_user, session)
    await _get_deployment(function_id, current_user, session)
    form_data = {
        "task_name": task_name,
        "function_id": function_id,
        "threshold": str(threshold),
    }
    upload_files = [
        ("files", (f.filename, await f.read(), f.content_type or "image/jpeg")) for f in files
    ]

    try:
        async with httpx.AsyncClient(timeout=600) as http:
            resp = await http.post(
                f"{DEPLOYER_URL}/annotate",
                headers=_DEPLOYER_HEADERS,
                data=form_data,
                files=upload_files,
            )
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")

    if resp.status_code != 200:
        raise HTTPException(502, f"Deployer error: {resp.text}")

    return cast(dict[str, Any], resp.json())
