from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.ontologies import Ontology, LabelClass
from cvops_api.schemas.ontologies import (
    OntologyCreate,
    OntologyOut,
    OntologyUpdate,
    LabelClassCreate,
    LabelClassUpdate,
    LabelClassOut,
)

router = APIRouter()


async def _get_ontology(
    id: uuid.UUID,
    current_user: User,
    session: AsyncSession,
) -> Ontology:
    r = await session.execute(select(Ontology).where(Ontology.id == id))
    ontology = r.scalar_one_or_none()
    if ontology is None or ontology.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Ontology not found")
    if ontology.org_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Ontology not found")
    return ontology


@router.get("/ontologies", response_model=list[OntologyOut])
async def list_ontologies(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OntologyOut]:
    r = await session.execute(
        select(Ontology).where(
            Ontology.org_id == current_user.org_id,
            Ontology.deleted_at == None,  # noqa: E711
        )
    )
    return [OntologyOut.model_validate(o) for o in r.scalars().all()]


@router.post("/ontologies", response_model=OntologyOut, status_code=status.HTTP_201_CREATED)
async def create_ontology(
    body: OntologyCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OntologyOut:
    ontology = Ontology(org_id=current_user.org_id, name=body.name)
    session.add(ontology)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists")
    return OntologyOut.model_validate(ontology)


@router.get("/ontologies/{id}", response_model=OntologyOut)
async def get_ontology(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OntologyOut:
    ontology = await _get_ontology(id, current_user, session)
    return OntologyOut.model_validate(ontology)


@router.patch("/ontologies/{id}", response_model=OntologyOut)
async def update_ontology(
    id: uuid.UUID,
    body: OntologyUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OntologyOut:
    ontology = await _get_ontology(id, current_user, session)
    ontology.name = body.name
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already exists")
    return OntologyOut.model_validate(ontology)


@router.delete("/ontologies/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ontology(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    ontology = await _get_ontology(id, current_user, session)
    ontology.deleted_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ontologies/{id}/classes", response_model=list[LabelClassOut])
async def list_label_classes(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[LabelClassOut]:
    ontology = await _get_ontology(id, current_user, session)
    rows = await session.execute(
        select(LabelClass)
        .where(LabelClass.ontology_id == ontology.id)
        .order_by(LabelClass.sort_order)
    )
    return [LabelClassOut.model_validate(lc) for lc in rows.scalars().all()]


@router.post(
    "/ontologies/{id}/classes",
    response_model=LabelClassOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_label_class(
    id: uuid.UUID,
    body: LabelClassCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LabelClassOut:
    ontology = await _get_ontology(id, current_user, session)
    lc = LabelClass(
        ontology_id=ontology.id,
        class_key=body.class_key,
        display_name=body.display_name,
        color=body.color,
        sort_order=body.sort_order,
    )
    session.add(lc)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate class_key or sort_order",
        )
    return LabelClassOut.model_validate(lc)


@router.patch("/ontologies/{id}/classes/{class_id}", response_model=LabelClassOut)
async def update_label_class(
    id: uuid.UUID,
    class_id: uuid.UUID,
    body: LabelClassUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LabelClassOut:
    ontology = await _get_ontology(id, current_user, session)
    r2 = await session.execute(select(LabelClass).where(LabelClass.id == class_id))
    lc = r2.scalar_one_or_none()
    if lc is None or lc.ontology_id != ontology.id:
        raise HTTPException(status_code=404, detail="LabelClass not found")

    if body.display_name is not None:
        lc.display_name = body.display_name
    if body.color is not None:
        lc.color = body.color
    if body.sort_order is not None:
        lc.sort_order = body.sort_order

    await session.commit()
    return LabelClassOut.model_validate(lc)


@router.delete(
    "/ontologies/{id}/classes/{class_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_label_class(
    id: uuid.UUID,
    class_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    ontology = await _get_ontology(id, current_user, session)
    r2 = await session.execute(select(LabelClass).where(LabelClass.id == class_id))
    lc = r2.scalar_one_or_none()
    if lc is None or lc.ontology_id != ontology.id:
        raise HTTPException(status_code=404, detail="LabelClass not found")

    await session.delete(lc)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
