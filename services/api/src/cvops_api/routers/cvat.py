from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.models import CvatDeployment, ModelVersion
from cvops_api.db.models.projects import Project

logger = logging.getLogger(__name__)

router = APIRouter()

DEPLOYER_URL = settings.MODEL_DEPLOYER_URL
_DEPLOYER_HEADERS = {"Authorization": f"Bearer {settings.WORKER_TOKEN}"}


def _function_name(model_name: str) -> str:
    """Nuclio function name the deployer will give ``model_name``.

    Mirrors ``slugify`` in services/worker-cvat/src/worker_cvat/deployer.py (the
    live deployer; the legacy model-deployer uses the same rule). Needed up front
    so the name can be reserved for the caller's org *before* the deployer runs,
    and a name another org owns is refused instead of overwriting its function.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", model_name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"pth-custom-{slug}"


async def _get_model_version(mv_id: uuid.UUID, user: User, session: AsyncSession) -> ModelVersion:
    """Load a model version, or 404 if it doesn't exist or its project is in another org.

    Both cases return the same 404 so a caller can't tell whether an id exists.
    """
    r = await session.execute(select(ModelVersion).where(ModelVersion.id == mv_id))
    mv = r.scalar_one_or_none()
    if mv is None:
        raise HTTPException(404, "Model not found")
    proj = await session.get(Project, mv.project_id)
    if proj is None or proj.org_id != user.org_id:
        raise HTTPException(404, "Model not found")
    return mv


async def _check_project(project_id: uuid.UUID, user: User, session: AsyncSession) -> None:
    """404 unless the project exists, isn't deleted, and belongs to the caller's org."""
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
    """Names of the Nuclio functions the caller's org owns (used to filter the CVAT list)."""
    r = await session.execute(
        select(CvatDeployment.function_name).where(
            CvatDeployment.org_id == user.org_id,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    return set(r.scalars().all())


async def _get_deployment(function_id: str, user: User, session: AsyncSession) -> CvatDeployment:
    """The caller's org's ownership row for a function, or 404 if the org doesn't own it."""
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


async def _reserve_function(
    function_name: str, model_name: str, user: User, session: AsyncSession
) -> tuple[CvatDeployment, bool]:
    """Claim exclusive ownership of ``function_name`` for the caller's org.

    Runs *before* the deployer is called, so two orgs can never both deploy the
    same Nuclio function. Returns ``(row, created)``: an org that already owns
    the live row reuses it (a same-org redeploy/update, ``created=False``);
    otherwise a new row is inserted and committed. The partial unique index on
    ``function_name`` (``deleted_at IS NULL``) makes the claim atomic, so a
    racing insert by another org fails here and becomes a 409.
    """
    r = await session.execute(
        select(CvatDeployment).where(
            CvatDeployment.function_name == function_name,
            CvatDeployment.deleted_at.is_(None),
        )
    )
    dep = r.scalar_one_or_none()
    if dep is not None:
        if dep.org_id != user.org_id:
            raise HTTPException(409, "A deployed model with this name already exists")
        return dep, False

    dep = CvatDeployment(
        org_id=user.org_id,
        function_name=function_name,
        model_name=model_name,
        created_by=user.id,
    )
    session.add(dep)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "A deployed model with this name already exists")
    return dep, True


async def _release_reservation(dep_id: uuid.UUID, session: AsyncSession) -> None:
    """Soft-delete a reservation whose deploy failed, freeing the name again.

    Runs on a fresh transaction: whatever the failed request left in the
    session is rolled back first. Soft-delete (not a hard delete) follows the
    codebase convention; the unique index is partial, so it frees the name.
    """
    await session.rollback()
    await session.execute(
        update(CvatDeployment)
        .where(CvatDeployment.id == dep_id)
        .values(deleted_at=datetime.now(UTC))
    )
    await session.commit()


async def _finish_deployment(
    dep: CvatDeployment,
    model_name: str,
    model_version_id: uuid.UUID | None,
    session: AsyncSession,
) -> None:
    """Record what is now deployed under the caller's reserved function."""
    dep.model_name = model_name
    dep.model_version_id = model_version_id
    await session.commit()


def _warn_on_name_mismatch(reserved: str, result: dict[str, Any]) -> None:
    """Log if the deployer named the function differently from the reservation.

    Ownership is reserved on ``_function_name(model_name)`` before the deployer
    runs; if worker-cvat's slug rule ever drifts from it, the row would point at
    the wrong function. Surface that loudly rather than silently re-keying.
    """
    actual = result.get("function_name")
    if actual and actual != reserved:
        logger.error(
            "CVAT deployer returned function %r but ownership was reserved for %r",
            actual,
            reserved,
        )


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
    function_name = _function_name(model_name)
    dep, created = await _reserve_function(function_name, model_name, current_user, session)

    try:
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
                        "file": (
                            f"{model_name}.pt",
                            weights_resp.content,
                            "application/octet-stream",
                        )
                    },
                    timeout=300,
                )
        except httpx.ConnectError:
            raise HTTPException(503, "CVAT deployer is not available")

        if deploy_resp.status_code != 200:
            raise HTTPException(502, f"Deployer error: {deploy_resp.text}")
        result = cast(dict[str, Any], deploy_resp.json())
    except Exception:
        if created:
            await _release_reservation(dep.id, session)
        raise

    _warn_on_name_mismatch(function_name, result)
    await _finish_deployment(dep, model_name, mv.id, session)
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
    function_name = _function_name(model_name)
    dep, created = await _reserve_function(function_name, model_name, current_user, session)

    try:
        contents = await file.read()
        try:
            async with httpx.AsyncClient(timeout=300) as http:
                resp = await http.post(
                    f"{DEPLOYER_URL}/deploy",
                    headers=_DEPLOYER_HEADERS,
                    data={"model_name": model_name},
                    files={
                        "file": (file.filename or "model.pt", contents, "application/octet-stream")
                    },
                )
        except httpx.ConnectError:
            raise HTTPException(503, "CVAT deployer is not available")
        if resp.status_code != 200:
            raise HTTPException(502, f"Deploy error: {resp.text}")
        result = cast(dict[str, Any], resp.json())
    except Exception:
        if created:
            await _release_reservation(dep.id, session)
        raise

    _warn_on_name_mismatch(function_name, result)
    await _finish_deployment(dep, model_name, None, session)
    return result


# ── Delete a deployed model from CVAT ────────────────────────────────────────


@router.delete("/cvat/models/{function_id}")
async def cvat_delete_model(
    function_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Remove a deployed model the caller's org owns from CVAT.

    The org's ownership row is released only after the deployer confirms the
    delete, so a failed delete keeps the claim and can be retried.
    """
    dep = await _get_deployment(function_id, current_user, session)

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.delete(
                f"{DEPLOYER_URL}/models/{function_id}", headers=_DEPLOYER_HEADERS
            )
    except httpx.ConnectError:
        raise HTTPException(503, "CVAT deployer is not available")
    if resp.status_code != 200:
        raise HTTPException(502, f"Delete error: {resp.text}")

    dep.deleted_at = datetime.now(UTC)
    await session.commit()
    return cast(dict[str, Any], resp.json())


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
