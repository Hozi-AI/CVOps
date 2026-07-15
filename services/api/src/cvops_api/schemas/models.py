from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel


class ModelVersionOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    project_id: uuid.UUID
    blob_hash: str
    name: str | None = None
    description: str | None = None
    trained_on_commit_id: uuid.UUID | None = None
    base_model: str | None = None
    hyperparams: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    code_version: str | None = None
    mlflow_run_id: str | None = None
    created_at: datetime


class ModelVersionCreate(BaseModel):
    blob_hash: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    name: str | None = None
    description: str | None = None
    base_model: str | None = None
    trained_on_commit_id: uuid.UUID | None = None
    mlflow_run_id: str | None = None
    hyperparams: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None


class ModelVersionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    mlflow_run_id: str | None = None


class ModelArtifactOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    model_version_id: uuid.UUID
    blob_hash: str
    filename: str
    mime_type: str | None = None
    created_at: datetime
    url: str | None = None  # presigned GET, populated per-request
