import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
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


class ModelArtifact(Base, EntityBase):
    """
    A file artifact (training plot, CSV, weight snapshot) attached to a
    model version. Stored as a content-addressed blob; filename is user-supplied.
    """

    __tablename__ = "model_artifacts"

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id"), nullable=False, index=True
    )
    blob_hash: Mapped[str] = mapped_column(ForeignKey("blobs.hash"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ModelArtifact id={self.id!r} model_version_id={self.model_version_id!r} "
            f"filename={self.filename!r}>"
        )


class CvatDeployment(Base, EntityBase):
    """
    Records which org owns a model deployed to CVAT as a Nuclio function.

    Nuclio functions live in one namespace shared by every tenant, so the
    function itself carries no owner. This row is what lets list/delete/annotate
    be scoped to the caller's org. Ownership is exclusive: at most one live row
    per function, enforced by a partial unique index, so two orgs can never both
    hold (and overwrite) the same function. The row is reserved before the
    deployer runs and soft-deleted if that deploy fails. Sharing a model across
    orgs needs an explicit flow of its own. ``model_version_id`` is null for
    functions deployed from a raw .pt upload.
    """

    __tablename__ = "cvat_deployments"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orgs.id"), nullable=False, index=True)
    function_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True, index=True
    )

    __table_args__ = (
        # One live owner per function. Partial so a released deployment frees
        # the name for any org to deploy again.
        Index(
            "uq_cvat_deployments_function_name",
            "function_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<CvatDeployment id={self.id!r} org_id={self.org_id!r} "
            f"function_name={self.function_name!r}>"
        )
