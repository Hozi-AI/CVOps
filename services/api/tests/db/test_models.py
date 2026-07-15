import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.models import ModelVersion, TrainingContainer
from tests.db.conftest import make_blob, make_commit, make_project


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _make_training_container(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    **kwargs,
) -> TrainingContainer:
    if project_id is None:
        project_id = (await make_project(session)).id
    tc = TrainingContainer(
        project_id=project_id,
        name=f"tc-{uuid.uuid4().hex[:8]}",
        image="registry.example.com/trainer:latest",
        icd_config={"inputs": [], "outputs": []},
        **kwargs,
    )
    session.add(tc)
    await session.flush()
    return tc


# ---------------------------------------------------------------------------
# TrainingContainer tests
# ---------------------------------------------------------------------------


async def test_training_container_create(session: AsyncSession):
    project = await make_project(session)
    tc = TrainingContainer(
        project_id=project.id,
        name="yolov8-trainer",
        image="registry.example.com/yolov8:1.0",
        icd_config={"inputs": [], "outputs": []},
    )
    session.add(tc)
    await session.flush()

    assert tc.id is not None
    assert tc.project_id == project.id
    assert tc.image == "registry.example.com/yolov8:1.0"


async def test_training_container_schema_version_default(session: AsyncSession):
    project = await make_project(session)
    tc = TrainingContainer(
        project_id=project.id,
        name="default-schema-tc",
        image="registry.example.com/trainer:latest",
        icd_config={"inputs": [], "outputs": []},
    )
    session.add(tc)
    await session.flush()
    await session.refresh(tc)

    assert tc.icd_schema_version == "1.0"


async def test_training_container_unique_name(session: AsyncSession):
    project = await make_project(session)
    tc_a = TrainingContainer(
        project_id=project.id,
        name="duplicate-tc",
        image="registry.example.com/trainer:1.0",
        icd_config={"inputs": [], "outputs": []},
    )
    session.add(tc_a)
    await session.flush()

    tc_b = TrainingContainer(
        project_id=project.id,
        name="duplicate-tc",
        image="registry.example.com/trainer:2.0",
        icd_config={"inputs": [], "outputs": []},
    )
    session.add(tc_b)

    with pytest.raises(IntegrityError):
        await session.flush()

    await session.rollback()


async def test_training_container_icd_config_jsonb(session: AsyncSession):
    project = await make_project(session)
    complex_icd = {
        "inputs": [
            {"name": "dataset_path", "type": "volume", "mount": "/data/input"},
            {"name": "config_file", "type": "file", "mount": "/config/train.yaml"},
        ],
        "outputs": [
            {"name": "weights", "type": "volume", "mount": "/data/output"},
        ],
        "env": {"BATCH_SIZE": "16", "EPOCHS": "50"},
    }
    tc = TrainingContainer(
        project_id=project.id,
        name="complex-icd-tc",
        image="registry.example.com/trainer:latest",
        icd_config=complex_icd,
    )
    session.add(tc)
    await session.flush()
    await session.refresh(tc)

    assert isinstance(tc.icd_config["inputs"], list)
    assert len(tc.icd_config["inputs"]) == 2
    assert tc.icd_config["inputs"][0]["name"] == "dataset_path"
    assert tc.icd_config["env"]["BATCH_SIZE"] == "16"


# ---------------------------------------------------------------------------
# ModelVersion tests
# ---------------------------------------------------------------------------


async def test_model_version_create(session: AsyncSession):
    project = await make_project(session)
    blob = await make_blob(session)
    commit = await make_commit(session, project_id=project.id)
    tc = await _make_training_container(session, project_id=project.id)

    mv = ModelVersion(
        project_id=project.id,
        blob_hash=blob.hash,
        trained_on_commit_id=commit.id,
        training_container_id=tc.id,
    )
    session.add(mv)
    await session.flush()

    assert mv.id is not None
    assert mv.project_id == project.id
    assert mv.blob_hash == blob.hash
    assert mv.trained_on_commit_id == commit.id
    assert mv.training_container_id == tc.id


async def test_model_version_metrics_jsonb(session: AsyncSession):
    project = await make_project(session)
    blob = await make_blob(session)
    commit = await make_commit(session, project_id=project.id)
    tc = await _make_training_container(session, project_id=project.id)

    mv = ModelVersion(
        project_id=project.id,
        blob_hash=blob.hash,
        trained_on_commit_id=commit.id,
        training_container_id=tc.id,
        metrics={"mAP50": 0.87, "precision": 0.91, "recall": 0.83},
    )
    session.add(mv)
    await session.flush()
    await session.refresh(mv)

    assert mv.metrics["mAP50"] == 0.87
    assert mv.metrics["precision"] == 0.91
    assert mv.metrics["recall"] == 0.83


async def test_model_version_optional_fields_null(session: AsyncSession):
    project = await make_project(session)
    blob = await make_blob(session)
    commit = await make_commit(session, project_id=project.id)
    tc = await _make_training_container(session, project_id=project.id)

    mv = ModelVersion(
        project_id=project.id,
        blob_hash=blob.hash,
        trained_on_commit_id=commit.id,
        training_container_id=tc.id,
    )
    session.add(mv)
    await session.flush()

    assert mv.base_model is None
    assert mv.hyperparams is None
    assert mv.metrics is None
    assert mv.code_version is None
    assert mv.env_hash is None
    assert mv.seed is None
    assert mv.mlflow_run_id is None


async def test_model_version_name_description(session: AsyncSession):
    """ModelVersion accepts and persists name/description."""
    project = await make_project(session)
    blob = await make_blob(session)
    commit = await make_commit(session, project_id=project.id)

    mv = ModelVersion(
        project_id=project.id,
        blob_hash=blob.hash,
        trained_on_commit_id=commit.id,
        name="yolov8-nano-v1",
        description="Trained on dataset commit abc",
    )
    session.add(mv)
    await session.flush()
    assert mv.name == "yolov8-nano-v1"
    assert mv.description == "Trained on dataset commit abc"
