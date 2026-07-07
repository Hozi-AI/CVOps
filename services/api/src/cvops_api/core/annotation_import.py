"""Import pre-annotated images (YOLO boxes) as samples + annotation revisions.

The inverse of the ``export_yolo`` step: where export reads each sample's
latest revision and emits ``<class_id> cx cy w h`` lines, this lands uploaded
images as ``Sample`` rows each carrying one ``AnnotationRevision`` whose payload
mirrors what ``export_yolo`` expects (``class_key`` + ``geometry.coords``), so a
round-trip is byte-stable.

All class-mapping authority is server-side: the client sends an ordered
``class_names`` list plus per-box integer ``class_id``; we map
``class_id -> class_names[i] -> LabelClass.class_key`` against the resolved
ontology and reject (422) any id that is out of range or names a class absent
from the ontology — never silently dropping boxes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.audit import emit_event
from cvops_api.core.storage import StorageBackend
from cvops_api.db.models.annotations import AnnotationRevision
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.ontologies import LabelClass, Ontology
from cvops_api.db.models.projects import Project
from cvops_api.db.models.samples import DataSource, Sample
from cvops_api.schemas.data_sources import (
    AnnotatedImageConfirmRequest,
    AnnotatedImageConfirmResponse,
)


async def import_annotated_images(
    session: AsyncSession,
    *,
    project: Project,
    source: DataSource,
    body: AnnotatedImageConfirmRequest,
    actor_id: uuid.UUID,
) -> AnnotatedImageConfirmResponse:
    """Land each item as a Sample (+ one AnnotationRevision for new samples).

    Dedup: an image whose blob already has a sample in this project keeps its
    existing sample and does NOT get a second revision stacked on it.
    """
    ontology = await _resolve_ontology(session, project, body.ontology_id)
    class_keys = await _load_class_keys(session, ontology.id)
    # Validate the whole batch up-front so a bad reference fails (422) before
    # anything is persisted (the surrounding request commits only on success).
    _validate_class_mapping(body, class_keys)

    now_iso = datetime.now(UTC).isoformat()
    group = body.group or f"Upload {now_iso}"
    meta = {"group": group, "uploaded_at": now_iso}

    sample_ids: list[uuid.UUID] = []
    annotated = 0
    for item in body.items:
        await session.execute(
            pg_insert(Blob)
            .values(
                hash=item.blob_hash,
                storage_backend=settings.S3_BACKEND,
                storage_key=StorageBackend._bucket_key(item.blob_hash),
                size_bytes=item.size_bytes or 0,
                media_type=item.content_type or "image/jpeg",
            )
            .on_conflict_do_nothing(index_elements=["hash"])
        )
        res = await session.execute(
            pg_insert(Sample)
            .values(
                id=uuid.uuid4(),
                project_id=project.id,
                blob_hash=item.blob_hash,
                source_id=source.id,
                width=item.width,
                height=item.height,
                frame_index=None,
                thumbnail_hash=item.blob_hash,
                metadata_=meta,
            )
            .on_conflict_do_nothing(index_elements=["project_id", "blob_hash"])
            .returning(Sample.id)
        )
        row = res.first()
        if row is not None:
            # Newly created sample → attach the imported annotation as revision 1.
            sample_ids.append(row[0])
            session.add(
                AnnotationRevision(
                    project_id=project.id,
                    sample_id=row[0],
                    ontology_id=ontology.id,
                    ontology_version=ontology.version,
                    revision_no=1,
                    parent_revision_id=None,
                    payload=[
                        {
                            "class_key": body.class_names[b.class_id],
                            "geometry": {"coords": [b.cx, b.cy, b.w, b.h]},
                            "confidence": b.confidence,
                        }
                        for b in item.boxes
                    ],
                    provenance={"source": "import:yolo", "review_status": "unreviewed"},
                    created_by=actor_id,
                )
            )
            annotated += 1
        else:
            # Dedup: sample already exists for this blob — reuse it, no revision.
            existing = (
                await session.execute(
                    select(Sample.id).where(
                        Sample.project_id == project.id,
                        Sample.blob_hash == item.blob_hash,
                    )
                )
            ).first()
            if existing is not None:
                sample_ids.append(existing[0])

    if sample_ids:
        await emit_event(
            session,
            actor_id=str(actor_id),
            actor_type="user",
            entity_type="data_source",
            entity_id=source.id,
            action="images.uploaded_annotated",
            payload={"count": len(sample_ids), "annotated": annotated, "group": group},
            org_id=project.org_id,
        )
    await session.commit()

    return AnnotatedImageConfirmResponse(
        source_id=source.id,
        created=len(sample_ids),
        annotated=annotated,
        sample_ids=sample_ids,
    )


async def _resolve_ontology(
    session: AsyncSession, project: Project, ontology_id: uuid.UUID | None
) -> Ontology:
    if ontology_id is not None:
        ont = (
            await session.execute(
                select(Ontology).where(
                    Ontology.id == ontology_id,
                    Ontology.org_id == project.org_id,
                )
            )
        ).scalar_one_or_none()
        if ont is None:
            raise HTTPException(status_code=404, detail="Ontology not found")
        return ont
    if project.default_ontology_id is None:
        raise HTTPException(
            status_code=422,
            detail="Project has no default ontology; pass ontology_id explicitly.",
        )
    ont = (
        await session.execute(select(Ontology).where(Ontology.id == project.default_ontology_id))
    ).scalar_one_or_none()
    if ont is None:
        raise HTTPException(status_code=422, detail="Project default ontology not found.")
    return ont


async def _load_class_keys(session: AsyncSession, ontology_id: uuid.UUID) -> set[str]:
    rows = (
        (
            await session.execute(
                select(LabelClass.class_key).where(LabelClass.ontology_id == ontology_id)
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


def _validate_class_mapping(body: AnnotatedImageConfirmRequest, class_keys: set[str]) -> None:
    n = len(body.class_names)
    bad_ids: set[int] = set()
    unknown: set[str] = set()
    for item in body.items:
        for b in item.boxes:
            if b.class_id < 0 or b.class_id >= n:
                bad_ids.add(b.class_id)
                continue
            name = body.class_names[b.class_id]
            if name not in class_keys:
                unknown.add(name)
    if bad_ids:
        raise HTTPException(
            status_code=422,
            detail=f"class_id out of range for class_names (size {n}): {sorted(bad_ids)}",
        )
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown classes not in the target ontology: {sorted(unknown)}",
        )
