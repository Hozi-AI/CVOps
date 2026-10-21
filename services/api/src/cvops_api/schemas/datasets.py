from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator


def _validate_split_strategy(value: dict[str, Any]) -> dict[str, Any]:
    """Reject nonsensical commit split ratios at the schema boundary (422).

    The router reads ``train_ratio`` / ``val_ratio`` from ``split_strategy`` to
    slice a commit into train/val/test. Absent keys fall back to defaults and
    are left untouched here; when present each must be a real number in [0, 1]
    and together must not exceed 1.0 (the remainder becomes the test split).
    Catching this here turns a silently-wrong split into a clear validation
    error instead of letting ``int(total * ratio)`` produce garbage counts.
    """
    for key in ("train_ratio", "val_ratio"):
        if key not in value:
            continue
        ratio = value[key]
        # bool is an int subclass — exclude it so True/False isn't a "ratio".
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
            raise ValueError(f"{key} must be a number")
        if not 0.0 <= float(ratio) <= 1.0:
            raise ValueError(f"{key} must be between 0 and 1")
    train = float(value.get("train_ratio", 0.0))
    val = float(value.get("val_ratio", 0.0))
    if train + val > 1.0:
        raise ValueError("train_ratio + val_ratio must not exceed 1.0")
    return value


class DatasetCreate(BaseModel):
    name: str


class DatasetOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime


class CommitCreate(BaseModel):
    message: str
    sample_ids: list[uuid.UUID]
    annotation_revision_ids: list[uuid.UUID]
    split_strategy: dict[str, Any] = {}
    ontology_id: uuid.UUID
    branch_name: str = "main"

    @field_validator("split_strategy")
    @classmethod
    def _check_split_strategy(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_split_strategy(value)


class CommitOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    dataset_id: uuid.UUID
    parent_commit_id: uuid.UUID | None = None
    message: str | None = None
    stats: dict[str, Any] | None = None
    ontology_id: uuid.UUID | None = None
    ontology_version: int | None = None
    created_at: datetime


class CommitFromSamples(BaseModel):
    message: str = ""
    sample_ids: list[uuid.UUID]
    branch_name: str = "main"
    split_strategy: dict[str, Any] = {}
    ontology_id: uuid.UUID | None = None

    @field_validator("split_strategy")
    @classmethod
    def _check_split_strategy(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_split_strategy(value)


class CommitFromSamplesOut(BaseModel):
    commit_id: uuid.UUID
    committed_count: int
    skipped_count: int
    warnings: list[str] = []


class RefCreate(BaseModel):
    ref_type: str = "branch"
    name: str
    target_commit_id: uuid.UUID
    is_mutable: bool = True


class RefOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    dataset_id: uuid.UUID
    ref_type: str
    name: str
    target_commit_id: uuid.UUID
    is_mutable: bool


class DatasetLinkCreate(BaseModel):
    dataset_id: uuid.UUID
    pinned_commit_id: uuid.UUID | None = None
    ref_id: uuid.UUID | None = None


class DatasetLinkUpdate(BaseModel):
    pinned_commit_id: uuid.UUID | None = None
    ref_id: uuid.UUID | None = None


class DiffOut(BaseModel):
    added: list[uuid.UUID]
    removed: list[uuid.UUID]
    changed: list[uuid.UUID]
