# Org-Level Ontologies Implementation Plan

**Status:** DONE

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift ontologies from project-scoped to org-scoped so one label set can be shared across multiple projects, and add a dedicated management page.

**Architecture:** Remove `project_id` FK from `ontologies` table and replace with `org_id` (derived from auth token). The router drops the `/projects/{id}/ontologies` prefix — all ontology endpoints become org-scoped. `Project.default_ontology_id` remains as the "which label set does this project use" pointer. A new top-level `/ontologies` frontend page replaces the buried `LabelClassesCard` in Project Settings.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, React 18, TypeScript, TanStack Query, Tailwind.

## Global Constraints

- All backend endpoints require `Authorization: Bearer <token>` (except `/auth/*`)
- Org isolation: every query filters on `current_user.org_id`
- Soft-delete: `deleted_at` column; list queries filter `WHERE deleted_at IS NULL`
- `LabelClass.sort_order` doubles as YOLO class_id — never reorder or reuse once a class has been used in an annotation
- No raw hex/rgb colors in frontend — use semantic tokens (`text-error`, `bg-iris-500`, etc.) from `index.css`
- All routes mounted under `/api/v1` prefix in `main.py`
- Frontend API base path: all `client.get/post/patch/delete` calls go to `/api/v1/...` via Vite proxy

---

### Task 1: ORM model + Alembic migration

**Files:**
- Modify: `services/api/src/cvops_api/db/models/ontologies.py`
- Create: `services/api/alembic/versions/0002_ontologies_org_scoped.py`
- Modify: `services/api/tests/db/conftest.py` (update `make_ontology`)
- Modify: `services/api/tests/db/test_ontologies.py`

**Interfaces:**
- Produces: `Ontology` ORM model with `org_id: Mapped[uuid.UUID]` instead of `project_id`
- Produces: `make_ontology(session, org_id=None)` factory (replaces `project_id` param)

- [ ] **Step 1: Update the ORM model**

Replace `project_id` with `org_id` in `services/api/src/cvops_api/db/models/ontologies.py`:

```python
"""
D3 — Ontology and LabelClass models.

An Ontology belongs to an Org and defines the controlled vocabulary of
label classes. A single ontology can be the default for multiple projects
in the same org.

A LabelClass is a single entry in that vocabulary. The sort_order column
doubles as the YOLO class_id at export time and must therefore never be
reused or reordered once a class has been used in an annotation.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cvops_api.db.base import Base, EntityBase


class Ontology(Base, EntityBase):
    __tablename__ = "ontologies"

    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orgs.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_ontologies_org_name"),)

    def __repr__(self) -> str:
        return (
            f"<Ontology id={self.id!r} org_id={self.org_id!r} "
            f"name={self.name!r} version={self.version!r}>"
        )


class LabelClass(Base, EntityBase):
    __tablename__ = "label_classes"

    ontology_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ontologies.id"),
        nullable=False,
        index=True,
    )
    class_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="#FF0000",
        server_default="#FF0000",
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("ontology_id", "class_key", name="uq_label_classes_ontology_key"),
        UniqueConstraint("ontology_id", "sort_order", name="uq_label_classes_ontology_order"),
    )

    def __repr__(self) -> str:
        return (
            f"<LabelClass id={self.id!r} ontology_id={self.ontology_id!r} "
            f"class_key={self.class_key!r} sort_order={self.sort_order!r}>"
        )
```

- [ ] **Step 2: Write the Alembic migration**

Create `services/api/alembic/versions/0002_ontologies_org_scoped.py`:

```python
"""ontologies: replace project_id with org_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-30 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add org_id as nullable first so we can back-fill before enforcing NOT NULL
    op.add_column('ontologies', sa.Column('org_id', sa.Uuid(), nullable=True))

    # 2. Back-fill from the project's org
    op.execute(
        "UPDATE ontologies o SET org_id = p.org_id "
        "FROM projects p WHERE p.id = o.project_id"
    )

    # 3. Enforce NOT NULL now that all rows are filled
    op.alter_column('ontologies', 'org_id', nullable=False)

    # 4. Add FK + index for org_id
    op.create_foreign_key('fk_ontologies_org_id', 'ontologies', 'orgs', ['org_id'], ['id'])
    op.create_index('ix_ontologies_org_id', 'ontologies', ['org_id'])

    # 5. Drop old project_id constraints and column
    op.drop_constraint('uq_ontologies_project_name', 'ontologies', type_='unique')
    op.drop_index('ix_ontologies_project_id', table_name='ontologies')
    op.drop_constraint('ontologies_project_id_fkey', 'ontologies', type_='foreignkey')
    op.drop_column('ontologies', 'project_id')

    # 6. Add new unique constraint
    op.create_unique_constraint('uq_ontologies_org_name', 'ontologies', ['org_id', 'name'])


def downgrade() -> None:
    op.add_column('ontologies', sa.Column('project_id', sa.Uuid(), nullable=True))
    op.drop_constraint('uq_ontologies_org_name', 'ontologies', type_='unique')
    op.drop_index('ix_ontologies_org_id', table_name='ontologies')
    op.drop_constraint('fk_ontologies_org_id', 'ontologies', type_='foreignkey')
    op.drop_column('ontologies', 'org_id')
    op.create_index('ix_ontologies_project_id', 'ontologies', ['project_id'])
    op.create_unique_constraint('uq_ontologies_project_name', 'ontologies', ['project_id', 'name'])
```

- [ ] **Step 3: Update `make_ontology` factory in `tests/db/conftest.py`**

Find the `make_ontology` function (around line 102) and replace:

```python
async def make_ontology(
    session: AsyncSession, org_id: uuid.UUID | None = None, **kwargs
) -> Ontology:
    if org_id is None:
        org_id = (await make_org(session)).id
    ont = Ontology(org_id=org_id, name=f"ont-{_uid()}", **kwargs)
    session.add(ont)
    await session.flush()
    return ont
```

Also update `make_commit` (around line 124) — it currently calls `make_ontology(session, project_id=project_id)`. Change to just `make_ontology(session, org_id=project.org_id)` — but since `make_commit` doesn't receive the project object, simplify: replace `make_ontology(session, project_id=project_id)` with `make_ontology(session)`.

```python
async def make_commit(
    session: AsyncSession,
    project_id: uuid.UUID | None = None,
    dataset_id: uuid.UUID | None = None,
    ontology_id: uuid.UUID | None = None,
    **kwargs,
) -> Commit:
    if project_id is None:
        project_id = (await make_project(session)).id
    if dataset_id is None:
        dataset_id = (await make_dataset(session, project_id=project_id)).id
    if ontology_id is None:
        ontology_id = (await make_ontology(session)).id
    commit = Commit(
        project_id=project_id,
        dataset_id=dataset_id,
        ontology_id=ontology_id,
        ontology_version=1,
        **kwargs,
    )
    session.add(commit)
    await session.flush()
    return commit
```

- [ ] **Step 4: Update `tests/db/test_ontologies.py`**

Replace the full file. The tests now use `org_id` instead of `project_id`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.db.models.ontologies import LabelClass, Ontology
from tests.db.conftest import make_ontology, make_org


async def test_ontology_create(session: AsyncSession):
    org = await make_org(session)
    ont = Ontology(org_id=org.id, name="base-ontology")
    session.add(ont)
    await session.flush()

    assert ont.id is not None
    assert ont.name == "base-ontology"
    assert ont.org_id == org.id


async def test_ontology_version_default(session: AsyncSession):
    org = await make_org(session)
    ont = Ontology(org_id=org.id, name="versioned-ontology")
    session.add(ont)
    await session.flush()
    await session.refresh(ont)

    assert ont.version == 1


async def test_ontology_unique_name_per_org(session: AsyncSession):
    org = await make_org(session)
    shared_name = f"shared-ont-{uuid.uuid4().hex[:8]}"

    session.add(Ontology(org_id=org.id, name=shared_name))
    await session.flush()

    session.add(Ontology(org_id=org.id, name=shared_name))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_ontology_same_name_different_orgs(session: AsyncSession):
    org_a = await make_org(session)
    org_b = await make_org(session)
    shared_name = f"shared-ont-{uuid.uuid4().hex[:8]}"

    ont_a = Ontology(org_id=org_a.id, name=shared_name)
    ont_b = Ontology(org_id=org_b.id, name=shared_name)
    session.add(ont_a)
    session.add(ont_b)
    await session.flush()

    assert ont_a.id != ont_b.id
    assert ont_a.name == ont_b.name


async def test_ontology_org_fk(session: AsyncSession):
    fake_org_id = uuid.uuid4()
    ont = Ontology(org_id=fake_org_id, name="orphan-ontology")
    session.add(ont)

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_create(session: AsyncSession):
    ont = await make_ontology(session)
    lc = LabelClass(
        ontology_id=ont.id,
        class_key="vehicle.car",
        display_name="Car",
        sort_order=0,
    )
    session.add(lc)
    await session.flush()

    assert lc.id is not None
    assert lc.class_key == "vehicle.car"
    assert lc.ontology_id == ont.id


async def test_label_class_color_default(session: AsyncSession):
    ont = await make_ontology(session)
    lc = LabelClass(
        ontology_id=ont.id,
        class_key="vehicle.truck",
        display_name="Truck",
        sort_order=0,
    )
    session.add(lc)
    await session.flush()
    await session.refresh(lc)

    assert lc.color == "#FF0000"


async def test_label_class_unique_class_key(session: AsyncSession):
    ont = await make_ontology(session)
    shared_key = "vehicle.car"

    session.add(LabelClass(ontology_id=ont.id, class_key=shared_key, display_name="Car", sort_order=0))
    await session.flush()

    session.add(LabelClass(ontology_id=ont.id, class_key=shared_key, display_name="Car Dup", sort_order=1))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_unique_sort_order(session: AsyncSession):
    ont = await make_ontology(session)

    session.add(LabelClass(ontology_id=ont.id, class_key="vehicle.car", display_name="Car", sort_order=0))
    await session.flush()

    session.add(LabelClass(ontology_id=ont.id, class_key="vehicle.truck", display_name="Truck", sort_order=0))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_label_class_different_ontologies_same_key(session: AsyncSession):
    ont_a = await make_ontology(session)
    ont_b = await make_ontology(session)
    shared_key = "vehicle.car"

    lc_a = LabelClass(ontology_id=ont_a.id, class_key=shared_key, display_name="Car", sort_order=0)
    lc_b = LabelClass(ontology_id=ont_b.id, class_key=shared_key, display_name="Car", sort_order=0)
    session.add(lc_a)
    session.add(lc_b)
    await session.flush()

    assert lc_a.id != lc_b.id
    assert lc_a.class_key == lc_b.class_key


async def test_label_class_sort_order_invariant(session: AsyncSession):
    ont = await make_ontology(session)

    lc0 = LabelClass(ontology_id=ont.id, class_key="person", display_name="Person", sort_order=0)
    lc1 = LabelClass(ontology_id=ont.id, class_key="vehicle.car", display_name="Car", sort_order=1)
    lc2 = LabelClass(ontology_id=ont.id, class_key="vehicle.truck", display_name="Truck", sort_order=2)
    session.add_all([lc0, lc1, lc2])
    await session.flush()

    result = await session.execute(
        select(LabelClass).where(LabelClass.ontology_id == ont.id).order_by(LabelClass.sort_order)
    )
    ordered = result.scalars().all()

    assert len(ordered) == 3
    assert ordered[0].class_key == "person"
    assert ordered[1].class_key == "vehicle.car"
    assert ordered[2].class_key == "vehicle.truck"


async def test_label_class_ontology_fk(session: AsyncSession):
    fake_ontology_id = uuid.uuid4()
    lc = LabelClass(
        ontology_id=fake_ontology_id,
        class_key="vehicle.car",
        display_name="Car",
        sort_order=0,
    )
    session.add(lc)

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
```

- [ ] **Step 5: Run DB-level tests**

```bash
cd services/api
pytest tests/db/test_ontologies.py -v
```

Expected: all tests PASS. If FK constraint name differs (the migration drops `ontologies_project_id_fkey`), check the actual name with `\d ontologies` in psql and fix the migration.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/cvops_api/db/models/ontologies.py \
        services/api/alembic/versions/0002_ontologies_org_scoped.py \
        services/api/tests/db/conftest.py \
        services/api/tests/db/test_ontologies.py
git commit -m "feat: lift ontologies from project-scope to org-scope in DB"
```

---

### Task 2: Backend router + schema

**Files:**
- Modify: `services/api/src/cvops_api/schemas/ontologies.py`
- Modify: `services/api/src/cvops_api/routers/ontologies.py`
- Modify: `services/api/tests/routers/test_ontologies.py`

**Interfaces:**
- Consumes: `Ontology` model with `org_id` (Task 1)
- Produces: `GET /ontologies`, `POST /ontologies`, `PATCH /ontologies/{id}`, `DELETE /ontologies/{id}`
- Produces: `OntologyOut` with `org_id` field instead of `project_id`

- [ ] **Step 1: Update schemas**

Replace `services/api/src/cvops_api/schemas/ontologies.py`:

```python
from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel


class OntologyCreate(BaseModel):
    name: str


class OntologyUpdate(BaseModel):
    name: str


class OntologyOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    version: int
    created_at: datetime


class LabelClassCreate(BaseModel):
    class_key: str
    display_name: str
    color: str = "#FF0000"
    sort_order: int


class LabelClassUpdate(BaseModel):
    display_name: str | None = None
    color: str | None = None
    sort_order: int | None = None


class LabelClassOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    ontology_id: uuid.UUID
    class_key: str
    display_name: str
    color: str
    sort_order: int
```

- [ ] **Step 2: Rewrite the router**

Replace `services/api/src/cvops_api/routers/ontologies.py`:

```python
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
```

- [ ] **Step 3: Rewrite the router tests**

Replace `services/api/tests/routers/test_ontologies.py`:

```python
"""Router tests for the ontologies router (org-scoped).

Endpoints tested: GET /ontologies, POST /ontologies, GET /ontologies/{id},
PATCH /ontologies/{id}, DELETE /ontologies/{id},
GET /ontologies/{id}/classes, POST /ontologies/{id}/classes,
PATCH /ontologies/{id}/classes/{class_id}, DELETE /ontologies/{id}/classes/{class_id}.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.ontologies import LabelClass, Ontology
from cvops_api.routers import ontologies


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def _client(factory, current_user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(ontologies.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Org]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        await s.commit()
        await s.refresh(user)
        await s.refresh(org)
        return user, org


# ── list ────────────────────────────────────────────────────────────────────


async def test_list_empty(factory) -> None:
    user, org = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200, res.text
    assert res.json() == []


async def test_list_excludes_soft_deleted(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="live")
        dead = Ontology(org_id=org.id, name="gone")
        s.add_all([ont, dead])
        await s.flush()
        from datetime import UTC, datetime
        dead.deleted_at = datetime.now(UTC)
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200
    names = [o["name"] for o in res.json()]
    assert "live" in names
    assert "gone" not in names


async def test_list_excludes_other_org(factory) -> None:
    user, org = await _seed(factory)
    other_user, other_org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=other_org.id, name="other-org-ont"))
        await s.commit()

    async with _client(factory, user) as c:
        res = await c.get("/ontologies")
    assert res.status_code == 200
    assert res.json() == []


# ── create ───────────────────────────────────────────────────────────────────


async def test_create_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post("/ontologies", json={"name": "detections"})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "detections"
    assert body["org_id"] == str(org.id)
    assert body["version"] == 1


async def test_create_ontology_duplicate_name_409(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=org.id, name="dup"))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.post("/ontologies", json={"name": "dup"})
    assert res.status_code == 409


# ── get ──────────────────────────────────────────────────────────────────────


async def test_get_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="get-test")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 200
    assert res.json()["name"] == "get-test"


async def test_get_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{uuid.uuid4()}")
    assert res.status_code == 404


async def test_get_ontology_cross_org_404(factory) -> None:
    user, _ = await _seed(factory)
    other_user, other_org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=other_org.id, name="private")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 404


# ── rename (PATCH) ───────────────────────────────────────────────────────────


async def test_rename_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="old-name")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.patch(f"/ontologies/{ont_id}", json={"name": "new-name"})
    assert res.status_code == 200
    assert res.json()["name"] == "new-name"


async def test_rename_ontology_duplicate_409(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        s.add(Ontology(org_id=org.id, name="taken"))
        ont = Ontology(org_id=org.id, name="mine")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.patch(f"/ontologies/{ont_id}", json={"name": "taken"})
    assert res.status_code == 409


# ── soft-delete ───────────────────────────────────────────────────────────────


async def test_delete_ontology(factory) -> None:
    user, org = await _seed(factory)
    async with factory() as s:
        ont = Ontology(org_id=org.id, name="to-delete")
        s.add(ont)
        await s.commit()
        ont_id = ont.id

    async with _client(factory, user) as c:
        res = await c.delete(f"/ontologies/{ont_id}")
    assert res.status_code == 204

    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}")
    assert res.status_code == 404


# ── label classes ─────────────────────────────────────────────────────────────


async def _make_ontology(factory, org_id) -> uuid.UUID:
    async with factory() as s:
        ont = Ontology(org_id=org_id, name=f"ont-{uuid.uuid4().hex[:6]}")
        s.add(ont)
        await s.commit()
        return ont.id


async def test_create_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{ont_id}/classes",
            json={"class_key": "car", "display_name": "Car", "color": "#FF0000", "sort_order": 0},
        )
    assert res.status_code == 201
    body = res.json()
    assert body["class_key"] == "car"
    assert body["ontology_id"] == str(ont_id)


async def test_create_label_class_duplicate_409(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        s.add(LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{ont_id}/classes",
            json={"class_key": "car", "display_name": "Car 2", "color": "#FF0000", "sort_order": 1},
        )
    assert res.status_code == 409


async def test_list_label_classes_ordered(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        s.add(LabelClass(ontology_id=ont_id, class_key="b", display_name="B", sort_order=1))
        s.add(LabelClass(ontology_id=ont_id, class_key="a", display_name="A", sort_order=0))
        await s.commit()
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}/classes")
    assert res.status_code == 200
    keys = [c["class_key"] for c in res.json()]
    assert keys == ["a", "b"]


async def test_update_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        lc = LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0)
        s.add(lc)
        await s.commit()
        lc_id = lc.id
    async with _client(factory, user) as c:
        res = await c.patch(
            f"/ontologies/{ont_id}/classes/{lc_id}",
            json={"display_name": "Automobile", "color": "#00FF00"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["display_name"] == "Automobile"
    assert body["color"] == "#00FF00"


async def test_delete_label_class(factory) -> None:
    user, org = await _seed(factory)
    ont_id = await _make_ontology(factory, org.id)
    async with factory() as s:
        lc = LabelClass(ontology_id=ont_id, class_key="car", display_name="Car", sort_order=0)
        s.add(lc)
        await s.commit()
        lc_id = lc.id
    async with _client(factory, user) as c:
        res = await c.delete(f"/ontologies/{ont_id}/classes/{lc_id}")
    assert res.status_code == 204
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{ont_id}/classes")
    assert res.json() == []


async def test_create_label_class_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.post(
            f"/ontologies/{uuid.uuid4()}/classes",
            json={"class_key": "car", "display_name": "Car", "color": "#FF0000", "sort_order": 0},
        )
    assert res.status_code == 404


async def test_list_classes_missing_ontology_404(factory) -> None:
    user, _ = await _seed(factory)
    async with _client(factory, user) as c:
        res = await c.get(f"/ontologies/{uuid.uuid4()}/classes")
    assert res.status_code == 404
```

- [ ] **Step 4: Run router tests**

```bash
cd services/api
pytest tests/routers/test_ontologies.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run full test suite to catch regressions**

```bash
cd services/api
pytest tests/ -q
```

Expected: all tests PASS. If any test in another file still creates `Ontology(project_id=...)`, fix those individually.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/cvops_api/schemas/ontologies.py \
        services/api/src/cvops_api/routers/ontologies.py \
        services/api/tests/routers/test_ontologies.py
git commit -m "feat: rewrite ontologies router as org-scoped with rename and soft-delete"
```

---

### Task 3: human_review step — update ontology lookup

**Files:**
- Modify: `packages/steps/src/cvops_steps/human_review.py` (lines ~134–144)

**Interfaces:**
- Consumes: `ontologies.org_id` (Task 1) — no more `ontologies.project_id`
- Produces: same behaviour — picks the project's default ontology, falls back to any org ontology

- [ ] **Step 1: Update the SQL query**

In `packages/steps/src/cvops_steps/human_review.py`, find the block starting at the comment `# ── Label space ──` (around line 128). Replace the query:

Old query:
```python
ont_id = (
    await session.execute(
        text(
            "SELECT o.id FROM ontologies o JOIN projects p ON p.id = o.project_id "
            "WHERE o.project_id = CAST(:pid AS uuid) AND o.deleted_at IS NULL "
            "ORDER BY (p.default_ontology_id = o.id) DESC NULLS LAST, o.version DESC "
            "LIMIT 1"
        ),
        {"pid": ctx.project_id},
    )
).scalar()
```

New query:
```python
ont_id = (
    await session.execute(
        text(
            "SELECT o.id FROM ontologies o "
            "JOIN projects p ON p.org_id = o.org_id "
            "WHERE p.id = CAST(:pid AS uuid) AND o.deleted_at IS NULL "
            "ORDER BY (p.default_ontology_id = o.id) DESC NULLS LAST, o.version DESC "
            "LIMIT 1"
        ),
        {"pid": ctx.project_id},
    )
).scalar()
```

- [ ] **Step 2: Run step tests**

```bash
cd services/api
pytest tests/ -k "human_review" -v
```

If no human_review tests exist, verify no import errors:

```bash
cd services/api
python -c "from cvops_steps.human_review import HumanReviewStep; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add packages/steps/src/cvops_steps/human_review.py
git commit -m "fix: update human_review ontology lookup for org-scoped ontologies"
```

---

### Task 4: Frontend API hooks

**Files:**
- Modify: `services/frontend/src/api/ontologies.ts`
- Modify: `services/frontend/src/components/runs/RunParamsDialog.tsx`
- Modify: `services/frontend/src/components/workflow/StepConfigPanel.tsx`

**Interfaces:**
- Produces: `useOntologies()` — no params, fetches `GET /api/v1/ontologies`
- Produces: `useCreateOntology()` — no projectId param
- Produces: `useUpdateOntology(ontologyId)` — calls `PATCH /ontologies/{id}`
- Produces: `useDeleteOntology()` — calls `DELETE /ontologies/{id}`
- Produces: `useUpdateLabelClass(ontologyId)` — calls `PATCH /ontologies/{id}/classes/{classId}`

- [ ] **Step 1: Rewrite `api/ontologies.ts`**

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'

export interface Ontology {
  id: string
  org_id: string
  name: string
  version: number
  created_at: string
}

export interface LabelClass {
  id: string
  ontology_id: string
  class_key: string
  display_name: string
  color: string
  sort_order: number
}

export function useOntologies() {
  return useQuery<Ontology[]>({
    queryKey: ['ontologies'],
    queryFn: async () => {
      const { data } = await client.get<Ontology[]>('/ontologies')
      return data
    },
  })
}

export function useCreateOntology() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string }) => {
      const { data } = await client.post<Ontology>('/ontologies', body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useUpdateOntology(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: { name: string }) => {
      const { data } = await client.patch<Ontology>(`/ontologies/${ontologyId}`, body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useDeleteOntology() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (ontologyId: string) => {
      await client.delete(`/ontologies/${ontologyId}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ontologies'] }),
  })
}

export function useLabelClasses(ontologyId: string | undefined) {
  return useQuery<LabelClass[]>({
    queryKey: ['label-classes', ontologyId],
    queryFn: async () => {
      const { data } = await client.get<LabelClass[]>(`/ontologies/${ontologyId}/classes`)
      return data
    },
    enabled: !!ontologyId,
  })
}

export function useCreateLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: {
      class_key: string
      display_name: string
      color: string
      sort_order: number
    }) => {
      const { data } = await client.post<LabelClass>(`/ontologies/${ontologyId}/classes`, body)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}

export function useUpdateLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      classId,
      body,
    }: {
      classId: string
      body: { display_name?: string; color?: string; sort_order?: number }
    }) => {
      const { data } = await client.patch<LabelClass>(
        `/ontologies/${ontologyId}/classes/${classId}`,
        body,
      )
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}

export function useDeleteLabelClass(ontologyId: string | undefined) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (classId: string) => {
      await client.delete(`/ontologies/${ontologyId}/classes/${classId}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['label-classes', ontologyId] }),
  })
}
```

- [ ] **Step 2: Update `RunParamsDialog.tsx`**

`useOntologies` no longer takes a `projectId`. Change the import call from `useOntologies(projectId)` to `useOntologies()`. The `projectId` prop can stay for the `source_id` picker. Find:

```typescript
const { data: ontologies } = useOntologies(projectId)
```

Replace with:

```typescript
const { data: ontologies } = useOntologies()
```

- [ ] **Step 3: Update `StepConfigPanel.tsx`**

`useOntologies` no longer takes a `projectId`. In `FieldRow`, the hook call is:

```typescript
const { data: ontologies } = useOntologies(spec.widget === 'ontology-picker' ? projectId : undefined)
```

Replace with:

```typescript
const { data: ontologies } = useOntologies()
```

The `projectId` prop on `FieldRow` and `StepConfigPanel` can be removed since it is no longer needed. Remove `projectId` from both the `FieldRow` props interface and `StepConfigPanel` props interface, remove the prop from the `<FieldRow ... projectId={projectId} ...>` call, and remove the `projectId={workflow?.project_id}` prop from the `<StepConfigPanel>` call in `WorkflowBuilder.tsx`.

- [ ] **Step 4: Typecheck**

```bash
cd services/frontend
npm run typecheck
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/api/ontologies.ts \
        services/frontend/src/components/runs/RunParamsDialog.tsx \
        services/frontend/src/components/workflow/StepConfigPanel.tsx \
        services/frontend/src/pages/WorkflowBuilder.tsx
git commit -m "feat: update frontend ontology hooks for org-scoped API"
```

---

### Task 5: New Ontologies page + routing + nav

**Files:**
- Create: `services/frontend/src/pages/Ontologies.tsx`
- Modify: `services/frontend/src/App.tsx`
- Modify: `services/frontend/src/components/layout/Sidebar.tsx`

**Interfaces:**
- Consumes: `useOntologies`, `useCreateOntology`, `useUpdateOntology`, `useDeleteOntology`, `useLabelClasses`, `useCreateLabelClass`, `useUpdateLabelClass`, `useDeleteLabelClass` (Task 4)
- Produces: route `/ontologies` showing all org ontologies with full CRUD

- [ ] **Step 1: Create `pages/Ontologies.tsx`**

```typescript
import { useState, type FormEvent } from 'react'
import {
  useOntologies,
  useCreateOntology,
  useUpdateOntology,
  useDeleteOntology,
  useLabelClasses,
  useCreateLabelClass,
  useUpdateLabelClass,
  useDeleteLabelClass,
  type Ontology,
  type LabelClass,
} from '../api/ontologies'
import {
  Breadcrumbs,
  Button,
  Card,
  Dialog,
  EmptyState,
  Field,
  Input,
  SkeletonList,
  ErrorState,
} from '../components/ui'
import { toast } from '../store/toast'

// ── Label class list + add form for one ontology ─────────────────────────────

function LabelClassRow({
  lc,
  ontologyId,
}: {
  lc: LabelClass
  ontologyId: string
}) {
  const [editing, setEditing] = useState(false)
  const [displayName, setDisplayName] = useState(lc.display_name)
  const [color, setColor] = useState(lc.color)
  const updateClass = useUpdateLabelClass(ontologyId)
  const deleteClass = useDeleteLabelClass(ontologyId)

  async function handleSave(e: FormEvent) {
    e.preventDefault()
    await updateClass.mutateAsync({ classId: lc.id, body: { display_name: displayName, color } })
    setEditing(false)
    toast.success('Class updated')
  }

  return (
    <li className="flex items-center gap-3 rounded-lg border border-border px-3 py-2">
      {editing ? (
        <form onSubmit={handleSave} className="flex flex-1 items-center gap-2">
          <input
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            className="h-8 w-10 cursor-pointer rounded border border-border-strong"
          />
          <Input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="flex-1 text-sm"
          />
          <span className="text-xs text-text-muted font-mono">{lc.class_key}</span>
          <Button size="sm" type="submit" loading={updateClass.isPending}>
            Save
          </Button>
          <Button size="sm" variant="secondary" type="button" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        </form>
      ) : (
        <>
          <span
            className="h-4 w-4 flex-shrink-0 rounded-full border border-border-strong"
            style={{ backgroundColor: lc.color }}
            aria-hidden
          />
          <span className="text-sm text-text-primary">{lc.display_name}</span>
          <span className="text-xs text-text-muted font-mono">{lc.class_key}</span>
          <span className="text-xs text-text-muted">#{lc.sort_order}</span>
          <div className="ml-auto flex gap-1">
            <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
              Edit
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-error hover:text-error"
              onClick={() =>
                deleteClass.mutate(lc.id, { onSuccess: () => toast.success('Class deleted') })
              }
              loading={deleteClass.isPending}
            >
              Delete
            </Button>
          </div>
        </>
      )}
    </li>
  )
}

function AddClassForm({ ontologyId, nextSortOrder }: { ontologyId: string; nextSortOrder: number }) {
  const [key, setKey] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [color, setColor] = useState('#7B6CF6')
  const createClass = useCreateLabelClass(ontologyId)

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    if (!key.trim()) return
    await createClass.mutateAsync({
      class_key: key.trim(),
      display_name: displayName.trim() || key.trim(),
      color,
      sort_order: nextSortOrder,
    })
    setKey('')
    setDisplayName('')
    setColor('#7B6CF6')
    toast.success('Class added')
  }

  return (
    <form onSubmit={handleAdd} className="flex items-end gap-2 pt-2">
      <Field label="Class key" className="flex-1">
        <Input
          required
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="e.g. car"
        />
      </Field>
      <Field label="Display name" className="flex-1">
        <Input
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="e.g. Car"
        />
      </Field>
      <Field label="Color">
        <input
          type="color"
          value={color}
          onChange={(e) => setColor(e.target.value)}
          className="h-10 w-12 cursor-pointer rounded-lg border border-border-strong"
        />
      </Field>
      <Button type="submit" size="sm" loading={createClass.isPending} disabled={!key.trim()}>
        Add
      </Button>
    </form>
  )
}

function OntologyCard({ ont }: { ont: Ontology }) {
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(ont.name)
  const updateOntology = useUpdateOntology(ont.id)
  const deleteOntology = useDeleteOntology()
  const { data: classes, isLoading } = useLabelClasses(ont.id)

  const nextSortOrder = classes && classes.length > 0 ? Math.max(...classes.map((c) => c.sort_order)) + 1 : 0

  async function handleRename(e: FormEvent) {
    e.preventDefault()
    if (!newName.trim() || newName === ont.name) { setRenaming(false); return }
    await updateOntology.mutateAsync({ name: newName.trim() })
    setRenaming(false)
    toast.success('Label set renamed')
  }

  function handleDelete() {
    if (!window.confirm(`Delete label set "${ont.name}"? This cannot be undone.`)) return
    deleteOntology.mutate(ont.id, { onSuccess: () => toast.success('Label set deleted') })
  }

  return (
    <Card className="p-5 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          {renaming ? (
            <form onSubmit={handleRename} className="flex items-center gap-2">
              <Input
                autoFocus
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="text-base font-semibold"
              />
              <Button size="sm" type="submit" loading={updateOntology.isPending}>Save</Button>
              <Button size="sm" variant="secondary" type="button" onClick={() => { setRenaming(false); setNewName(ont.name) }}>Cancel</Button>
            </form>
          ) : (
            <div className="flex items-center gap-2">
              <h3 className="text-base font-semibold text-text-primary">{ont.name}</h3>
              <span className="text-xs text-text-muted bg-surface-3 rounded px-1.5 py-0.5">v{ont.version}</span>
            </div>
          )}
        </div>
        {!renaming && (
          <div className="flex gap-1 flex-shrink-0">
            <Button size="sm" variant="ghost" onClick={() => setRenaming(true)}>Rename</Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-error hover:text-error"
              onClick={handleDelete}
              loading={deleteOntology.isPending}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      {isLoading ? (
        <SkeletonList rows={2} />
      ) : classes && classes.length > 0 ? (
        <ul className="space-y-1.5">
          {classes.map((lc) => (
            <LabelClassRow key={lc.id} lc={lc} ontologyId={ont.id} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-text-muted">No classes yet — add the first one below.</p>
      )}

      <AddClassForm ontologyId={ont.id} nextSortOrder={nextSortOrder} />
    </Card>
  )
}

// ── Create ontology dialog ────────────────────────────────────────────────────

function CreateOntologyDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [name, setName] = useState('')
  const createOntology = useCreateOntology()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    await createOntology.mutateAsync({ name: name.trim() })
    toast.success('Label set created')
    setName('')
    onClose()
  }

  function handleClose() {
    setName('')
    onClose()
  }

  return (
    <Dialog open={open} onClose={handleClose} title="New label set">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Field label="Name">
          <Input
            autoFocus
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. detections-v1"
          />
        </Field>
        {createOntology.isError && (
          <p className="text-sm text-error">Name already exists — choose a different one.</p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" type="button" size="sm" onClick={handleClose}>
            Cancel
          </Button>
          <Button type="submit" size="sm" loading={createOntology.isPending} disabled={!name.trim()}>
            Create
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function Ontologies() {
  const { data: ontologies, isLoading, isError, refetch } = useOntologies()
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <div className="mx-auto max-w-3xl p-6">
      <Breadcrumbs items={[{ label: 'Label Sets' }]} />

      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-text-primary">Label Sets</h2>
          <p className="mt-0.5 text-sm text-text-muted">
            Org-wide label vocabularies — use one across any number of projects
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>+ New Label Set</Button>
      </div>

      {isLoading && <SkeletonList rows={3} />}
      {isError && <ErrorState onRetry={refetch} />}
      {!isLoading && !isError && ontologies?.length === 0 && (
        <EmptyState
          title="No label sets yet"
          description="Create your first label set to define what reviewers can annotate in CVAT."
          action={<Button onClick={() => setCreateOpen(true)}>+ New Label Set</Button>}
        />
      )}
      {ontologies && ontologies.length > 0 && (
        <div className="space-y-4">
          {ontologies.map((ont) => (
            <OntologyCard key={ont.id} ont={ont} />
          ))}
        </div>
      )}

      <CreateOntologyDialog open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  )
}
```

- [ ] **Step 2: Add route to `App.tsx`**

Import the new page and add a route. After the existing imports add:

```typescript
import Ontologies from './pages/Ontologies'
```

Inside the `<Route element={<RequireAuth>...}>` block, add:

```typescript
<Route path="/ontologies" element={<Ontologies />} />
```

Place it after the `/cvat-models` route, before the `/projects/:id` group.

- [ ] **Step 3: Add nav entry to `Sidebar.tsx`**

In `Sidebar.tsx`, inside the global `<nav>` block (alongside "All Projects" and "Deployed Models"), add:

```typescript
<NavLink to="/ontologies" className={navClass}>
  Label Sets
</NavLink>
```

Place it after the "Deployed Models" link.

- [ ] **Step 4: Typecheck**

```bash
cd services/frontend
npm run typecheck
```

Expected: no errors. Common issues: `Card`, `EmptyState`, `ErrorState`, `SkeletonList` — confirm they exist in `components/ui/index.ts`. If any are missing, check the actual export names and substitute.

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/pages/Ontologies.tsx \
        services/frontend/src/App.tsx \
        services/frontend/src/components/layout/Sidebar.tsx
git commit -m "feat: add Label Sets page with full ontology and class CRUD"
```

---

### Task 6: Update Project Settings

**Files:**
- Modify: `services/frontend/src/pages/ProjectSettings.tsx`

**Interfaces:**
- Consumes: `useOntologies()` (Task 4) — org-wide list, no projectId
- Consumes: existing `useUpdateProject(id)` — already accepts `default_ontology_id`

- [ ] **Step 1: Rewrite the ontology section in ProjectSettings**

Remove the entire `LabelClassesCard` component and its usage. Replace the `{id && <LabelClassesCard projectId={id} />}` line with a "Default label set" card that lets the user pick from org ontologies.

Remove these imports (no longer needed):
```typescript
import {
  useOntologies,
  useCreateOntology,
  useLabelClasses,
  useCreateLabelClass,
  useDeleteLabelClass,
} from '../api/ontologies'
```

Add this import instead:
```typescript
import { useOntologies } from '../api/ontologies'
```

Find and remove the `LabelClassesCard` function entirely.

Replace `{id && <LabelClassesCard projectId={id} />}` with:

```tsx
{/* Default label set */}
<div className="bg-surface-2 rounded-xl border border-border shadow-sm p-6 mb-6 space-y-4">
  <div>
    <h3 className="text-sm font-bold text-text-primary">Default label set</h3>
    <p className="text-xs text-text-secondary mt-1">
      The ontology used when committing datasets and running CVAT review tasks for this project.
      Manage label sets in <Link to="/ontologies" className="text-iris-400 hover:underline">Label Sets</Link>.
    </p>
  </div>
  <div>
    <label className="block text-sm font-medium text-text-primary mb-1">Label set</label>
    <select
      value={project?.default_ontology_id ?? ''}
      onChange={(e) =>
        updateProject.mutate({ default_ontology_id: e.target.value || null })
      }
      disabled={updateProject.isPending}
      className="w-full border border-border-strong rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-focus"
    >
      <option value="">None</option>
      {ontologies?.map((o) => (
        <option key={o.id} value={o.id}>
          {o.name} (v{o.version})
        </option>
      ))}
    </select>
    {updateProject.isSuccess && (
      <p className="text-xs text-success mt-1">✓ Saved</p>
    )}
  </div>
</div>
```

Add `useOntologies` call near the top of `ProjectSettings`:

```typescript
const { data: ontologies } = useOntologies()
```

The `updateProject` mutation already exists in the component. The `default_ontology_id` field needs to be added to the `useUpdateProject` payload type if it isn't already — check `services/frontend/src/api/projects.ts` and add `default_ontology_id?: string | null` to the update body interface if missing.

- [ ] **Step 2: Typecheck**

```bash
cd services/frontend
npm run typecheck
```

Expected: no errors.

- [ ] **Step 3: Run frontend lint**

```bash
cd services/frontend
npm run lint
```

Expected: no warnings.

- [ ] **Step 4: Commit**

```bash
git add services/frontend/src/pages/ProjectSettings.tsx
git commit -m "feat: replace LabelClassesCard with default label set picker in Project Settings"
```

---

## Self-Review

**Spec coverage:**
- ✅ Lift ontologies to org-scope: Task 1 (model + migration), Task 2 (router), Task 3 (step)
- ✅ Share one label set across projects: org-scoped ownership + `default_ontology_id` pointer
- ✅ View/create/edit/delete ontologies: Task 5 (Ontologies page)
- ✅ Add/edit/delete label classes: Task 5 (OntologyCard + LabelClassRow)
- ✅ Navigation entry point: Task 5 (Sidebar)
- ✅ Project settings wired to org ontologies: Task 6
- ✅ RunParamsDialog and StepConfigPanel updated: Task 4
- ✅ Tests for all backend changes: Tasks 1–2

**Placeholder scan:** None found.

**Type consistency:**
- `OntologyOut.org_id` used consistently in router tests and frontend
- `useOntologies()` (no params) used consistently in RunParamsDialog, StepConfigPanel, ProjectSettings, Ontologies page
- `useUpdateOntology(ont.id)` and `useDeleteOntology()` match hook signatures in api/ontologies.ts
