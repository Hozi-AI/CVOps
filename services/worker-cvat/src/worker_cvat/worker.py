"""Redis-Streams CVAT worker.

Mirrors the worker-preprocessing pattern exactly:
  • Consumes the ``cvat`` stream via a named consumer group.
  • Postgres is the authority for job state; Redis is the doorbell.
  • Registers DeployModelStep (queue="cvat") locally — no cvops_steps needed.

Also exposes GET /models on :8001 (proxied by the main API's cvat.py router
via MODEL_DEPLOYER_URL) so the dashboard can list Nuclio-deployed models.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
import signal
import socket
import uuid

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select

from cvops_api.core.redis_client import close_redis, get_redis, init_redis
from cvops_api.core.registry import registry
from cvops_api.db.models.runs import Run
from cvops_api.db.session import async_session_factory, engine
from cvops_api.engine.coordinator import (
    enqueue_step,
    find_orphan_step_runs,
    process_step,
    queue_for,
)

from worker_cvat.deploy_step import DeployModelStep
from worker_cvat.sync import handle_cvat_sync

STREAM = os.getenv("REDIS_STREAM", "cvat")
CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "4"))
ORPHAN_INTERVAL_SECONDS = 60
ORPHAN_MIN_AGE_SECONDS = 30
BLOCK_MS = 5000
HTTP_PORT = int(os.getenv("MODEL_DEPLOYER_PORT", "8001"))

GROUP = f"worker-{STREAM}"
CONSUMER = f"worker-{socket.gethostname()}-{os.getpid()}"
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _my_step_types() -> set[str]:
    """Step types whose queue routes to this worker's stream."""
    return {reg.type_key for reg in registry.all() if queue_for(reg.impl) == STREAM}


async def _ensure_group() -> None:
    redis = get_redis()
    try:
        await redis.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001 — BUSYGROUP means it already exists
        if "BUSYGROUP" not in str(exc):
            raise


async def _claim_and_run(job_id: str) -> None:
    run_id = uuid.UUID(job_id)
    async with async_session_factory() as session:
        r = await session.execute(
            select(Run)
            .where(Run.id == run_id, Run.status == "pending")
            .with_for_update(skip_locked=True)
        )
        claimed = r.scalar_one_or_none()
        if claimed is None:
            return
        await process_step(session, run_id, SYSTEM_ACTOR_ID)


async def _consume_loop(stop: asyncio.Event, sem: asyncio.Semaphore) -> None:
    redis = get_redis()
    inflight: set[asyncio.Task[None]] = set()

    async def _handle(msg_id: str, fields: dict[str, str]) -> None:
        # Two doorbell kinds arrive on the cvat stream:
        #   • {kind: "cvat_sync", cvat_task_id} — webhook bridge: a CVAT task is
        #     done; the pull handler resumes the parked review gate.
        #   • {job_id, step_type} — normal run doorbell; covers step.human_review
        #     (pushes the task, parks at the gate) and step.deploy_model.
        try:
            if fields.get("kind"):
                await handle_cvat_sync(fields)
            else:
                await _claim_and_run(fields["job_id"])
        finally:
            await redis.xack(STREAM, GROUP, msg_id)
            sem.release()

    while not stop.is_set():
        try:
            resp = await redis.xreadgroup(
                GROUP, CONSUMER, streams={STREAM: ">"}, count=CONCURRENCY, block=BLOCK_MS
            )
        except RedisTimeoutError:
            continue
        except (RedisConnectionError, asyncio.CancelledError):
            break
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                await sem.acquire()
                task = asyncio.create_task(_handle(msg_id, fields))
                inflight.add(task)
                task.add_done_callback(inflight.discard)

    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


async def _orphan_recovery_loop(stop: asyncio.Event) -> None:
    """Re-enqueue pending runs Redis may have dropped on restart."""
    my_types = _my_step_types()
    if not my_types:
        return
    while not stop.is_set():
        try:
            async with async_session_factory() as session:
                orphans = await find_orphan_step_runs(
                    session, my_types, ORPHAN_MIN_AGE_SECONDS
                )
            for run_id, step_type in orphans:
                await enqueue_step(run_id, step_type, STREAM)
        except Exception as exc:  # noqa: BLE001 — never let recovery kill the worker
            print(f"[orphan-recovery] error: {exc}", flush=True)
        for _ in range(ORPHAN_INTERVAL_SECONDS):
            if stop.is_set():
                break
            await asyncio.sleep(1)


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

    config = uvicorn.Config(
        _build_http_app(), host="0.0.0.0", port=HTTP_PORT, log_level="warning"
    )
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


async def run() -> None:
    await init_redis()
    # cvops_steps provides step.human_review (the CVAT review gate); DeployModelStep
    # (step.deploy_model) is local to this worker. Both route to the cvat queue.
    try:
        from cvops_steps import register_all

        register_all()
    except ImportError:
        print("[worker] cvops_steps not importable — human_review unavailable", flush=True)
    registry.register(DeployModelStep())

    await _ensure_group()
    print(
        f"[worker] {CONSUMER} joined group {GROUP!r} on stream {STREAM!r}; "
        f"steps={sorted(_my_step_types())} concurrency={CONCURRENCY} "
        f"http_port={HTTP_PORT}",
        flush=True,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    sem = asyncio.Semaphore(CONCURRENCY)
    orphan_task = asyncio.create_task(_orphan_recovery_loop(stop))
    http_task = asyncio.create_task(_http_server_loop(stop))

    try:
        await _consume_loop(stop, sem)
    finally:
        stop.set()
        await asyncio.gather(orphan_task, http_task, return_exceptions=True)
        await close_redis()
        await engine.dispose()
        print("[worker] shutdown complete", flush=True)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
