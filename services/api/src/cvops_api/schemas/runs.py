from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel


class RunCreate(BaseModel):
    params: dict[str, Any] = {}


class RunOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    # The DAG node id + step type — the frontend needs step_id to address gate
    # actions (POST /runs/{id}/gates/{step_id}/resolve|sync); without it the UI
    # falls back to the run id and the gate lookup 404s.
    step_id: str | None = None
    step_type: str | None = None
    status: str
    attempt: int
    input_refs: dict[str, Any] | None = None
    output_refs: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class RunDetail(BaseModel):
    run: RunOut
    steps: list[RunOut]


class EventOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_type: str | None = None
    entity_type: str
    entity_id: uuid.UUID
    action: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ActivityEventOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    actor_id: uuid.UUID | None = None
    actor_type: str | None = None
    actor_email: str | None = None
    entity_type: str
    entity_id: uuid.UUID
    action: str
    payload: dict[str, Any] | None = None


class GateResolve(BaseModel):
    resolution: str


class TrainCommitRequest(BaseModel):
    git_url: str
    entry_point: str = "train.py"
    branch: str | None = None
    hyperparams: dict[str, Any] | None = None
    training_container_id: uuid.UUID | None = None


class ImportRequest(BaseModel):
    blob_hash: str | None = None
    folder_path: str | None = None
    format: str = "auto"
    ontology_id: uuid.UUID | None = None
    dataset_name: str = "Imported Dataset"
    commit_message: str = "Imported dataset"
    review: bool = False
    split_strategy: dict[str, Any] = {}
