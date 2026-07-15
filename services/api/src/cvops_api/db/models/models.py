import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cvops_api.db.base import Base, EntityBase


class TrainingContainer(Base, EntityBase):
    """
    Represents a versioned Docker container image with an Interface Contract
    Document (ICD) that describes its inputs, outputs, and volume-mount mapping.
    Each container belongs to exactly one project and must be uniquely named
    within that project.
    """

    __tablename__ = "training_containers"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    icd_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    icd_schema_version: Mapped[str] = mapped_column(
        Text, nullable=False, default="1.0", server_default="1.0"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_training_containers_project_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<TrainingContainer id={self.id!r} project_id={self.project_id!r} "
            f"name={self.name!r} image={self.image!r} "
            f"icd_schema_version={self.icd_schema_version!r}>"
        )


class ModelVersion(Base, EntityBase):
    """
    Represents a single trained model artifact linked to the exact dataset
    commit, training container, and blob (weights tar.gz) that produced it.
    Optional fields capture hyperparameters, evaluation metrics, reproducibility
    metadata (seed, env_hash, code_version), and an MLflow run reference.
    """

    __tablename__ = "model_versions"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id"), nullable=False, index=True
    )
    blob_hash: Mapped[str] = mapped_column(ForeignKey("blobs.hash"), nullable=False)
    trained_on_commit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("commits.id"), nullable=True
    )
    training_container_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("training_containers.id"), nullable=True
    )
    base_model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hyperparams: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    code_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    env_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mlflow_run_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ModelVersion id={self.id!r} project_id={self.project_id!r} "
            f"blob_hash={self.blob_hash!r} base_model={self.base_model!r} "
            f"training_container_id={self.training_container_id!r}>"
        )
