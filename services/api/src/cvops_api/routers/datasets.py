from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from datetime import datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, cast

from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage, public_s3_endpoint
from cvops_api.core.audit import emit_event
from cvops_api.core.pagination import decode_cursor_parts, decode_cursor_uuid
from cvops_api.core.sample_view import build_sample_outs
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import Sample
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.versioning import (
    Dataset,
    Commit,
    CommitSample,
    Ref,
    ProjectDatasetLink,
)
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.models import TrainingContainer
from cvops_api.engine.coordinator import advance_workflow
from cvops_api.engine.dispatch import create_adhoc_run
from cvops_api.schemas.datasets import (
    DatasetCreate,
    DatasetOut,
    CommitCreate,
    CommitOut,
    CommitFromSamples,
    CommitFromSamplesOut,
    RefCreate,
    RefOut,
    DatasetLinkCreate,
    DatasetLinkUpdate,
    DiffOut,
)
from cvops_api.schemas.samples import CursorPage, SampleOut
from cvops_api.schemas.runs import RunOut, TrainCommitRequest

router = APIRouter()


async def _check_project(
    project_id: uuid.UUID,
    current_user: User,
    session: AsyncSession,
) -> Project:
    r = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.org_id == current_user.org_id,
            Project.deleted_at == None,  # noqa: E711
        )
    )
    proj = r.scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


def _split_counts(total: int, strategy: dict[str, Any]) -> tuple[int, int]:
    """Return (train_count, val_count) for `total` samples given a split strategy.

    The ratios are validated up front on the request schema (see
    `schemas.datasets._validate_split_strategy`), so here we only apply the
    defaults and floor the counts; the remainder becomes the test split.
    """
    train_ratio = strategy.get("train_ratio", 0.7)
    val_ratio = strategy.get("val_ratio", 0.15)
    return int(total * train_ratio), int(total * val_ratio)


# ── Datasets ─────────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/datasets", response_model=list[DatasetOut])
async def list_datasets(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DatasetOut]:
    await _check_project(project_id, current_user, session)
    r = await session.execute(select(Dataset).where(Dataset.project_id == project_id))
    return [DatasetOut.model_validate(d) for d in r.scalars().all()]


@router.post(
    "/projects/{project_id}/datasets",
    response_model=DatasetOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset(
    project_id: uuid.UUID,
    body: DatasetCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DatasetOut:
    await _check_project(project_id, current_user, session)
    dataset = Dataset(project_id=project_id, name=body.name)
    session.add(dataset)
    await session.commit()
    return DatasetOut.model_validate(dataset)


@router.get("/datasets/{id}", response_model=DatasetOut)
async def get_dataset(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DatasetOut:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)
    return DatasetOut.model_validate(dataset)


# ── Review in CVAT ────────────────────────────────────────────────────────────


async def _latest_commit_id(session: AsyncSession, dataset_id: uuid.UUID) -> uuid.UUID | None:
    """The commit whose samples make up the review set.

    Prefer the `main` branch ref; fall back to the most recent commit by
    `created_at` (a dataset committed without a `main` branch still reviews).
    """
    r = await session.execute(
        select(Ref.target_commit_id).where(
            Ref.dataset_id == dataset_id,
            Ref.ref_type == "branch",
            Ref.name == "main",
        )
    )
    commit_id = r.scalar_one_or_none()
    if commit_id is not None:
        return commit_id

    r2 = await session.execute(
        select(Commit.id)
        .where(Commit.dataset_id == dataset_id)
        .order_by(Commit.created_at.desc())
        .limit(1)
    )
    return r2.scalar_one_or_none()


@router.post("/datasets/{id}/review")
async def review_dataset(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Dispatch a `human_review` run over the dataset's current committed samples.

    The review set is the latest commit's `commit_samples`; their annotation
    revisions ride along as CVAT pre-labels. Returns the parent run id so the UI
    can navigate to the run view, where the gate surfaces the "Open in CVAT" link.
    """
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    proj = await _check_project(dataset.project_id, current_user, session)

    commit_id = await _latest_commit_id(session, dataset.id)
    if commit_id is None:
        raise HTTPException(status_code=400, detail="Dataset has no commits to review")

    rows = (
        await session.execute(
            select(CommitSample.sample_id, CommitSample.annotation_revision_id).where(
                CommitSample.commit_id == commit_id
            )
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=400, detail="Dataset commit has no samples to review")

    # Pre-labels are optional: samples without a committed annotation revision
    # have annotation_revision_id = NULL. Drop those (don't stringify None into
    # the literal "None", which then fails the UUID cast in human_review). The
    # full working set rides on sample_ids; revisions are matched by their own
    # sample_id, so the lists need not be positionally aligned.
    params: dict[str, Any] = {
        "sample_ids": [str(sid) for sid, _ in rows],
        "annotation_revision_ids": [str(arid) for _, arid in rows if arid is not None],
    }

    # The commit step (below) pins the reviewed revisions into a new commit, so it
    # needs the project's ontology. Resolve it now (default, else latest) and fail
    # clearly if absent — human_review requires one too.
    ontology_id = proj.default_ontology_id
    if ontology_id is None:
        ontology_id = (
            await session.execute(
                select(Ontology.id)
                .where(Ontology.org_id == proj.org_id, Ontology.deleted_at.is_(None))
                .order_by(Ontology.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if ontology_id is None:
        raise HTTPException(
            status_code=400,
            detail="Project has no ontology — add label classes before reviewing",
        )

    # Ad-hoc DAG: human_review gate → commit_dataset. When the CVAT sync resolves
    # the gate (writing the reviewed revisions onto the review step's output_refs),
    # the coordinator enqueues commit_dataset, which freezes those revisions into a
    # new commit on `main` — so reviewed labels land in the dataset automatically.
    definition: dict[str, Any] = {
        "steps": [
            {
                "id": "review",
                "type": "step.human_review",
                "config": {},
                "inputs": {
                    "sample_ids": "$run.params.sample_ids",
                    "annotation_revision_ids": "$run.params.annotation_revision_ids",
                },
            },
            {
                "id": "commit",
                "type": "step.commit_dataset",
                "config": {
                    "dataset_name": dataset.name,
                    "ontology_id": str(ontology_id),
                    "branch_name": "main",
                    "message": "Committed from CVAT review",
                },
                "inputs": {
                    "sample_ids": "$run.params.sample_ids",
                    "annotation_revision_ids": "$steps.review.outputs.annotation_revision_ids",
                },
            },
        ],
        "edges": [{"from": "review", "to": "commit"}],
    }
    run = await create_adhoc_run(session, dataset.project_id, definition, params, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    return {"run_id": str(run.id)}


# ── Commits ───────────────────────────────────────────────────────────────────


@router.get(
    "/datasets/{id}/commits",
    response_model=CursorPage[CommitOut],
)
async def list_commits(
    id: uuid.UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[CommitOut]:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    # Commit ids are random UUIDs, so they are NOT chronological — order by
    # (created_at, id) DESC for true newest-first history. The cursor encodes
    # the last item's "<iso_created_at>|<uuid>" and the next page filters with a
    # tuple/row comparison so it stays stable across ties on created_at.
    q = select(Commit).where(Commit.dataset_id == id)
    if cursor is not None:
        cursor_created_at_raw, cursor_id_raw = decode_cursor_parts(cursor)
        try:
            cursor_created_at = datetime.fromisoformat(cursor_created_at_raw)
            cursor_id = uuid.UUID(cursor_id_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid pagination cursor") from exc
        q = q.where(tuple_(Commit.created_at, Commit.id) < (cursor_created_at, cursor_id))
    q = q.order_by(Commit.created_at.desc(), Commit.id.desc()).limit(limit + 1)

    result = await session.execute(q)
    items = list(result.scalars().all())

    next_cursor: str | None = None
    if len(items) == limit + 1:
        items = items[:limit]
        last = items[-1]
        next_cursor = base64.b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()

    return CursorPage(
        items=[CommitOut.model_validate(c) for c in items],
        next_cursor=next_cursor,
    )


@router.get("/datasets/{id}/commits/{commit_id}")
async def get_commit(
    id: uuid.UUID,
    commit_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    rc = await session.execute(
        select(Commit).where(Commit.id == commit_id, Commit.dataset_id == dataset.id)
    )
    commit = rc.scalar_one_or_none()
    if commit is None:
        raise HTTPException(status_code=404, detail="Commit not found")

    return Response(
        content=CommitOut.model_validate(commit).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "immutable, max-age=31536000"},
    )


@router.get(
    "/datasets/{id}/commits/{commit_id}/samples",
    response_model=CursorPage[SampleOut],
)
async def list_commit_samples(
    id: uuid.UUID,
    commit_id: uuid.UUID,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CursorPage[SampleOut]:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    q = (
        select(Sample)
        .join(CommitSample, CommitSample.sample_id == Sample.id)
        .where(CommitSample.commit_id == commit_id, Sample.deleted_at.is_(None))
    )
    if cursor is not None:
        cursor_uuid = decode_cursor_uuid(cursor)
        q = q.where(Sample.id > cursor_uuid)
    q = q.order_by(Sample.id).limit(limit + 1)

    rows = await session.execute(q)
    items = list(rows.scalars().all())
    next_cursor: str | None = None
    if len(items) == limit + 1:
        next_cursor = base64.b64encode(str(items[-1].id).encode()).decode()
        items = items[:limit]
    return CursorPage(items=await build_sample_outs(session, items), next_cursor=next_cursor)


@router.post(
    "/datasets/{id}/commits",
    response_model=CommitOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_commit(
    id: uuid.UUID,
    body: CommitCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CommitOut:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    # Resolve ontology version
    r_ont = await session.execute(select(Ontology.version).where(Ontology.id == body.ontology_id))
    ont_version = r_ont.scalar_one_or_none()
    if ont_version is None:
        raise HTTPException(status_code=404, detail="Ontology not found")

    # Find parent commit via branch ref
    r_ref = await session.execute(
        select(Ref).where(
            Ref.dataset_id == dataset.id,
            Ref.name == body.branch_name,
        )
    )
    ref = r_ref.scalar_one_or_none()
    parent_commit_id = ref.target_commit_id if ref is not None else None

    # Create commit
    commit = Commit(
        dataset_id=dataset.id,
        project_id=dataset.project_id,
        ontology_id=body.ontology_id,
        ontology_version=ont_version,
        message=body.message,
        stats={"sample_count": len(body.sample_ids)},
        parent_commit_id=parent_commit_id,
    )
    session.add(commit)
    await session.flush()

    # Assign splits
    total = len(body.sample_ids)
    train_count, val_count = _split_counts(total, body.split_strategy)

    for idx, (sid, arid) in enumerate(zip(body.sample_ids, body.annotation_revision_ids)):
        if idx < train_count:
            split = "train"
        elif idx < train_count + val_count:
            split = "val"
        else:
            split = "test"
        session.add(
            CommitSample(
                commit_id=commit.id,
                sample_id=sid,
                annotation_revision_id=arid,
                split=split,
            )
        )

    # Advance or create branch ref via CAS
    if ref:
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(Ref)
                .where(Ref.id == ref.id, Ref.target_commit_id == parent_commit_id)
                .values(target_commit_id=commit.id)
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="Concurrent branch update — retry")
    else:
        session.add(
            Ref(
                dataset_id=dataset.id,
                ref_type="branch",
                name=body.branch_name,
                target_commit_id=commit.id,
                is_mutable=True,
            )
        )

    await session.commit()
    return CommitOut.model_validate(commit)


@router.post(
    "/datasets/{id}/commits/from-samples",
    response_model=CommitFromSamplesOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_commit_from_samples(
    id: uuid.UUID,
    body: CommitFromSamples,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CommitFromSamplesOut:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    project = await _check_project(dataset.project_id, current_user, session)

    # Validate samples belong to project & not deleted. Cross-tenant ids → reject whole request.
    valid = list(
        (
            await session.execute(
                select(Sample.id).where(
                    Sample.id.in_(body.sample_ids),
                    Sample.project_id == dataset.project_id,
                    Sample.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    valid_set = set(valid)
    missing = [str(sid) for sid in body.sample_ids if sid not in valid_set]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"samples not in project: {', '.join(missing)}",
        )

    # Resolve winning (latest) revision per sample via DISTINCT ON.
    rows = await session.execute(
        select(
            AnnotationRevision.sample_id,
            AnnotationRevision.id,
            AnnotationRevision.ontology_id,
        )
        .where(AnnotationRevision.sample_id.in_(valid))
        .distinct(AnnotationRevision.sample_id)
        .order_by(AnnotationRevision.sample_id, AnnotationRevision.revision_no.desc())
    )
    winning: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    for sample_id, revision_id, ont_id in rows.all():
        winning[sample_id] = (revision_id, ont_id)

    # Commit ALL selected samples: annotated ones pin their winning revision,
    # raw (unannotated) images commit with a null revision — a dataset of raw
    # images is valid.
    committed = valid
    if not committed:
        raise HTTPException(status_code=422, detail="no samples to commit")

    # Optional ontology: (1) body; (2) shared across winning revisions; (3)
    # project default; else None (raw-image commits have no ontology).
    ontology_id = body.ontology_id
    if ontology_id is None:
        ont_ids = {winning[sid][1] for sid in committed if sid in winning}
        if len(ont_ids) == 1:
            ontology_id = next(iter(ont_ids))
        elif project.default_ontology_id is not None:
            ontology_id = project.default_ontology_id

    ont_version: int | None = None
    if ontology_id is not None:
        r_ont = await session.execute(select(Ontology.version).where(Ontology.id == ontology_id))
        ont_version = r_ont.scalar_one_or_none()
        if ont_version is None:
            raise HTTPException(status_code=404, detail="Ontology not found")

    # Find parent commit via branch ref
    r_ref = await session.execute(
        select(Ref).where(
            Ref.dataset_id == dataset.id,
            Ref.name == body.branch_name,
        )
    )
    ref = r_ref.scalar_one_or_none()
    parent_commit_id = ref.target_commit_id if ref is not None else None

    # Assign splits over the new batch.
    total = len(committed)
    train_count, val_count = _split_counts(total, body.split_strategy)

    # Cumulative state (a git "tree"): the new commit's commit_samples are the
    # parent commit's commit_samples UNION this batch. The batch wins on overlap
    # (fresh revision + freshly-assigned split); parent-only samples carry forward
    # unchanged; batch-only samples are added. Keyed by sample_id.
    merged: dict[uuid.UUID, tuple[uuid.UUID | None, str]] = {}

    # Carry forward the parent commit's pinned samples first.
    if parent_commit_id is not None:
        r_parent = await session.execute(
            select(
                CommitSample.sample_id,
                CommitSample.annotation_revision_id,
                CommitSample.split,
            ).where(CommitSample.commit_id == parent_commit_id)
        )
        for p_sid, p_rev, p_split in r_parent.all():
            merged[p_sid] = (p_rev, p_split)

    # The batch wins on overlap (and adds batch-only samples).
    # TODO(#141): removal semantics — a future "this commit deletes sample X"
    # would drop X from `merged` here instead of always unioning.
    for idx, sid in enumerate(committed):
        if idx < train_count:
            split = "train"
        elif idx < train_count + val_count:
            split = "val"
        else:
            split = "test"
        merged[sid] = (winning[sid][0] if sid in winning else None, split)

    # Recompute stats over the FULL cumulative set, not just the batch.
    by_split: dict[str, int] = {}
    cumulative_annotated = 0
    for rev_id, split in merged.values():
        by_split[split] = by_split.get(split, 0) + 1
        if rev_id is not None:
            cumulative_annotated += 1

    # Create commit
    commit = Commit(
        dataset_id=dataset.id,
        project_id=dataset.project_id,
        ontology_id=ontology_id,
        ontology_version=ont_version,
        message=body.message,
        stats={
            "sample_count": len(merged),
            "annotated": cumulative_annotated,
            "by_split": by_split,
        },
        parent_commit_id=parent_commit_id,
    )
    session.add(commit)
    await session.flush()

    for sid, (rev_id, split) in merged.items():
        session.add(
            CommitSample(
                commit_id=commit.id,
                sample_id=sid,
                annotation_revision_id=rev_id,
                split=split,
            )
        )

    # Advance or create branch ref via CAS
    if ref:
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(Ref)
                .where(Ref.id == ref.id, Ref.target_commit_id == parent_commit_id)
                .values(target_commit_id=commit.id)
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="Concurrent branch update — retry")
    else:
        session.add(
            Ref(
                dataset_id=dataset.id,
                ref_type="branch",
                name=body.branch_name,
                target_commit_id=commit.id,
                is_mutable=True,
            )
        )

    await emit_event(
        session,
        actor_id=str(current_user.id),
        actor_type="user",
        entity_type="commit",
        entity_id=commit.id,
        action="branch.advanced",
        payload={
            "dataset_id": str(dataset.id),
            "branch": body.branch_name,
            "from_samples": True,
        },
        org_id=current_user.org_id,
    )
    await session.commit()
    return CommitFromSamplesOut(
        commit_id=commit.id,
        committed_count=len(committed),
        skipped_count=0,
    )


@router.post(
    "/datasets/{id}/commits/{commit_id}/train",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def train_commit(
    id: uuid.UUID,
    commit_id: uuid.UUID,
    body: TrainCommitRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    """Ad-hoc "Train this commit": bake a git_url/entry_point/hyperparams trainer
    into an ephemeral export_yolo → train run scoped to this commit. No saved
    Workflow and no pre-registered TrainingContainer required; the DAG rides
    inline on the run's config and is advanced in-request.
    """
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    rc = await session.execute(
        select(Commit).where(Commit.id == commit_id, Commit.dataset_id == dataset.id)
    )
    commit = rc.scalar_one_or_none()
    if commit is None:
        raise HTTPException(status_code=404, detail="Commit not found")

    # Optional saved training environment: load it scoped to this project so a
    # container from another project (or org) is unreachable.
    if body.training_container_id is not None:
        rtc = await session.execute(
            select(TrainingContainer).where(
                TrainingContainer.id == body.training_container_id,
                TrainingContainer.project_id == dataset.project_id,
                TrainingContainer.deleted_at == None,  # noqa: E711
            )
        )
        if rtc.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Training container not found")

    train_config: dict[str, Any] = {
        "git_url": body.git_url,
        "entry_point": body.entry_point,
        "hyperparams": body.hyperparams or {},
    }
    if body.branch:
        train_config["branch"] = body.branch
    if body.training_container_id is not None:
        train_config["training_container_id"] = str(body.training_container_id)

    definition = {
        "steps": [
            {
                "id": "export",
                "type": "step.export_yolo",
                "config": {},
                "inputs": {"commit_id": str(commit_id)},
            },
            {
                "id": "train",
                "type": "step.train",
                "config": train_config,
                "inputs": {
                    "export_blob_hash": "$steps.export.outputs.export_blob_hash",
                    "commit_id": "$steps.export.outputs.commit_id",
                },
            },
        ],
        "edges": [{"from": "export", "to": "train"}],
    }

    run = await create_adhoc_run(session, dataset.project_id, definition, {}, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    await session.refresh(run)
    return RunOut.model_validate(run)


@router.post(
    "/datasets/{id}/commits/{commit_id}/export",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def export_commit(
    id: uuid.UUID,
    commit_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    """Dispatch a standalone export_yolo run for this commit (for download)."""
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    rc = await session.execute(
        select(Commit).where(Commit.id == commit_id, Commit.dataset_id == dataset.id)
    )
    if rc.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Commit not found")

    definition = {
        "steps": [
            {
                "id": "export",
                "type": "step.export_yolo",
                "config": {},
                "inputs": {"commit_id": str(commit_id)},
            }
        ],
        "edges": [],
    }
    run = await create_adhoc_run(session, dataset.project_id, definition, {}, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    await session.refresh(run)
    return RunOut.model_validate(run)


@router.get("/datasets/{id}/commits/{commit_id}/export-url")
async def commit_export_url(
    id: uuid.UUID,
    commit_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a presigned download URL for the most recent succeeded export of this commit."""
    from sqlalchemy import text as sa_text

    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    row = (
        await session.execute(
            sa_text(
                "SELECT output_refs->>'export_blob_hash' AS hash FROM runs "
                "WHERE step_type = 'step.export_yolo' "
                "AND input_refs->>'commit_id' = :cid "
                "AND status = 'succeeded' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"cid": str(commit_id)},
        )
    ).first()
    if row is None or not row[0]:
        raise HTTPException(status_code=404, detail="No export found for this commit — run an export first")

    endpoint = public_s3_endpoint(request.url.hostname)
    url = await get_storage().get_presigned_get(row[0], endpoint=endpoint)
    return {"url": url, "blob_hash": row[0]}


# ── Refs ──────────────────────────────────────────────────────────────────────


@router.get("/datasets/{id}/refs", response_model=list[RefOut])
async def list_refs(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[RefOut]:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    r_refs = await session.execute(select(Ref).where(Ref.dataset_id == id))
    return [RefOut.model_validate(ref) for ref in r_refs.scalars().all()]


@router.post(
    "/datasets/{id}/refs",
    response_model=RefOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_ref(
    id: uuid.UUID,
    body: RefCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RefOut:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    ref = Ref(
        dataset_id=dataset.id,
        ref_type=body.ref_type,
        name=body.name,
        target_commit_id=body.target_commit_id,
        is_mutable=body.is_mutable,
    )
    session.add(ref)
    await session.commit()
    return RefOut.model_validate(ref)


@router.delete(
    "/datasets/{id}/refs/{ref_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_ref(
    id: uuid.UUID,
    ref_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    r_ref = await session.execute(select(Ref).where(Ref.id == ref_id, Ref.dataset_id == id))
    ref = r_ref.scalar_one_or_none()
    if ref is None:
        raise HTTPException(status_code=404, detail="Ref not found")

    await session.delete(ref)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Diff ──────────────────────────────────────────────────────────────────────


@router.get("/datasets/{id}/diff", response_model=DiffOut)
async def diff_commits(
    id: uuid.UUID,
    from_commit: uuid.UUID = Query(..., alias="from"),
    to_commit: uuid.UUID = Query(..., alias="to"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DiffOut:
    r = await session.execute(select(Dataset).where(Dataset.id == id))
    dataset = r.scalar_one_or_none()
    if dataset is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    await _check_project(dataset.project_id, current_user, session)

    # Verify both commits belong to this dataset
    r_from = await session.execute(
        select(Commit).where(Commit.id == from_commit, Commit.dataset_id == id)
    )
    commit_from = r_from.scalar_one_or_none()
    if commit_from is None:
        raise HTTPException(status_code=404, detail="from_commit not found in dataset")

    r_to = await session.execute(
        select(Commit).where(Commit.id == to_commit, Commit.dataset_id == id)
    )
    commit_to = r_to.scalar_one_or_none()
    if commit_to is None:
        raise HTTPException(status_code=404, detail="to_commit not found in dataset")

    r_from_samples = await session.execute(
        select(CommitSample.sample_id).where(CommitSample.commit_id == from_commit)
    )
    from_samples = set(r_from_samples.scalars().all())

    r_to_samples = await session.execute(
        select(CommitSample.sample_id).where(CommitSample.commit_id == to_commit)
    )
    to_samples = set(r_to_samples.scalars().all())

    return DiffOut(
        added=list(to_samples - from_samples),
        removed=list(from_samples - to_samples),
        changed=[],
    )


# ── Dataset Links ─────────────────────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/dataset-links",
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset_link(
    project_id: uuid.UUID,
    body: DatasetLinkCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _check_project(project_id, current_user, session)

    # Exactly one of pinned_commit_id / ref_id must be non-null
    has_pin = body.pinned_commit_id is not None
    has_ref = body.ref_id is not None
    if has_pin == has_ref:
        raise HTTPException(
            status_code=422,
            detail="Exactly one of pinned_commit_id or ref_id must be provided",
        )

    link = ProjectDatasetLink(
        project_id=project_id,
        dataset_id=body.dataset_id,
        pinned_commit_id=body.pinned_commit_id,
        ref_id=body.ref_id,
    )
    session.add(link)
    await session.commit()
    return {
        "id": str(link.id),
        "project_id": str(link.project_id),
        "dataset_id": str(link.dataset_id),
        "pinned_commit_id": str(link.pinned_commit_id) if link.pinned_commit_id else None,
        "ref_id": str(link.ref_id) if link.ref_id else None,
    }


@router.patch("/dataset-links/{id}")
async def update_dataset_link(
    id: uuid.UUID,
    body: DatasetLinkUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    r = await session.execute(select(ProjectDatasetLink).where(ProjectDatasetLink.id == id))
    link = r.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Dataset link not found")
    await _check_project(link.project_id, current_user, session)

    if body.pinned_commit_id is not None:
        link.pinned_commit_id = body.pinned_commit_id
        link.ref_id = None
    if body.ref_id is not None:
        link.ref_id = body.ref_id
        link.pinned_commit_id = None

    await session.commit()
    return {
        "id": str(link.id),
        "project_id": str(link.project_id),
        "dataset_id": str(link.dataset_id),
        "pinned_commit_id": str(link.pinned_commit_id) if link.pinned_commit_id else None,
        "ref_id": str(link.ref_id) if link.ref_id else None,
    }


@router.delete(
    "/dataset-links/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_dataset_link(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    r = await session.execute(select(ProjectDatasetLink).where(ProjectDatasetLink.id == id))
    link = r.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Dataset link not found")
    await _check_project(link.project_id, current_user, session)

    await session.delete(link)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
