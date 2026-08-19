"""
Workflow coordinator — the orchestration logic shared by the API producer and
the workers, now that steps execute out-of-process on Redis Streams.

Two halves:

  * ``advance_workflow`` — looks at a parent workflow run, creates child ``runs``
    rows for every step that just became ready, and ``XADD``s a thin doorbell
    message onto each step's queue. It does NOT run any step. Called by the API
    on the initial kick and by a worker after each step finishes.

  * ``process_step`` — runs exactly one already-created child run (claimed by the
    worker via ``FOR UPDATE SKIP LOCKED``). On success it calls
    ``advance_workflow`` to enqueue whatever became ready next.

Postgres is the authority for job state; Redis only carries
``{job_id, step_type, queue}`` (see ``docs/services/redis-streams.md``).
"""

from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime, timedelta, UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.audit import emit_event
from cvops_api.core.redis_client import get_redis
from cvops_api.core.registry import registry
from cvops_api.core.storage import get_storage
from cvops_api.db.models.projects import Project
from cvops_api.db.models.runs import Run
from cvops_api.db.models.workflows import Workflow
from cvops_api.engine.ref_resolver import resolve_refs, ResolutionError
from cvops_api.engine.step import StepContext, GateException

DEFAULT_QUEUE = "preprocessing"


# ── Dispatch helpers ────────────────────────────────────────────────────────


def queue_for(step_impl: Any) -> str:
    """Resolve the Redis Stream a step is dispatched to.

    Empty/unset ``queue`` on the step → the default preprocessing stream, so
    existing steps (e.g. extract_frames) route correctly with no edit.
    """
    return getattr(step_impl, "queue", "") or DEFAULT_QUEUE


async def find_orphan_step_runs(
    session: AsyncSession,
    step_types: set[str],
    min_age_seconds: int = 30,
) -> list[tuple[uuid.UUID, str]]:
    """Pending step runs older than ``min_age_seconds`` for the given types.

    The safety net for messages Redis dropped on restart: a worker re-enqueues
    these. The ``status='pending'`` claim guard + consumer-group dedup keep
    re-enqueue from double-running anything already in flight.
    """
    if not step_types:
        return []
    cutoff = datetime.now(UTC) - timedelta(seconds=min_age_seconds)
    rows = await session.execute(
        select(Run.id, Run.step_type).where(
            Run.status == "pending",
            Run.step_type.in_(step_types),
            Run.created_at < cutoff,
        )
    )
    return [(rid, stype) for rid, stype in rows.all()]


async def enqueue_step(run_id: uuid.UUID, step_type: str, queue: str) -> None:
    """XADD a thin doorbell message for one step run.

    Only the pointer travels over Redis; the worker reloads full state from PG.
    Wrapped so a Redis outage surfaces as a clear error to the producer rather
    than a bare connection traceback (the API already depends on Redis).
    """
    try:
        await get_redis().xadd(
            queue,
            {"job_id": str(run_id), "step_type": step_type, "queue": queue},
        )
    except Exception as exc:  # noqa: BLE001 — re-raise with context, don't swallow
        raise RuntimeError(
            f"Failed to enqueue step {run_id} onto Redis stream {queue!r}: {exc}"
        ) from exc


# ── Coordinator ─────────────────────────────────────────────────────────────


async def advance_workflow(
    session: AsyncSession,
    parent_run_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Create + enqueue every step that is now ready on a workflow run.

    Idempotent and concurrency-safe: it takes ``FOR UPDATE`` on the parent so
    two concurrent advances (e.g. two workers finishing sibling steps) can't
    double-create or double-enqueue the same child.
    """
    # 1. Lock the parent for the duration of this advance.
    r = await session.execute(select(Run).where(Run.id == parent_run_id).with_for_update())
    parent = r.scalar_one_or_none()
    if parent is None:
        return
    if parent.status in {"succeeded", "failed", "cancelled"}:
        await session.commit()
        return

    proj = await session.get(Project, parent.project_id)
    org_id = proj.org_id if proj else None

    # Ad-hoc runs (no saved Workflow) carry their DAG inline on the parent's
    # config; saved workflow runs load it from the Workflow row.
    if parent.workflow_id is None:
        definition = (parent.config or {}).get("definition") or {}
        if not definition:
            await _fail(session, parent, actor_id, "Ad-hoc run has no definition", org_id=org_id)
            await session.commit()
            return
    else:
        wf_result = await session.execute(
            select(Workflow).where(Workflow.id == parent.workflow_id)
        )
        workflow = wf_result.scalar_one_or_none()
        if workflow is None:
            await _fail(session, parent, actor_id, "Workflow definition not found", org_id=org_id)
            await session.commit()
            return
        definition = workflow.definition or {}

    steps_list = definition.get("steps", [])
    edges = definition.get("edges", [])
    steps_by_id = {s["id"]: s for s in steps_list}

    # 2. Topological order (also rejects cycles).
    ordered = _topo_sort(list(steps_by_id.keys()), edges)
    if ordered is None:
        await _fail(session, parent, actor_id, "Workflow has a cycle", org_id=org_id)
        await session.commit()
        return

    # Predecessor map from edges: step_id → [ids that must finish first].
    preds: dict[str, list[str]] = {sid: [] for sid in steps_by_id}
    for e in edges:
        src, dst = _edge_endpoints(e)
        preds.setdefault(dst, []).append(src)

    # 3. Rebuild step_outputs + statuses from existing child runs.
    step_outputs: dict[str, dict[str, Any]] = {}
    child_status: dict[str, str] = {}
    existing = await session.execute(select(Run).where(Run.parent_run_id == parent_run_id))
    for prev in existing.scalars().all():
        if not prev.step_id:
            continue
        child_status[prev.step_id] = prev.status
        if prev.status == "succeeded":
            step_outputs[prev.step_id] = prev.output_refs or {}

    # 4. Terminal-state short circuits.
    if any(st == "failed" for st in child_status.values()):
        if parent.status != "failed":
            await _fail(session, parent, actor_id, "A step failed")
        await session.commit()
        return
    if any(st == "waiting" for st in child_status.values()):
        await session.commit()
        return  # paused at a gate; the gate-resolve endpoint advances later

    run_params: dict[str, Any] = (parent.input_refs or {}).get("params", {})

    # 5. Create each ready step. Enqueues are deferred until after a single
    #    commit so the parent FOR UPDATE lock is held through the whole creation
    #    phase (serializing concurrent advances) and no worker can see a doorbell
    #    message before its row is durable.
    to_enqueue: list[tuple[uuid.UUID, str, str]] = []
    for step_id in ordered:
        if step_id in child_status:
            continue  # already created (pending/running/succeeded)
        # Ready = every predecessor has a succeeded child run.
        if not all(child_status.get(p) == "succeeded" for p in preds.get(step_id, [])):
            continue

        step_def = steps_by_id[step_id]
        type_key: str = step_def["type"]
        config: dict[str, Any] = step_def.get("config", {})

        # Validate config.
        try:
            registry.validate_config(type_key, config)
        except Exception as exc:  # noqa: BLE001
            await _fail(session, parent, actor_id, f"Step '{step_id}' config invalid: {exc}", org_id=org_id)
            await session.commit()
            return

        # Resolve impl.
        try:
            reg = registry.resolve(type_key)
        except KeyError:
            await _fail(session, parent, actor_id, f"Unknown step type: {type_key!r}", org_id=org_id)
            await session.commit()
            return
        step_impl = reg.impl

        # Freeze inputs at create time. Predecessors are succeeded, so their
        # outputs are final — this is what makes cross-process resolution work.
        try:
            inputs_template = step_def.get("inputs")
            if inputs_template:
                resolved = resolve_refs(inputs_template, step_outputs, run_params)
            else:
                # Unwired step (e.g. one built in the canvas, which has no input-
                # ref UI yet): forward the run params so an entry ingest step still
                # receives source_id etc. instead of KeyError-ing on empty inputs.
                resolved = dict(run_params)
        except ResolutionError as exc:
            await _fail(session, parent, actor_id, f"Step '{step_id}' input resolution: {exc}", org_id=org_id)
            await session.commit()
            return

        idem_key = step_impl.idempotency_key(config, resolved)

        # Idempotency reuse: a prior succeeded run with the same key short
        # -circuits execution — copy its outputs, no enqueue. A deterministic
        # step (e.g. export_yolo on the same commit) can have run to success
        # many times, so several rows share the key; their outputs are identical
        # by construction, so the most recent one is an equally valid source.
        idem_result = await session.execute(
            select(Run)
            .where(
                Run.step_type == type_key,
                Run.status == "succeeded",
                Run.config["_idem_key"].astext == idem_key,
            )
            .order_by(Run.finished_at.desc())
            .limit(1)
        )
        prior = idem_result.scalars().first()
        if prior is not None:
            child = Run(
                project_id=parent.project_id,
                kind="step",
                parent_run_id=parent_run_id,
                workflow_id=parent.workflow_id,
                step_id=step_id,
                step_type=type_key,
                status="succeeded",
                input_refs=resolved,
                output_refs=prior.output_refs or {},
                config={**config, "_idem_key": idem_key},
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            session.add(child)
            await session.flush()
            child_status[step_id] = "succeeded"
            step_outputs[step_id] = child.output_refs or {}
            await emit_event(
                session,
                actor_id=actor_id,
                actor_type="system",
                entity_type="run",
                entity_id=child.id,
                action="run.succeeded",
                org_id=org_id,
            )
            continue

        child = Run(
            project_id=parent.project_id,
            kind="step",
            parent_run_id=parent_run_id,
            workflow_id=parent.workflow_id,
            step_id=step_id,
            step_type=type_key,
            status="pending",
            input_refs=resolved,
            output_refs={},
            config={**config, "_idem_key": idem_key},
        )
        session.add(child)
        await session.flush()
        child_status[step_id] = "pending"
        to_enqueue.append((child.id, type_key, queue_for(step_impl)))

    # 6. Finalize if every step succeeded (idempotency reuse can complete the
    #    whole workflow without enqueuing anything).
    if steps_by_id and all(child_status.get(sid) == "succeeded" for sid in steps_by_id):
        parent.status = "succeeded"
        parent.finished_at = datetime.now(UTC)
        await session.flush()
        await emit_event(
            session,
            actor_id=actor_id,
            actor_type="system",
            entity_type="run",
            entity_id=parent_run_id,
            action="run.succeeded",
            org_id=org_id,
        )

    # Durable first, doorbell second: commit (releasing the parent lock) so the
    # rows exist, then ring each queue.
    await session.commit()
    for run_id, step_type, queue in to_enqueue:
        await enqueue_step(run_id, step_type, queue)


async def process_step(
    session: AsyncSession,
    step_run_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """Run a single already-claimed child step run.

    The caller (worker) has selected the row ``FOR UPDATE SKIP LOCKED`` with
    ``status='pending'`` in this same session, so we own it exclusively.
    """
    r = await session.execute(select(Run).where(Run.id == step_run_id))
    child = r.scalar_one_or_none()
    if child is None or child.status != "pending":
        return  # already taken / gone

    proj = await session.get(Project, child.project_id)
    org_id = proj.org_id if proj else None

    parent_run_id = child.parent_run_id
    type_key = child.step_type or ""
    config = child.config or {}
    inputs = child.input_refs or {}

    parent = None
    if parent_run_id is not None:
        pr = await session.execute(select(Run).where(Run.id == parent_run_id))
        parent = pr.scalar_one_or_none()

    # Resolve impl.
    try:
        reg = registry.resolve(type_key)
    except KeyError:
        if parent is not None:
            await _fail_child(session, child, parent, actor_id, f"Unknown step type: {type_key!r}", org_id=org_id)
        else:
            child.status = "failed"
            child.error = f"Unknown step type: {type_key!r}"
            child.finished_at = datetime.now(UTC)
        await session.commit()
        return
    step_impl = reg.impl

    # Mark running.
    child.status = "running"
    child.started_at = datetime.now(UTC)
    await session.flush()
    await emit_event(
        session,
        actor_id=actor_id,
        actor_type="system",
        entity_type="run",
        entity_id=child.id,
        action="run.started",
        org_id=org_id,
    )
    await session.commit()

    ctx = StepContext(
        session=session,
        storage=get_storage(),
        project_id=str(child.project_id),
        run_id=str(child.id),
        actor_id=str(actor_id),
        emit_event=lambda **kw: emit_event(session, **kw),
    )

    try:
        output = await step_impl.run(ctx, config, inputs)
    except GateException as gate:
        child.status = "waiting"
        child.output_refs = {"gate_data": gate.gate_data}
        child.finished_at = datetime.now(UTC)
        await session.flush()
        await emit_event(
            session,
            actor_id=actor_id,
            actor_type="system",
            entity_type="run",
            entity_id=child.id,
            action="run.waiting",
            org_id=org_id,
        )
        await session.commit()
        return  # gate has no advance; resume comes via the gate-resolve endpoint
    except Exception as exc:  # noqa: BLE001 — record failure, don't crash the worker
        # The step may have left the session dirty; drop it and write the
        # failure on a clean transaction so it can't be swallowed.
        await session.rollback()
        r2 = await session.execute(select(Run).where(Run.id == step_run_id))
        c2 = r2.scalar_one()
        p2 = None
        if parent_run_id is not None:
            pr2 = await session.execute(select(Run).where(Run.id == parent_run_id))
            p2 = pr2.scalar_one_or_none()
        if p2 is not None:
            await _fail_child(session, c2, p2, actor_id, str(exc), org_id=org_id)
        else:
            c2.status = "failed"
            c2.error = str(exc)
            c2.finished_at = datetime.now(UTC)
            await session.flush()
        await session.commit()
        return

    child.output_refs = output
    child.status = "succeeded"
    child.finished_at = datetime.now(UTC)
    await session.flush()
    await emit_event(
        session,
        actor_id=actor_id,
        actor_type="system",
        entity_type="run",
        entity_id=child.id,
        action="run.succeeded",
        org_id=org_id,
    )
    await session.commit()

    # Enqueue whatever became ready next / finalize the parent.
    if parent_run_id is not None:
        await advance_workflow(session, parent_run_id, actor_id)


# ── Internals (moved from executor.py) ──────────────────────────────────────


def _edge_endpoints(e: object) -> tuple[str, str]:
    """Normalise an edge to a (from, to) pair.

    Accepts every shape the codebase produces: dicts keyed ``from``/``to``
    (engine-native), dicts keyed ``source``/``target`` (the React-Flow builder
    in the frontend), and two-element ``[from, to]`` lists (the model's doc
    comment + DB tests). Keeping the engine tolerant means a workflow saved by
    the builder runs without a translation layer.
    """
    if isinstance(e, dict):
        src = e.get("from", e.get("source"))
        dst = e.get("to", e.get("target"))
    elif isinstance(e, (list, tuple)) and len(e) >= 2:
        src, dst = e[0], e[1]
    else:
        src = dst = None
    if src is None or dst is None:
        raise ValueError(f"Malformed workflow edge: {e!r}")
    return str(src), str(dst)


def _topo_sort(step_ids: list[str], edges: list[object]) -> list[str] | None:
    """Kahn's algorithm. Returns None if a cycle is detected."""
    in_degree: dict[str, int] = {s: 0 for s in step_ids}
    adj: dict[str, list[str]] = {s: [] for s in step_ids}
    for e in edges:
        src, dst = _edge_endpoints(e)
        adj.setdefault(src, []).append(dst)
        in_degree[dst] = in_degree.get(dst, 0) + 1

    queue: deque[str] = deque(s for s in step_ids if in_degree[s] == 0)
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for nb in adj.get(node, []):
            in_degree[nb] -= 1
            if in_degree[nb] == 0:
                queue.append(nb)

    return result if len(result) == len(step_ids) else None


async def _fail(
    session: AsyncSession,
    run: Run,
    actor_id: uuid.UUID,
    error: str,
    org_id: uuid.UUID | None = None,
) -> None:
    run.status = "failed"
    run.error = error
    run.finished_at = datetime.now(UTC)
    await session.flush()
    await emit_event(
        session,
        actor_id=actor_id,
        actor_type="system",
        entity_type="run",
        entity_id=run.id,
        action="run.failed",
        payload={"error": error},
        org_id=org_id,
    )


async def _fail_child(
    session: AsyncSession,
    child: Run,
    parent: Run,
    actor_id: uuid.UUID,
    error: str,
    org_id: uuid.UUID | None = None,
) -> None:
    child.status = "failed"
    child.error = error
    child.finished_at = datetime.now(UTC)
    await _fail(session, parent, actor_id, f"Step '{child.step_id}' failed: {error}", org_id=org_id)
