# worker-training Implementation Plan

**Status:** DONE

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `services/worker-training` — a self-contained worker service that launches user Docker training containers, captures metrics and weights on exit, and writes a `ModelVersion` record to PostgreSQL.

**Architecture:** Single-step worker (`step.train`) built on `packages/worker-common`. Reads the training container ICD from PG, downloads the export dataset from MinIO into a tmpdir, runs the user's Docker image with dataset + output mounts, reads `metrics.json` and weights on successful exit, uploads weights to MinIO, inserts a `ModelVersion` row, and returns `{model_version_id}` to the executor.

**Tech Stack:** Python 3.12, `docker` (docker-py >= 7.1), `sqlalchemy[asyncio]`, `cvops-worker-common`, `cvops-api`

---

## Key Context

Read these before touching any code:

- `docs/superpowers/specs/2026-06-14-worker-training-design.md` — approved design
- `docs/guides/implementing-worker-steps.md` — how to build a worker service on worker-common
- `docs/services/worker-training.md` — ICD: env vars, Docker flow, writes/reads table
- `packages/worker-common/src/cvops_worker_common/` — ConsumerLoop, run_job, session factory
- `services/api/src/cvops_api/engine/step.py` — StepContext, GateException
- `services/api/src/cvops_api/db/models/models.py` — TrainingContainer, ModelVersion (exact field names)
- `services/api/src/cvops_api/core/storage.py` — StorageBackend.save_bytes(), get_bytes()

## Prerequisite: Fix step.train inputs

`ModelVersion.trained_on_commit_id` is required but `step.train` currently only receives `{export_blob_hash}`. Before implementing the step, `step.export_yolo` must pass `commit_id` through in its output_refs.

Update `docs/services/step-contract.md` step table:
```
step.export_yolo → outputs: {export_blob_hash: "sha256:...", commit_id: uuid}
step.train       → inputs:  {export_blob_hash: "sha256:...", commit_id: uuid}
```

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `services/worker-training/pyproject.toml` | Create | Package metadata + deps |
| `services/worker-training/src/worker_training/__init__.py` | Create | Empty |
| `services/worker-training/src/worker_training/main.py` | Create | Entry point: register step + start ConsumerLoop |
| `services/worker-training/src/worker_training/steps/__init__.py` | Create | Empty |
| `services/worker-training/src/worker_training/steps/train.py` | Create | `TrainStep` — full `run()` implementation |
| `services/worker-training/tests/__init__.py` | Create | Empty |
| `services/worker-training/tests/conftest.py` | Create | Shared fixtures (ctx, docker mock, storage mock) |
| `services/worker-training/tests/test_train_step.py` | Create | All tests for TrainStep |
| `services/worker-training/Dockerfile` | Create | Build config |

---

## Task 1: Service Scaffold

**Files:**
- Create: `services/worker-training/pyproject.toml`
- Create: `services/worker-training/src/worker_training/__init__.py`
- Create: `services/worker-training/src/worker_training/steps/__init__.py`
- Create: `services/worker-training/tests/__init__.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p services/worker-training/src/worker_training/steps
mkdir -p services/worker-training/tests
touch services/worker-training/src/worker_training/__init__.py
touch services/worker-training/src/worker_training/steps/__init__.py
touch services/worker-training/tests/__init__.py
```

- [ ] **Step 2: Write pyproject.toml**

```toml
# services/worker-training/pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "worker-training"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "cvops-worker-common",
  "cvops-api",
  "docker>=7.1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
]

[tool.hatch.build.targets.wheel]
packages = ["src/worker_training"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 3: Install in dev mode**

```bash
cd services/worker-training
pip install -e ".[dev]"
pip install -e "../../packages/worker-common"
pip install -e "../../services/api[dev]"
```

- [ ] **Step 4: Commit**

```bash
git add services/worker-training/
git commit -m "chore: scaffold worker-training service"
```

---

## Task 2: Test Fixtures + TrainStep Skeleton

**Files:**
- Create: `services/worker-training/tests/conftest.py`
- Create: `services/worker-training/tests/test_train_step.py`
- Create: `services/worker-training/src/worker_training/steps/train.py`

- [ ] **Step 1: Write conftest.py with shared fixtures**

```python
# services/worker-training/tests/conftest.py
import io
import json
import os
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from cvops_api.engine.step import StepContext


def _make_export_tar() -> bytes:
    """Minimal tar.gz that extract_dataset() can unpack."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        content = b"fake image"
        info = tarfile.TarInfo(name="images/train/img.jpg")
        info.size = len(content)
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_weights_dir(tmp_path):
    """Create a fake weights directory for the output mock."""
    weights_dir = tmp_path / "output" / "weights"
    weights_dir.mkdir(parents=True)
    (weights_dir / "best.pt").write_bytes(b"fake weights")
    return weights_dir


@pytest.fixture
def run_id():
    return str(uuid.uuid4())


@pytest.fixture
def training_container_id():
    return str(uuid.uuid4())


@pytest.fixture
def project_id():
    return str(uuid.uuid4())


@pytest.fixture
def commit_id():
    return str(uuid.uuid4())


@pytest.fixture
def mock_tc(training_container_id):
    tc = MagicMock()
    tc.id = uuid.UUID(training_container_id)
    tc.image = "my-org/trainer:latest"
    tc.icd_config = {
        "inputs": {
            "dataset_path": {"env": "DATASET_PATH"},
            "epochs": {"env": "EPOCHS"},
            "batch_size": {"env": "BATCH_SIZE"},
        },
        "outputs": {
            "metrics_file": {"path": "/output/metrics.json"},
            "weights_path": {"path": "/output/weights/"},
        },
        "volume_mount": "/data/dataset",
        "mlflow_tracking_uri": None,
    }
    return tc


@pytest.fixture
def mock_storage():
    storage = AsyncMock()
    storage.get_bytes.return_value = _make_export_tar()
    storage.save_bytes.return_value = "sha256:abc123deadbeef"
    return storage


@pytest.fixture
def mock_session(mock_tc):
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_tc
    session.execute.return_value = result
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def ctx(run_id, project_id, mock_session, mock_storage):
    return StepContext(
        session=mock_session,
        storage=mock_storage,
        project_id=project_id,
        run_id=run_id,
        actor_id="service:worker",
        emit_event=AsyncMock(),
    )


@pytest.fixture
def base_config(training_container_id):
    return {
        "training_container_id": training_container_id,
        "hyperparams": {"epochs": 10, "batch_size": 16},
    }


@pytest.fixture
def base_inputs(commit_id):
    return {
        "export_blob_hash": "sha256:exportdeadbeef",
        "commit_id": commit_id,
    }


@pytest.fixture
def mock_docker():
    mock = MagicMock()
    container = MagicMock()
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b"Epoch 1/10\nTraining complete\n"
    mock.containers.run.return_value = container
    return mock, container
```

- [ ] **Step 2: Write TrainStep skeleton**

```python
# services/worker-training/src/worker_training/steps/train.py
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

import docker
from sqlalchemy import select

from cvops_api.db.models.models import ModelVersion, TrainingContainer
from cvops_api.engine.step import Step, StepContext


class TrainStep(Step):
    type_key = "step.train"
    config_schema = {
        "type": "object",
        "properties": {
            "training_container_id": {"type": "string"},
            "hyperparams": {"type": "object"},
        },
        "required": ["training_container_id"],
    }

    async def run(
        self, ctx: StepContext, config: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError
```

- [ ] **Step 3: Write one failing test to verify the skeleton**

```python
# services/worker-training/tests/test_train_step.py
import pytest
from worker_training.steps.train import TrainStep


@pytest.mark.asyncio
async def test_train_step_raises_not_implemented(ctx, base_config, base_inputs):
    step = TrainStep()
    with pytest.raises(NotImplementedError):
        await step.run(ctx, base_config, base_inputs)
```

- [ ] **Step 4: Run test — expect NotImplementedError**

```bash
cd services/worker-training
pytest tests/test_train_step.py::test_train_step_raises_not_implemented -v
```

Expected: `PASSED` (raises NotImplementedError as expected)

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "test: add TrainStep skeleton and test fixtures"
```

---

## Task 3: Dataset Download + Extraction

**Files:**
- Modify: `services/worker-training/src/worker_training/steps/train.py`
- Modify: `services/worker-training/tests/test_train_step.py`

- [ ] **Step 1: Write failing test for dataset download**

Add to `tests/test_train_step.py`:

```python
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_download_dataset_extracts_to_tmpdir(ctx, base_inputs, tmp_path):
    from worker_training.steps.train import _download_dataset

    dataset_dir = await _download_dataset(ctx, base_inputs["export_blob_hash"], tmp_path)

    ctx.storage.get_bytes.assert_awaited_once_with("sha256:exportdeadbeef")
    assert dataset_dir.exists()
    assert dataset_dir == tmp_path / "dataset"
    # the fake tar has images/train/img.jpg
    assert (dataset_dir / "images" / "train" / "img.jpg").exists()
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_train_step.py::test_download_dataset_extracts_to_tmpdir -v
```

Expected: `FAILED` — `cannot import name '_download_dataset'`

- [ ] **Step 3: Implement `_download_dataset`**

Add to `train.py` (above `TrainStep`):

```python
async def _download_dataset(
    ctx: StepContext, export_blob_hash: str, workdir: Path
) -> Path:
    raw = await ctx.storage.get_bytes(export_blob_hash)
    dataset_dir = workdir / "dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=__import__("io").BytesIO(raw), mode="r:gz") as tf:
        tf.extractall(dataset_dir)
    return dataset_dir
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_train_step.py::test_download_dataset_extracts_to_tmpdir -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "feat: implement dataset download and extraction"
```

---

## Task 4: ICD Loading + Docker Container Dispatch

**Files:**
- Modify: `services/worker-training/src/worker_training/steps/train.py`
- Modify: `services/worker-training/tests/test_train_step.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_train_step.py`:

```python
from unittest.mock import MagicMock, patch
import uuid


@pytest.mark.asyncio
async def test_load_training_container(ctx, base_config, mock_tc):
    from worker_training.steps.train import _load_training_container

    tc = await _load_training_container(ctx.session, base_config["training_container_id"])

    assert tc.image == "my-org/trainer:latest"
    assert "dataset_path" in tc.icd_config["inputs"]


@pytest.mark.asyncio
async def test_build_env_maps_hyperparams_and_dataset(mock_tc, tmp_path):
    from worker_training.steps.train import _build_env

    dataset_dir = tmp_path / "dataset"
    hyperparams = {"epochs": 10, "batch_size": 16}

    env = _build_env(mock_tc.icd_config, hyperparams, dataset_dir)

    assert env["DATASET_PATH"] == str(dataset_dir)
    assert env["EPOCHS"] == "10"
    assert env["BATCH_SIZE"] == "16"


@pytest.mark.asyncio
async def test_run_container_called_with_correct_args(mock_tc, tmp_path):
    from worker_training.steps.train import _run_container

    mock_client, mock_container = MagicMock(), MagicMock()
    mock_container.wait.return_value = {"StatusCode": 0}
    mock_container.logs.return_value = b"done"
    mock_client.containers.run.return_value = mock_container

    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    env = {"DATASET_PATH": str(dataset_dir), "EPOCHS": "10"}

    exit_code, logs = _run_container(mock_client, mock_tc, env, dataset_dir, output_dir)

    mock_client.containers.run.assert_called_once_with(
        "my-org/trainer:latest",
        environment=env,
        volumes={
            str(dataset_dir): {"bind": "/data/dataset", "mode": "ro"},
            str(output_dir): {"bind": "/output", "mode": "rw"},
        },
        detach=True,
        remove=True,
    )
    assert exit_code == 0
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
pytest tests/test_train_step.py::test_load_training_container \
       tests/test_train_step.py::test_build_env_maps_hyperparams_and_dataset \
       tests/test_train_step.py::test_run_container_called_with_correct_args -v
```

Expected: all `FAILED`

- [ ] **Step 3: Implement `_load_training_container`, `_build_env`, `_run_container`**

Add to `train.py`:

```python
async def _load_training_container(
    session: Any, training_container_id: str
) -> TrainingContainer:
    result = await session.execute(
        select(TrainingContainer).where(
            TrainingContainer.id == uuid.UUID(training_container_id)
        )
    )
    tc = result.scalar_one_or_none()
    if tc is None:
        raise RuntimeError(f"TrainingContainer {training_container_id!r} not found")
    return tc


def _build_env(
    icd_config: dict[str, Any],
    hyperparams: dict[str, Any],
    dataset_dir: Path,
) -> dict[str, str]:
    env: dict[str, str] = {}
    for param_name, mapping in icd_config["inputs"].items():
        env_var = mapping["env"]
        if param_name == "dataset_path":
            env[env_var] = str(dataset_dir)
        elif param_name in hyperparams:
            env[env_var] = str(hyperparams[param_name])
    return env


def _run_container(
    client: Any,
    tc: TrainingContainer,
    env: dict[str, str],
    dataset_dir: Path,
    output_dir: Path,
) -> tuple[int, str]:
    timeout = int(os.environ.get("DOCKER_TIMEOUT", "7200"))
    container = client.containers.run(
        tc.image,
        environment=env,
        volumes={
            str(dataset_dir): {"bind": tc.icd_config["volume_mount"], "mode": "ro"},
            str(output_dir): {"bind": "/output", "mode": "rw"},
        },
        detach=True,
        remove=True,
    )
    result = container.wait(timeout=timeout)
    logs = container.logs().decode("utf-8", errors="replace")
    return result["StatusCode"], logs
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/test_train_step.py::test_load_training_container \
       tests/test_train_step.py::test_build_env_maps_hyperparams_and_dataset \
       tests/test_train_step.py::test_run_container_called_with_correct_args -v
```

Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "feat: implement ICD loading, env building, docker dispatch"
```

---

## Task 5: Results Capture — metrics.json + Weights Upload

**Files:**
- Modify: `services/worker-training/src/worker_training/steps/train.py`
- Modify: `services/worker-training/tests/test_train_step.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_train_step.py`:

```python
import json
import io
import tarfile


@pytest.mark.asyncio
async def test_read_metrics_parses_json(tmp_path):
    from worker_training.steps.train import _read_metrics

    metrics = {"mAP50": 0.87, "loss": 0.043}
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "metrics.json").write_text(json.dumps(metrics))

    result = _read_metrics(output_dir, "/output/metrics.json")

    assert result == metrics


@pytest.mark.asyncio
async def test_read_metrics_raises_if_missing(tmp_path):
    from worker_training.steps.train import _read_metrics

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    with pytest.raises(RuntimeError, match="metrics.json not found"):
        _read_metrics(output_dir, "/output/metrics.json")


@pytest.mark.asyncio
async def test_upload_weights_returns_blob_hash(ctx, tmp_path):
    from worker_training.steps.train import _upload_weights

    output_dir = tmp_path / "output"
    weights_dir = output_dir / "weights"
    weights_dir.mkdir(parents=True)
    (weights_dir / "best.pt").write_bytes(b"fake weights")

    blob_hash = await _upload_weights(ctx, output_dir, "/output/weights/")

    assert blob_hash == "sha256:abc123deadbeef"
    ctx.storage.save_bytes.assert_awaited_once()
    call_args = ctx.storage.save_bytes.call_args
    assert call_args[0][1] == "application/x-tar"


@pytest.mark.asyncio
async def test_upload_weights_raises_if_missing(ctx, tmp_path):
    from worker_training.steps.train import _upload_weights

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    with pytest.raises(RuntimeError, match="weights not found"):
        await _upload_weights(ctx, output_dir, "/output/weights/")
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
pytest tests/test_train_step.py::test_read_metrics_parses_json \
       tests/test_train_step.py::test_read_metrics_raises_if_missing \
       tests/test_train_step.py::test_upload_weights_returns_blob_hash \
       tests/test_train_step.py::test_upload_weights_raises_if_missing -v
```

Expected: all `FAILED`

- [ ] **Step 3: Implement `_read_metrics` and `_upload_weights`**

Add to `train.py`:

```python
def _read_metrics(output_dir: Path, metrics_path: str) -> dict[str, Any]:
    path = output_dir / Path(metrics_path).relative_to("/")
    if not path.exists():
        raise RuntimeError(f"metrics.json not found at {path}")
    return json.loads(path.read_text())


async def _upload_weights(
    ctx: StepContext, output_dir: Path, weights_path: str
) -> str:
    weights_dir = output_dir / Path(weights_path).relative_to("/")
    if not weights_dir.exists() or not any(weights_dir.iterdir()):
        raise RuntimeError(f"weights not found at {weights_dir}")

    buf = __import__("io").BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(weights_dir, arcname="weights")
    blob_hash = await ctx.storage.save_bytes(buf.getvalue(), "application/x-tar")
    return blob_hash
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
pytest tests/test_train_step.py::test_read_metrics_parses_json \
       tests/test_train_step.py::test_read_metrics_raises_if_missing \
       tests/test_train_step.py::test_upload_weights_returns_blob_hash \
       tests/test_train_step.py::test_upload_weights_raises_if_missing -v
```

Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "feat: implement metrics.json parsing and weights upload"
```

---

## Task 6: ModelVersion Write

**Files:**
- Modify: `services/worker-training/src/worker_training/steps/train.py`
- Modify: `services/worker-training/tests/test_train_step.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_train_step.py`:

```python
@pytest.mark.asyncio
async def test_write_model_version_inserts_row(ctx, mock_tc, commit_id):
    from worker_training.steps.train import _write_model_version

    metrics = {"mAP50": 0.87, "loss": 0.043, "mlflow_run_id": "mlflow-abc"}
    weights_blob_hash = "sha256:weightsdeadbeef"
    hyperparams = {"epochs": 10}

    model_version_id = await _write_model_version(
        ctx, mock_tc, commit_id, weights_blob_hash, metrics, hyperparams
    )

    ctx.session.add.assert_called_once()
    mv = ctx.session.add.call_args[0][0]
    assert mv.blob_hash == weights_blob_hash
    assert mv.trained_on_commit_id == uuid.UUID(commit_id)
    assert mv.training_container_id == mock_tc.id
    assert mv.metrics == metrics
    assert mv.hyperparams == hyperparams
    assert mv.mlflow_run_id == "mlflow-abc"
    assert mv.seed == hyperparams.get("seed")
    assert model_version_id == str(mv.id)
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_train_step.py::test_write_model_version_inserts_row -v
```

Expected: `FAILED`

- [ ] **Step 3: Implement `_write_model_version`**

Add to `train.py`:

```python
async def _write_model_version(
    ctx: StepContext,
    tc: TrainingContainer,
    commit_id: str,
    weights_blob_hash: str,
    metrics: dict[str, Any],
    hyperparams: dict[str, Any],
) -> str:
    mv = ModelVersion(
        project_id=uuid.UUID(ctx.project_id),
        blob_hash=weights_blob_hash,
        trained_on_commit_id=uuid.UUID(commit_id),
        training_container_id=tc.id,
        hyperparams=hyperparams,
        metrics=metrics,
        mlflow_run_id=metrics.get("mlflow_run_id"),
        seed=hyperparams.get("seed"),
    )
    ctx.session.add(mv)
    await ctx.session.flush()
    return str(mv.id)
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_train_step.py::test_write_model_version_inserts_row -v
```

Expected: `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "feat: implement ModelVersion write"
```

---

## Task 7: Wire `run()` + Error Handling + Cleanup

**Files:**
- Modify: `services/worker-training/src/worker_training/steps/train.py`
- Modify: `services/worker-training/tests/test_train_step.py`

- [ ] **Step 1: Write failing tests for the full `run()` method**

Add to `tests/test_train_step.py`:

```python
from unittest.mock import patch


@pytest.mark.asyncio
async def test_run_happy_path_returns_model_version_id(
    ctx, base_config, base_inputs, mock_tc, tmp_path
):
    step = TrainStep()

    def fake_run_container(client, tc, env, dataset_dir, output_dir):
        # simulate container writing outputs
        (output_dir / "metrics.json").write_text(
            json.dumps({"mAP50": 0.87, "mlflow_run_id": "mlflow-run-1"})
        )
        weights = output_dir / "weights"
        weights.mkdir()
        (weights / "best.pt").write_bytes(b"weights")
        return 0, "Training complete"

    with patch("worker_training.steps.train.docker.from_env") as mock_de, \
         patch("worker_training.steps.train._run_container", side_effect=fake_run_container), \
         patch("worker_training.steps.train.tempfile.mkdtemp", return_value=str(tmp_path)):

        result = await step.run(ctx, base_config, base_inputs)

    assert "model_version_id" in result
    ctx.storage.get_bytes.assert_awaited_once_with(base_inputs["export_blob_hash"])
    ctx.storage.save_bytes.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_nonzero_exit_raises_runtime_error(
    ctx, base_config, base_inputs, tmp_path
):
    step = TrainStep()

    def fake_run_container(client, tc, env, dataset_dir, output_dir):
        return 1, "CUDA out of memory\nFatal error"

    with patch("worker_training.steps.train.docker.from_env"), \
         patch("worker_training.steps.train._run_container", side_effect=fake_run_container), \
         patch("worker_training.steps.train.tempfile.mkdtemp", return_value=str(tmp_path)):

        with pytest.raises(RuntimeError, match="CUDA out of memory"):
            await step.run(ctx, base_config, base_inputs)


@pytest.mark.asyncio
async def test_run_cleans_up_tmpdir_on_success(
    ctx, base_config, base_inputs, tmp_path
):
    step = TrainStep()

    def fake_run_container(client, tc, env, dataset_dir, output_dir):
        (output_dir / "metrics.json").write_text(json.dumps({"mAP50": 0.9}))
        weights = output_dir / "weights"
        weights.mkdir()
        (weights / "best.pt").write_bytes(b"w")
        return 0, "ok"

    with patch("worker_training.steps.train.docker.from_env"), \
         patch("worker_training.steps.train._run_container", side_effect=fake_run_container), \
         patch("worker_training.steps.train.tempfile.mkdtemp", return_value=str(tmp_path)), \
         patch("shutil.rmtree") as mock_rm:

        await step.run(ctx, base_config, base_inputs)

    mock_rm.assert_called_once_with(str(tmp_path), ignore_errors=True)


@pytest.mark.asyncio
async def test_run_cleans_up_tmpdir_on_failure(
    ctx, base_config, base_inputs, tmp_path
):
    step = TrainStep()

    def fake_run_container(client, tc, env, dataset_dir, output_dir):
        return 1, "crash"

    with patch("worker_training.steps.train.docker.from_env"), \
         patch("worker_training.steps.train._run_container", side_effect=fake_run_container), \
         patch("worker_training.steps.train.tempfile.mkdtemp", return_value=str(tmp_path)), \
         patch("shutil.rmtree") as mock_rm:

        with pytest.raises(RuntimeError):
            await step.run(ctx, base_config, base_inputs)

    mock_rm.assert_called_once_with(str(tmp_path), ignore_errors=True)
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
pytest tests/test_train_step.py::test_run_happy_path_returns_model_version_id \
       tests/test_train_step.py::test_run_nonzero_exit_raises_runtime_error \
       tests/test_train_step.py::test_run_cleans_up_tmpdir_on_success \
       tests/test_train_step.py::test_run_cleans_up_tmpdir_on_failure -v
```

Expected: all `FAILED` — `run()` raises `NotImplementedError`

- [ ] **Step 3: Implement `run()`**

Replace `raise NotImplementedError` in `TrainStep.run()`:

```python
    async def run(
        self, ctx: StepContext, config: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        export_blob_hash: str = inputs["export_blob_hash"]
        commit_id: str = inputs["commit_id"]
        training_container_id: str = config["training_container_id"]
        hyperparams: dict[str, Any] = config.get("hyperparams", {})

        tc = await _load_training_container(ctx.session, training_container_id)
        client = docker.from_env()
        workdir = Path(tempfile.mkdtemp())

        try:
            dataset_dir = await _download_dataset(ctx, export_blob_hash, workdir)
            output_dir = workdir / "output"
            output_dir.mkdir()

            env = _build_env(tc.icd_config, hyperparams, dataset_dir)
            exit_code, logs = _run_container(client, tc, env, dataset_dir, output_dir)

            if exit_code != 0:
                raise RuntimeError(logs[-500:])

            metrics = _read_metrics(output_dir, tc.icd_config["outputs"]["metrics_file"]["path"])
            weights_blob_hash = await _upload_weights(
                ctx, output_dir, tc.icd_config["outputs"]["weights_path"]["path"]
            )
            model_version_id = await _write_model_version(
                ctx, tc, commit_id, weights_blob_hash, metrics, hyperparams
            )

            await ctx.emit_event(
                actor_id=ctx.actor_id,
                actor_type="service",
                entity_type="run",
                entity_id=ctx.run_id,
                action="train.completed",
                payload={"model_version_id": model_version_id},
            )

            return {"model_version_id": model_version_id}

        finally:
            shutil.rmtree(str(workdir), ignore_errors=True)
```

- [ ] **Step 4: Run all tests — expect PASS**

```bash
pytest tests/ -v
```

Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add services/worker-training/
git commit -m "feat: implement TrainStep.run() with error handling and cleanup"
```

---

## Task 8: Entry Point

**Files:**
- Create: `services/worker-training/src/worker_training/main.py`

- [ ] **Step 1: Write main.py**

```python
# services/worker-training/src/worker_training/main.py
from __future__ import annotations

import asyncio
import logging

from cvops_api.core.registry import registry
from cvops_worker_common import ConsumerLoop

from worker_training.steps.train import TrainStep

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


def main() -> None:
    registry.register(TrainStep())
    loop = ConsumerLoop(
        stream="training",
        step_types=["step.train"],
    )
    asyncio.run(loop.run_forever())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify import works**

```bash
cd services/worker-training
python -c "from worker_training.main import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add services/worker-training/src/worker_training/main.py
git commit -m "feat: add worker-training entry point"
```

---

## Task 9: Dockerfile

**Files:**
- Create: `services/worker-training/Dockerfile`

- [ ] **Step 1: Write Dockerfile**

```dockerfile
# services/worker-training/Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install shared deps first — layer cached until they change
COPY packages/worker-common ./packages/worker-common
COPY services/api ./services/api
RUN pip install --no-cache-dir \
    ./packages/worker-common \
    ./services/api

# Install this worker
COPY services/worker-training ./services/worker-training
RUN pip install --no-cache-dir ./services/worker-training

CMD ["python", "-m", "worker_training.main"]
```

Note: Docker socket is mounted at runtime via docker-compose — not in the image.

- [ ] **Step 2: Add to docker-compose (in `manifests/docker-compose.yml` under the `worker` or `phase2` profile)**

```yaml
worker-training:
  build:
    context: ..
    dockerfile: services/worker-training/Dockerfile
  environment:
    DATABASE_URL: postgresql+asyncpg://cvops:cvops@postgres:5432/cvops
    REDIS_URL: redis://redis:6379/0
    REDIS_STREAM: training
    S3_ENDPOINT: http://garage:3900
    S3_ACCESS_KEY: ${GARAGE_KEY_ID}
    S3_SECRET_KEY: ${GARAGE_SECRET_KEY}
    S3_BUCKET: cvops-blobs
    WORKER_TOKEN: ${WORKER_TOKEN}
    API_BASE_URL: http://api:8000
    DOCKER_TIMEOUT: "7200"
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
  depends_on:
    - postgres
    - redis
    - garage
  profiles: ["worker", "all"]
  restart: unless-stopped
```

- [ ] **Step 3: Run full test suite one last time**

```bash
cd services/worker-training
pytest tests/ -v
```

Expected: all `PASSED`

- [ ] **Step 4: Final commit**

```bash
git add services/worker-training/Dockerfile
git add manifests/docker-compose.yml
git commit -m "chore: add worker-training Dockerfile and compose service"
```

---

## Self-Review

**Spec coverage:**
- ✅ `services/worker-training/` directory structure
- ✅ `pyproject.toml` with docker dep
- ✅ `step.train` full flow: download → run → capture → write model_version
- ✅ MLflow: reads `mlflow_run_id` from metrics, stores on `model_version`, no SDK
- ✅ Error handling: non-zero exit raises RuntimeError with last 500 chars of logs
- ✅ Cleanup: `finally` block, `shutil.rmtree(ignore_errors=True)`
- ✅ Concurrency: one job per worker (ConsumerLoop count=1 via WORKER_CONCURRENCY=1)
- ✅ Docker socket via compose volume mount
- ✅ Dockerfile
- ✅ `trained_on_commit_id` sourced from `inputs["commit_id"]` (passed through from export_yolo)

**Placeholder scan:** None found.

**Type consistency:**
- `_download_dataset(ctx, export_blob_hash, workdir)` → `Path` — consistent across Tasks 3, 7
- `_run_container(client, tc, env, dataset_dir, output_dir)` → `tuple[int, str]` — consistent across Tasks 4, 7
- `_read_metrics(output_dir, metrics_path)` → `dict` — consistent across Tasks 5, 7
- `_upload_weights(ctx, output_dir, weights_path)` → `str` — consistent across Tasks 5, 7
- `_write_model_version(ctx, tc, commit_id, weights_blob_hash, metrics, hyperparams)` → `str` — consistent across Tasks 6, 7
