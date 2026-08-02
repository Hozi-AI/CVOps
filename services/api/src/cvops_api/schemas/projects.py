from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str
    task_type: str = "detection"
    modality: str = "image"
    settings: dict[str, Any] | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    task_type: str | None = None
    modality: str | None = None
    default_ontology_id: uuid.UUID | None = None
    default_ingest_workflow_id: uuid.UUID | None = None
    settings: dict[str, Any] | None = None


class ProjectOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    task_type: str
    modality: str
    default_ontology_id: uuid.UUID | None = None
    default_ingest_workflow_id: uuid.UUID | None = None
    created_at: datetime
