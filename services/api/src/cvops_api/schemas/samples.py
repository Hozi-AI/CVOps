from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from pydantic import BaseModel, Field, model_validator

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class TagBrief(BaseModel):
    """Minimal tag shape embedded in a SampleOut."""

    model_config = {"from_attributes": True}
    id: uuid.UUID
    name: str
    color: str


class SampleOut(BaseModel):
    model_config = {"from_attributes": True, "populate_by_name": True}
    id: uuid.UUID
    project_id: uuid.UUID
    blob_hash: str
    source_id: uuid.UUID
    modality: str
    width: int
    height: int
    frame_index: int | None = None
    perceptual_hash: str | None = None
    # Reads the ORM's `metadata_` attribute but serializes as the clean `metadata`
    # (matches the frontend Sample type and avoids leaking the trailing underscore).
    metadata: dict[str, Any] | None = Field(
        None, validation_alias="metadata_", serialization_alias="metadata"
    )
    review_status: str = "unreviewed"
    created_at: datetime
    # Enriched per-page by core.sample_view.build_sample_outs (not plain ORM columns).
    tags: list[TagBrief] = []
    has_annotations: bool = False
    latest_revision_id: uuid.UUID | None = None


class SampleUpdate(BaseModel):
    """Edit a sample's mutable operational fields. Content (blob/dims) is immutable."""

    metadata: dict[str, Any] | None = None
    metadata_mode: Literal["merge", "replace"] = "merge"
    tag_ids: list[uuid.UUID] | None = None


BulkAction = Literal["delete", "set_review_status", "add_tags", "remove_tags", "add_to_collection"]

REVIEW_STATUSES = ("unreviewed", "accepted", "rejected")


class SampleBulkAction(BaseModel):
    """Unified bulk action over a set of samples (all scoped to one project)."""

    action: BulkAction
    sample_ids: list[uuid.UUID]
    review_status: str | None = None
    tag_ids: list[uuid.UUID] | None = None
    collection_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_required(self) -> "SampleBulkAction":
        if self.action == "set_review_status":
            if self.review_status not in REVIEW_STATUSES:
                raise ValueError(f"review_status must be one of {REVIEW_STATUSES}")
        elif self.action in ("add_tags", "remove_tags"):
            if not self.tag_ids:
                raise ValueError(f"tag_ids is required for action={self.action}")
        elif self.action == "add_to_collection":
            if self.collection_id is None:
                raise ValueError("collection_id is required for action=add_to_collection")
        return self


class BulkResult(BaseModel):
    matched: int
    affected: int
    skipped_ids: list[uuid.UUID] = []


class SampleIdList(BaseModel):
    sample_ids: list[uuid.UUID]


class TagIdList(BaseModel):
    tag_ids: list[uuid.UUID]


class AnnotationRevisionOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    sample_id: uuid.UUID
    ontology_id: uuid.UUID
    ontology_version: int
    revision_no: int
    # payload is the list of annotation objects ([{class_key, geometry}, ...]),
    # not a dict — typing it as a dict made response validation 500.
    payload: list[dict[str, Any]]
    provenance: dict[str, Any] | None = None
    created_at: datetime


class AnnotationCreate(BaseModel):
    ontology_id: uuid.UUID
    payload: dict[str, Any]
    provenance: dict[str, Any] | None = None
