"""HTTP-layer tests for the model-deployer FastAPI app.

All external work is mocked: ``deploy`` (Nuclio/nuctl), ``list_models`` and
``annotate`` (CVAT) are monkeypatched on the ``app`` module so no real model,
container build, or CVAT server is touched. ``ultralytics`` is stubbed in
conftest before import. These tests pin the service's request handling,
validation, and error mapping.
"""

from __future__ import annotations

import io


def test_health(client) -> None:
    res = client.get("/health")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "ok"}


def test_health_no_auth_allowed(unauth_client) -> None:
    res = unauth_client.get("/health")
    assert res.status_code == 200


def test_deploy_requires_auth(unauth_client) -> None:
    res = unauth_client.post(
        "/deploy",
        data={"model_name": "yolo"},
        files={"file": ("weights.pt", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert res.status_code == 401


def test_models_requires_auth(unauth_client) -> None:
    res = unauth_client.get("/models")
    assert res.status_code == 401


def test_annotate_requires_auth(unauth_client) -> None:
    res = unauth_client.post(
        "/annotate",
        data={"task_name": "t", "function_id": "fn", "threshold": "0.3"},
        files={"files": ("a.jpg", io.BytesIO(b"img"), "image/jpeg")},
    )
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# POST /deploy
# --------------------------------------------------------------------------- #


def test_deploy_happy_path(client, app_module, monkeypatch) -> None:
    captured: dict = {}

    def _fake_deploy(pt_path, model_name):
        captured["pt_name"] = pt_path.name
        captured["model_name"] = model_name
        return "yolo-detector"

    monkeypatch.setattr(app_module, "deploy", _fake_deploy)

    res = client.post(
        "/deploy",
        data={"model_name": "yolo"},
        files={
            "file": (
                "weights.pt",
                io.BytesIO(b"fake-weights"),
                "application/octet-stream",
            )
        },
    )

    assert res.status_code == 200, res.text
    assert res.json() == {
        "status": "ok",
        "function_name": "yolo-detector",
        "model_name": "yolo",
    }
    assert captured["model_name"] == "yolo"
    assert captured["pt_name"] == "weights.pt"


def test_deploy_rejects_non_pt_file(client, app_module, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "deploy", lambda *a, **k: "should-not-be-called")

    res = client.post(
        "/deploy",
        data={"model_name": "yolo"},
        files={"file": ("weights.onnx", io.BytesIO(b"x"), "application/octet-stream")},
    )

    assert res.status_code == 400, res.text
    assert "Only .pt files" in res.text


def test_deploy_maps_deploy_error_to_500(client, app_module, monkeypatch) -> None:
    def _boom(pt_path, model_name):
        raise RuntimeError("nuctl exploded")

    monkeypatch.setattr(app_module, "deploy", _boom)

    res = client.post(
        "/deploy",
        data={"model_name": "yolo"},
        files={"file": ("weights.pt", io.BytesIO(b"x"), "application/octet-stream")},
    )

    assert res.status_code == 500, res.text
    assert "nuctl exploded" in res.text


# --------------------------------------------------------------------------- #
# GET /models
# --------------------------------------------------------------------------- #


def test_models_passthrough(client, app_module, monkeypatch) -> None:
    models = [{"id": "yolo", "name": "YOLOv8", "kind": "detector", "description": ""}]
    monkeypatch.setattr(app_module, "list_models", lambda: models)

    res = client.get("/models")

    assert res.status_code == 200, res.text
    assert res.json() == models


def test_models_maps_cvat_error_to_502(client, app_module, monkeypatch) -> None:
    def _boom():
        raise RuntimeError("cvat down")

    monkeypatch.setattr(app_module, "list_models", _boom)

    res = client.get("/models")

    assert res.status_code == 502, res.text
    assert "Could not reach CVAT" in res.text
    assert "cvat down" in res.text


# --------------------------------------------------------------------------- #
# POST /annotate
# --------------------------------------------------------------------------- #


def test_annotate_calls_handler_with_form_params(client, app_module, monkeypatch) -> None:
    """annotate_task now takes form fields, so the handler is reachable."""
    called: dict = {}

    def _spy(**kwargs):
        called.update(kwargs)
        return {"task_id": 1, "job_id": 2, "cvat_url": "http://x"}

    monkeypatch.setattr(app_module, "annotate", _spy)

    res = client.post(
        "/annotate",
        data={"task_name": "t", "function_id": "fn", "threshold": "0.3"},
        files={"files": ("a.jpg", io.BytesIO(b"img"), "image/jpeg")},
    )

    assert res.status_code == 200, res.text
    assert called.get("task_name") == "t"
    assert called.get("function_id") == "fn"
