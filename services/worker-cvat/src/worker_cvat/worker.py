"""Redis-Streams CVAT worker.

Consumes the ``cvat`` stream through worker-common's ``ConsumerLoop``, the same
loop every worker runs: a named consumer group, retries with back-off, a
dead-letter queue (``cvat:dlq``), orphan recovery and graceful shutdown.
Postgres is the authority for job state; Redis is the doorbell. Two doorbell
kinds arrive on the stream:

  • ``{job_id, step_type}`` — a run doorbell for ``step.human_review`` (pushes
    the task to CVAT and parks at the gate) or ``step.deploy_model``;
  • ``{kind: "cvat_sync", cvat_task_id}`` — the webhook bridge: a CVAT task is
    done, and ``handle_cvat_sync`` resumes the parked review gate. These carry
    no runs row, so a failing one is retried rather than recovered from Postgres.

Also serves GET /models, POST /deploy and DELETE /models/{id} on :8001 (proxied
by the main API's cvat.py router via MODEL_DEPLOYER_URL) as a background task
of the loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse

from cvops_api.core.registry import registry
from cvops_worker_common import ConsumerLoop
from cvops_worker_common.config import worker_settings

from worker_cvat.deploy_step import DeployModelStep
from worker_cvat.sync import handle_cvat_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

STREAM = worker_settings.REDIS_STREAM or "cvat"
HTTP_PORT = int(os.getenv("MODEL_DEPLOYER_PORT", "8001"))


def _build_http_app():
    """Tiny FastAPI app exposed on :8001, proxied by the main API's cvat.py router."""
    from pathlib import Path

    app = FastAPI(title="worker-cvat")

    @app.get("/models")
    def get_models():
        from worker_cvat import cvat_client

        try:
            return JSONResponse(content=cvat_client.list_deployed_models())
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502, content={"error": str(exc)})

    @app.post("/deploy")
    async def deploy(model_name: str = Form(...), file: UploadFile = File(...)):
        try:
            from worker_cvat import deployer

            contents = await file.read()
            loop = asyncio.get_running_loop()
            with tempfile.TemporaryDirectory() as tmp:
                pt_path = Path(tmp) / "model.pt"
                pt_path.write_bytes(contents)
                function_name = await loop.run_in_executor(
                    None, deployer.deploy, pt_path, model_name
                )
            return JSONResponse(content={"function_name": function_name})
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"[deploy] error: {traceback.format_exc()}", flush=True)
            return JSONResponse(status_code=502, content={"error": str(exc)})

    @app.delete("/models/{function_id}")
    async def delete_model(function_id: str):
        try:
            from worker_cvat import deployer

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, deployer.delete, function_id)
            return JSONResponse(content={"deleted": function_id})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502, content={"error": str(exc)})

    return app


async def _http_server_loop(stop: asyncio.Event) -> None:
    import uvicorn

    config = uvicorn.Config(_build_http_app(), host="0.0.0.0", port=HTTP_PORT, log_level="warning")
    server = uvicorn.Server(config)
    try:
        serve_task = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await serve_task
    except (OSError, SystemExit) as exc:
        # ponytail: non-fatal — HTTP server is only needed for model deployment,
        # not for the CVAT review gate. Log and let the consume loop keep running.
        print(f"[worker] HTTP server on :{HTTP_PORT} unavailable: {exc}", flush=True)


def setup_registry() -> None:
    """Register the steps this worker runs; both route to the cvat queue."""
    # cvops_steps provides step.human_review (the CVAT review gate);
    # DeployModelStep (step.deploy_model) is local to this worker.
    try:
        from cvops_steps import register_all

        register_all()
    except ImportError:
        logger.warning("cvops_steps not importable — human_review unavailable")
    registry.register(DeployModelStep())


def build_loop() -> ConsumerLoop:
    # No step list: the loop consumes every registered step routed to this stream.
    return ConsumerLoop(
        stream=STREAM,
        sync_handler=handle_cvat_sync,
        background=[_http_server_loop],
    )


def main() -> None:
    setup_registry()
    asyncio.run(build_loop().run_forever())


if __name__ == "__main__":
    main()
