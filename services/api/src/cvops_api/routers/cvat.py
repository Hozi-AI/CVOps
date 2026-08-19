from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.models import ModelVersion

router = APIRouter()

DEPLOYER_URL = settings.MODEL_DEPLOYER_URL
_DEPLOYER_HEADERS = {"Authorization": f"Bearer {settings.WORKER_TOKEN}"}


async def _get_model_version(mv_id: uuid.UUID, user: User, session: AsyncSession) -> ModelVersion:
    r = await session.execute(select(ModelVersion).where(ModelVersion.id == mv_id))
    mv = r.scalar_one_or_none()
    if mv is None:
        raise HTTPException(404, "Model not found")
    return mv


# ── Deploy a stored model to CVAT ────────────────────────────────────────────

@router.post("/models/{id}/cvat-deploy")
async def cvat_deploy_model(
    id: uuid.UUID,
    model_name: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Download model weights from MinIO and deploy to CVAT via Nuclio."""
    mv = await _get_model_version(id, current_user, session)

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
                files={"file": (f"{model_name}.pt", weights_resp.content, "application/octet-stream")},
                timeout=300,
            )
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")

    if deploy_resp.status_code != 200:
        raise HTTPException(502, f"Deployer error: {deploy_resp.text}")

    return deploy_resp.json()


# ── List models available in CVAT ─────────────────────────────────────────────

@router.get("/cvat/models")
async def list_cvat_models(
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    """Return all models currently deployed in CVAT."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(f"{DEPLOYER_URL}/models", headers=_DEPLOYER_HEADERS)
    except httpx.ConnectError:
        return []
    if resp.status_code != 200:
        raise HTTPException(502, f"Deployer error: {resp.text}")
    return resp.json()


# ── Upload a .pt file and deploy it to CVAT ───────────────────────────────────

@router.post("/cvat/deploy")
async def cvat_deploy_file(
    model_name: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Accept a .pt upload and forward it to the CVAT worker for deployment."""
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
    return resp.json()


# ── Delete a deployed model from CVAT ────────────────────────────────────────

@router.delete("/cvat/models/{function_id}")
async def cvat_delete_model(
    function_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """Remove a Nuclio function from CVAT."""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.delete(f"{DEPLOYER_URL}/models/{function_id}", headers=_DEPLOYER_HEADERS)
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")
    if resp.status_code != 200:
        raise HTTPException(502, f"Delete error: {resp.text}")
    return resp.json()


# ── Trigger auto-annotation ───────────────────────────────────────────────────

@router.post("/projects/{project_id}/cvat-annotate")
async def cvat_annotate(
    project_id: uuid.UUID,
    task_name: str = Form(...),
    function_id: str = Form(...),
    threshold: float = Form(0.3),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Upload images and trigger auto-annotation in CVAT with the selected model."""
    form_data = {
        "task_name": task_name,
        "function_id": function_id,
        "threshold": str(threshold),
    }
    upload_files = [
        ("files", (f.filename, await f.read(), f.content_type or "image/jpeg"))
        for f in files
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

    return resp.json()
