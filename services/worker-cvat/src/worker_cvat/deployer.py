"""Deploy a YOLO .pt model to CVAT via Nuclio (nuctl)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

CVAT_NETWORK = os.environ.get("CVAT_NETWORK",              "cvat_cvat")
REDIS_HOST   = os.environ.get("CVAT_FUNCTIONS_REDIS_HOST", "cvat_redis_ondisk")
REDIS_PORT   = os.environ.get("CVAT_FUNCTIONS_REDIS_PORT", "6666")
BASE_IMAGE   = os.environ.get("YOLO_BASE_IMAGE",           "python:3.9-slim")
NUCTL        = os.environ.get("NUCTL_PATH",                "/usr/local/bin/nuctl")

_HANDLER_SRC = Path(__file__).parent / "nuclio_handler.py"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    return f"pth-custom-{re.sub(r'-+', '-', slug).strip('-')}"


def _extract_labels(pt_path: Path) -> list[dict]:
    from ultralytics import YOLO
    model = YOLO(str(pt_path))
    return [{"id": int(k), "name": v} for k, v in sorted(model.names.items())]


def _function_yaml(func_name: str, display_name: str, labels: list[dict]) -> str:
    return f"""metadata:
  name: {func_name}
  namespace: cvat
  annotations:
    name: {display_name}
    type: detector
    framework: pytorch
    spec: '{json.dumps(labels)}'

spec:
  description: {display_name} YOLO detector
  runtime: python:3.9
  handler: main:handler
  eventTimeout: 30s

  build:
    baseImage: {BASE_IMAGE}
    directives:
      postCopy:
        - kind: ENV
          value: MODEL_PATH=/opt/nuclio/model.pt

  triggers:
    myHttpTrigger:
      maxWorkers: 1
      kind: http
      workerAvailabilityTimeoutMilliseconds: 10000
      attributes:
        maxRequestBodySize: 33554432

  resources:
    requests:
      memory: 512Mi
    limits:
      memory: 2048Mi

platform:
  attributes:
    restartPolicy:
      name: always
      maximumRetryCount: 3
    mountMode: volume
"""


def _ensure_cvat_project() -> None:
    res = subprocess.run([NUCTL, "get", "project", "cvat", "--platform", "local"],
                        capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run([NUCTL, "create", "project", "cvat", "--platform", "local"],
                       capture_output=True, text=True, check=True)


def delete(function_id: str) -> None:
    """Remove a deployed Nuclio function from CVAT."""
    result = subprocess.run(
        [NUCTL, "delete", "function", function_id, "--platform", "local"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def deploy(pt_path: Path, model_name: str) -> str:
    """Deploy .pt to Nuclio inside CVAT. Returns the Nuclio function name."""
    func_name = slugify(model_name)
    labels = _extract_labels(pt_path)
    _ensure_cvat_project()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copy2(_HANDLER_SRC, tmp_path / "main.py")
        shutil.copy2(pt_path, tmp_path / "model.pt")
        (tmp_path / "function.yaml").write_text(
            _function_yaml(func_name, model_name, labels)
        )
        result = subprocess.run(
            [
                NUCTL, "deploy", func_name,
                "--project-name", "cvat",
                "--platform", "local",
                "--path", str(tmp_path),
                "--file", str(tmp_path / "function.yaml"),
                "--env", f"CVAT_FUNCTIONS_REDIS_HOST={REDIS_HOST}",
                "--env", f"CVAT_FUNCTIONS_REDIS_PORT={REDIS_PORT}",
                "--platform-config", json.dumps({"attributes": {"network": CVAT_NETWORK}}),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    return func_name
