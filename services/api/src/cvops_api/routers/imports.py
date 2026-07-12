"""POST /projects/{id}/imports — import an existing labeled dataset.

Accepts a zip blob (uploaded via /imports/upload-url) or a server-side folder
path and dispatches an inline import_dataset → [human_review?] → commit_dataset
DAG, exactly like the ad-hoc train endpoint in datasets.py.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.engine.coordinator import advance_workflow
from cvops_api.engine.dispatch import create_adhoc_run
from cvops_api.schemas.runs import ImportRequest, RunOut

router = APIRouter()


async def _check_project(
    project_id: uuid.UUID,
    user: User,
    session: AsyncSession,
) -> Project:
    r = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.org_id == user.org_id,
            Project.deleted_at.is_(None),
        )
    )
    proj = r.scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


def _public_endpoint(request: Request) -> str:
    pub = os.environ.get("S3_PUBLIC_ENDPOINT", "")
    if pub:
        return pub
    port = os.environ.get("S3_PUBLIC_PORT", "3900")
    return f"http://{request.url.hostname}:{port}"


@router.post(
    "/projects/{project_id}/imports/upload-url",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def get_import_upload_url(
    project_id: uuid.UUID,
    body: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a presigned PUT URL for uploading a zip blob.

    Body: {"blob_hash": "sha256:..."}
    Response: {"upload_url": "https://..."}
    """
    await _check_project(project_id, current_user, session)
    blob_hash = body.get("blob_hash", "")
    if not blob_hash.startswith("sha256:"):
        raise HTTPException(status_code=422, detail="blob_hash must start with 'sha256:'")
    url = await get_storage().get_presigned_put(
        blob_hash, endpoint=_public_endpoint(request)
    )
    return {"upload_url": url}


@router.post(
    "/projects/{project_id}/imports",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_dataset(
    project_id: uuid.UUID,
    body: ImportRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    """Dispatch an import_dataset → [human_review?] → commit_dataset run."""
    await _check_project(project_id, current_user, session)

    if bool(body.blob_hash) == bool(body.folder_path):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of blob_hash or folder_path must be provided",
        )
    if body.ontology_id is None:
        raise HTTPException(
            status_code=422,
            detail="ontology_id is required (commit_dataset needs it to create the commit)",
        )

    import_config: dict[str, Any] = {
        "format": body.format,
        "ontology_id": str(body.ontology_id),
    }

    import_inputs: dict[str, Any] = {}
    if body.blob_hash:
        import_inputs["blob_hash"] = body.blob_hash
    else:
        import_inputs["folder_path"] = body.folder_path

    commit_config: dict[str, Any] = {
        "dataset_name": body.dataset_name,
        "branch_name": "main",
        "message": "Imported dataset",
        "ontology_id": str(body.ontology_id),
    }

    if body.review:
        steps = [
            {
                "id": "import",
                "type": "step.import_dataset",
                "config": import_config,
                "inputs": import_inputs,
            },
            {
                "id": "review",
                "type": "step.human_review",
                "config": {},
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.import.outputs.annotation_revision_ids",
                },
            },
            {
                "id": "commit",
                "type": "step.commit_dataset",
                "config": commit_config,
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.review.outputs.annotation_revision_ids",
                },
            },
        ]
        edges = [{"from": "import", "to": "review"}, {"from": "review", "to": "commit"}]
    else:
        steps = [
            {
                "id": "import",
                "type": "step.import_dataset",
                "config": import_config,
                "inputs": import_inputs,
            },
            {
                "id": "commit",
                "type": "step.commit_dataset",
                "config": commit_config,
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.import.outputs.annotation_revision_ids",
                },
            },
        ]
        edges = [{"from": "import", "to": "commit"}]

    definition = {"steps": steps, "edges": edges}
    run = await create_adhoc_run(session, project_id, definition, {}, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    await session.refresh(run)
    return RunOut.model_validate(run)
