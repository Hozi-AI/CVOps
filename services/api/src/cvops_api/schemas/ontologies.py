from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel


class OntologyCreate(BaseModel):
    name: str


class OntologyUpdate(BaseModel):
    name: str


class OntologyOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    version: int
    created_at: datetime


class LabelClassCreate(BaseModel):
    class_key: str
    display_name: str
    color: str = "#FF0000"
    sort_order: int


class LabelClassUpdate(BaseModel):
    display_name: str | None = None
    color: str | None = None
    sort_order: int | None = None


class LabelClassOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    ontology_id: uuid.UUID
    class_key: str
    display_name: str
    color: str
    sort_order: int
