# Model Versioning, Artifact Gallery & Auto-Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow users to manually upload `.pt` files with names, descriptions, and dataset links; browse training artifact images; and trigger model-based auto-annotation on samples.

**Architecture:** Three independent subsystems share the `ModelVersion` DB record as a pivot: Part A adds metadata + manual upload API; Part B attaches a `model_artifacts` table for training run files; Part C implements `AutoLabelStep` using local YOLO inference on the `training` worker queue. All three can be shipped and tested independently.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Alembic / Pydantic — TypeScript / React 18 / TanStack Query — MinIO (presigned URLs) — ultralytics YOLO (auto-label only, on `worker-training`)

## Global Constraints

- All byte payloads go through MinIO presigned URLs — never stream through the API process.
- Presigned GET/PUT calls must pass `endpoint=public_s3_endpoint(request.url.hostname)` so URLs are browser-reachable.
- Multi-tenancy: always filter on `org_id` via a project membership check.
- List endpoints use cursor pagination — `WHERE id > cursor ORDER BY id LIMIT n+1`; cursor = base64-encoded UUID.
- `steps` package (`cvops_steps`) must only import from `cvops_api.core` / `cvops_api.engine`, never from `cvops_api.db.models` or routers. DB access in steps uses raw SQL via `ctx.session`.
- Heavy ML deps (`ultralytics`, `torch`) must be lazy-imported inside `run()` — the API env doesn't have them.
- No hardcoded hex colors in frontend — use semantic tokens from `index.css` / Tailwind config.
- Commit subject format: `<type>: <3–10 word title>` (lowercase type). Use `scripts/open-pr.sh` for PRs.

---

## Part A — Model Metadata & Manual Upload

### Task 1: Add `name` and `description` to `ModelVersion`

**Files:**
- Modify: `services/api/src/cvops_api/db/models/models.py`
- Create: `services/api/alembic/versions/0002_model_version_name_desc.py`
- Modify: `services/api/src/cvops_api/schemas/models.py`
- Test: `services/api/tests/db/test_models.py` (add assertions for new columns)

**Interfaces:**
- Produces: `ModelVersion.name: str | None`, `ModelVersion.description: str | None`; `ModelVersionOut.name`, `ModelVersionOut.description`; `ModelVersionCreate`; `ModelVersionPatch` — all consumed by Tasks 2 and 3.

- [ ] **Step 1: Add columns to ORM model**

In `services/api/src/cvops_api/db/models/models.py`, inside the `ModelVersion` class, add after `base_model`:

```python
name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
```

- [ ] **Step 2: Write the Alembic migration**

Create `services/api/alembic/versions/0002_model_version_name_desc.py`:

```python
"""add name and description to model_versions

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("model_versions", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_versions", "description")
    op.drop_column("model_versions", "name")
```

- [ ] **Step 3: Update schemas**

Replace the contents of `services/api/src/cvops_api/schemas/models.py`:

```python
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any
from pydantic import BaseModel


class ModelVersionOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    project_id: uuid.UUID
    blob_hash: str
    name: str | None = None
    description: str | None = None
    trained_on_commit_id: uuid.UUID | None = None
    base_model: str | None = None
    hyperparams: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    code_version: str | None = None
    mlflow_run_id: str | None = None
    created_at: datetime


class ModelVersionCreate(BaseModel):
    blob_hash: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    name: str | None = None
    description: str | None = None
    base_model: str | None = None
    trained_on_commit_id: uuid.UUID | None = None
    mlflow_run_id: str | None = None
    hyperparams: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None


class ModelVersionPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    mlflow_run_id: str | None = None


class ModelArtifactOut(BaseModel):
    model_config = {"from_attributes": True}
    id: uuid.UUID
    model_version_id: uuid.UUID
    blob_hash: str
    filename: str
    mime_type: str | None = None
    created_at: datetime
    url: str | None = None  # presigned GET, populated per-request
```

- [ ] **Step 4: Write test for new fields**

In `services/api/tests/db/test_models.py`, add:

```python
def test_model_version_name_description(db_session):
    """ModelVersion accepts and persists name/description."""
    mv = ModelVersion(
        project_id=uuid.uuid4(),
        blob_hash="a" * 64,
        trained_on_commit_id=None,
        name="yolov8-nano-v1",
        description="Trained on dataset commit abc",
    )
    db_session.add(mv)
    db_session.flush()
    assert mv.name == "yolov8-nano-v1"
    assert mv.description == "Trained on dataset commit abc"
```

- [ ] **Step 5: Run test — expect PASS (schema is derived from ORM, not migration)**

```bash
cd services/api
pytest tests/db/test_models.py -q
```

Expected: all tests pass (including new one). Tests use `Base.metadata.create_all` so migrations don't need to run here.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/cvops_api/db/models/models.py \
        services/api/alembic/versions/0002_model_version_name_desc.py \
        services/api/src/cvops_api/schemas/models.py \
        services/api/tests/db/test_models.py
git commit -m "feat: add name and description to model versions"
```

---

### Task 2: Manual model upload API (upload-url, POST, PATCH)

**Files:**
- Modify: `services/api/src/cvops_api/routers/models.py`
- Create: `services/api/tests/routers/test_models_upload.py`

**Interfaces:**
- Consumes: `ModelVersionCreate`, `ModelVersionPatch`, `ModelVersionOut` from Task 1; `Blob` ORM; `get_storage()`, `public_s3_endpoint`, `settings.S3_BACKEND`, `StorageBackend._bucket_key` from `cvops_api.core.storage`.
- Produces: `GET /projects/{project_id}/models/upload-url?blob_hash=X`, `POST /projects/{project_id}/models`, `PATCH /models/{id}` — consumed by Task 3 frontend.

- [ ] **Step 1: Write failing tests**

Create `services/api/tests/routers/test_models_upload.py`:

```python
"""Tests for manual model upload endpoints."""
from __future__ import annotations
import hashlib
import uuid
import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_get_upload_url_returns_presigned(client: AsyncClient, auth_headers, project):
    blob_hash = hashlib.sha256(b"fake-weights").hexdigest()
    resp = await client.get(
        f"/api/v1/projects/{project.id}/models/upload-url",
        params={"blob_hash": blob_hash},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "upload_url" in body
    assert blob_hash in body["upload_url"]


@pytest.mark.anyio
async def test_create_model_version_manual(client: AsyncClient, auth_headers, project, stored_blob):
    """POST /projects/{id}/models creates a ModelVersion with name/description."""
    resp = await client.post(
        f"/api/v1/projects/{project.id}/models",
        json={
            "blob_hash": stored_blob.hash,
            "size_bytes": 1024,
            "name": "yolov8-nano",
            "description": "Test upload",
            "base_model": "yolov8n",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "yolov8-nano"
    assert data["description"] == "Test upload"
    assert data["project_id"] == str(project.id)


@pytest.mark.anyio
async def test_patch_model_version(client: AsyncClient, auth_headers, model_version):
    resp = await client.patch(
        f"/api/v1/models/{model_version.id}",
        json={"name": "updated-name", "mlflow_run_id": "run-abc123"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "updated-name"
    assert data["mlflow_run_id"] == "run-abc123"


@pytest.mark.anyio
async def test_create_model_version_wrong_project(client: AsyncClient, auth_headers):
    resp = await client.post(
        f"/api/v1/projects/{uuid.uuid4()}/models",
        json={"blob_hash": "a" * 64, "size_bytes": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd services/api
pytest tests/routers/test_models_upload.py -q
```

Expected: `404 Not Found` (endpoints don't exist yet).

- [ ] **Step 3: Implement endpoints in `routers/models.py`**

Replace the full content of `services/api/src/cvops_api/routers/models.py`:

```python
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import StorageBackend, get_storage, public_s3_endpoint
from cvops_api.db.models.blobs import Blob
from cvops_api.db.models.models import ModelVersion
from cvops_api.db.models.projects import Project
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.schemas.models import ModelVersionCreate, ModelVersionOut, ModelVersionPatch

router = APIRouter()


async def _get_project(
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


async def _get_model_version(
    mv_id: uuid.UUID,
    current_user: User,
    session: AsyncSession,
) -> ModelVersion:
    r = await session.execute(select(ModelVersion).where(ModelVersion.id == mv_id))
    mv = r.scalar_one_or_none()
    if mv is None:
        raise HTTPException(status_code=404, detail="Not found")
    proj = await session.get(Project, mv.project_id)
    if proj is None or proj.org_id != current_user.org_id:
        raise HTTPException(status_code=404, detail="Not found")
    return mv


# ── Upload slot ───────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/models/upload-url")
async def get_model_upload_url(
    project_id: uuid.UUID,
    blob_hash: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    await _get_project(project_id, current_user, session)
    url = await get_storage().get_presigned_put(
        blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"upload_url": url}


# ── Create (manual upload confirm) ───────────────────────────────────────────

@router.post("/projects/{project_id}/models", response_model=ModelVersionOut, status_code=201)
async def create_model_version(
    project_id: uuid.UUID,
    body: ModelVersionCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    await _get_project(project_id, current_user, session)
    await session.execute(
        pg_insert(Blob)
        .values(
            hash=body.blob_hash,
            storage_backend=settings.S3_BACKEND,
            storage_key=StorageBackend._bucket_key(body.blob_hash),
            size_bytes=body.size_bytes,
            media_type=body.media_type,
        )
        .on_conflict_do_nothing(index_elements=["hash"])
    )
    mv = ModelVersion(
        project_id=project_id,
        blob_hash=body.blob_hash,
        trained_on_commit_id=body.trained_on_commit_id,
        name=body.name,
        description=body.description,
        base_model=body.base_model,
        mlflow_run_id=body.mlflow_run_id,
        hyperparams=body.hyperparams,
        metrics=body.metrics,
        created_by=current_user.id,
    )
    session.add(mv)
    await session.commit()
    await session.refresh(mv)
    return ModelVersionOut.model_validate(mv)


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/models", response_model=list[ModelVersionOut])
async def list_models(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ModelVersionOut]:
    await _get_project(project_id, current_user, session)
    r = await session.execute(select(ModelVersion).where(ModelVersion.project_id == project_id))
    return [ModelVersionOut.model_validate(mv) for mv in r.scalars().all()]


@router.get("/models/{id}", response_model=ModelVersionOut)
async def get_model(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    return ModelVersionOut.model_validate(await _get_model_version(id, current_user, session))


@router.get("/models/{id}/weights-url")
async def get_weights_url(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    mv = await _get_model_version(id, current_user, session)
    url = await get_storage().get_presigned_get(
        mv.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"url": url}


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/models/{id}", response_model=ModelVersionOut)
async def patch_model_version(
    id: uuid.UUID,
    body: ModelVersionPatch,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelVersionOut:
    mv = await _get_model_version(id, current_user, session)
    for key, val in body.model_dump(exclude_unset=True).items():
        setattr(mv, key, val)
    await session.commit()
    await session.refresh(mv)
    return ModelVersionOut.model_validate(mv)
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
cd services/api
pytest tests/routers/test_models_upload.py tests/routers/test_models.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/cvops_api/routers/models.py \
        services/api/tests/routers/test_models_upload.py
git commit -m "feat: add manual model upload and patch endpoints"
```

---

### Task 3: Frontend — upload form, metadata edit, dataset commit link

**Files:**
- Modify: `services/frontend/src/api/models.ts`
- Modify: `services/frontend/src/pages/Models.tsx`
- Modify: `services/frontend/src/pages/ModelDetail.tsx`

**Interfaces:**
- Consumes: `GET /projects/{id}/models/upload-url`, `POST /projects/{id}/models`, `PATCH /models/{id}` from Task 2; `ModelVersion.name`, `ModelVersion.description`, `ModelVersion.trained_on_commit_id` from Task 1.
- Produces: upload form, dataset link in detail view — consumed by user directly.

- [ ] **Step 1: Add mutations to `api/models.ts`**

Replace the full content of `services/frontend/src/api/models.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from '../lib/client'
import { PRESIGNED_URL_GC_MS, PRESIGNED_URL_STALE_MS } from '../lib/presign'

export interface ModelVersion {
  id: string
  project_id: string
  blob_hash: string
  name: string | null
  description: string | null
  trained_on_commit_id: string | null
  base_model: string | null
  hyperparams: Record<string, unknown> | null
  metrics: Record<string, unknown> | null
  code_version: string | null
  mlflow_run_id: string | null
  created_at: string
}

export interface ModelVersionCreate {
  blob_hash: string
  size_bytes: number
  media_type?: string
  name?: string
  description?: string
  base_model?: string
  trained_on_commit_id?: string
  mlflow_run_id?: string
}

export function useModels(projectId: string | undefined) {
  return useQuery<ModelVersion[]>({
    queryKey: ['models', projectId],
    queryFn: async () => {
      const { data } = await client.get<ModelVersion[]>(`/projects/${projectId}/models`)
      return data
    },
    enabled: !!projectId,
  })
}

export function useModel(id: string | undefined) {
  return useQuery<ModelVersion>({
    queryKey: ['model', id],
    queryFn: async () => {
      const { data } = await client.get<ModelVersion>(`/models/${id}`)
      return data
    },
    enabled: !!id,
  })
}

export function useWeightsUrl(id: string | undefined) {
  return useQuery<{ url: string }>({
    queryKey: ['weights-url', id],
    queryFn: async () => {
      const { data } = await client.get<{ url: string }>(`/models/${id}/weights-url`)
      return data
    },
    enabled: !!id,
    staleTime: PRESIGNED_URL_STALE_MS,
    gcTime: PRESIGNED_URL_GC_MS,
  })
}

async function sha256hex(file: File): Promise<string> {
  const buf = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buf)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

export function useUploadModel(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (params: {
      file: File
      name?: string
      description?: string
      baseModel?: string
      trainedOnCommitId?: string
      mlflowRunId?: string
    }) => {
      const blobHash = await sha256hex(params.file)

      // Get presigned PUT URL
      const { data: slot } = await client.get<{ upload_url: string }>(
        `/projects/${projectId}/models/upload-url`,
        { params: { blob_hash: blobHash } },
      )

      // Upload directly to MinIO
      await fetch(slot.upload_url, {
        method: 'PUT',
        body: params.file,
        headers: { 'Content-Type': 'application/octet-stream' },
      })

      // Register model version
      const { data } = await client.post<ModelVersion>(`/projects/${projectId}/models`, {
        blob_hash: blobHash,
        size_bytes: params.file.size,
        name: params.name,
        description: params.description,
        base_model: params.baseModel,
        trained_on_commit_id: params.trainedOnCommitId || undefined,
        mlflow_run_id: params.mlflowRunId || undefined,
      } satisfies ModelVersionCreate)
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', projectId] }),
  })
}

export function usePatchModel(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (patch: { name?: string; description?: string; mlflow_run_id?: string }) => {
      const { data } = await client.patch<ModelVersion>(`/models/${id}`, patch)
      return data
    },
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['model', id] })
      qc.invalidateQueries({ queryKey: ['models', data.project_id] })
    },
  })
}
```

- [ ] **Step 2: Add upload form to `pages/Models.tsx`**

Replace the full content of `services/frontend/src/pages/Models.tsx`:

```typescript
import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModels, useUploadModel } from '../api/models'
import { toast } from '../store/toast'
import { Breadcrumbs, Button, Card, EmptyState, ErrorState, Field, Input, Label, SkeletonList } from '../components/ui'
import { formatValue } from '../lib/format'

export default function Models() {
  const { id: projectId } = useParams<{ id: string }>()
  const { data: models, isLoading, isError, refetch } = useModels(projectId)
  const upload = useUploadModel(projectId!)

  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [baseModel, setBaseModel] = useState('')
  const [commitId, setCommitId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault()
    if (!file || !projectId) return
    const toastId = toast.info(`Uploading "${name || file.name}"…`, 'Computing hash and uploading', 0)
    try {
      await upload.mutateAsync({ file, name, description, baseModel, trainedOnCommitId: commitId })
      toast.dismiss(toastId)
      toast.success('Model version uploaded')
      setShowForm(false)
      setName(''); setDescription(''); setBaseModel(''); setCommitId(''); setFile(null)
      if (fileRef.current) fileRef.current.value = ''
    } catch {
      toast.dismiss(toastId)
      toast.error('Upload failed')
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <Breadcrumbs
        items={[{ label: 'Project', to: `/projects/${projectId}` }, { label: 'Models' }]}
      />

      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-xl font-bold text-text-primary">Models</h2>
        <Button onClick={() => setShowForm((v) => !v)}>+ Upload Model</Button>
      </div>

      {showForm && (
        <Card className="mb-6 p-5">
          <form onSubmit={handleUpload} className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. yolov8n-v2" />
              </Field>
              <Field label="Base model">
                <Input value={baseModel} onChange={(e) => setBaseModel(e.target.value)} placeholder="e.g. yolov8n" />
              </Field>
            </div>
            <Field label="Description">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What changed, what it was trained on…" />
            </Field>
            <Field label="Dataset commit ID (optional)">
              <Input
                value={commitId}
                onChange={(e) => setCommitId(e.target.value)}
                placeholder="Paste commit UUID"
                className="font-mono text-xs"
              />
            </Field>
            <div>
              <Label>Weights file (.pt)</Label>
              <input
                required
                ref={fileRef}
                type="file"
                accept=".pt"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="w-full text-sm text-text-secondary file:mr-3 file:rounded-lg file:border-0 file:bg-iris/10 file:px-3 file:py-2 file:text-sm file:font-medium file:text-iris-400 hover:file:bg-iris/20"
              />
            </div>
            <div className="flex items-center gap-2">
              <Button type="submit" loading={upload.isPending} disabled={!file}>
                {upload.isPending ? 'Uploading…' : 'Upload'}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setShowForm(false)} disabled={upload.isPending}>
                Cancel
              </Button>
              {upload.isPending && (
                <span className="text-xs text-text-muted">Hashing file and uploading to storage…</span>
              )}
            </div>
          </form>
        </Card>
      )}

      {isLoading && <SkeletonList rows={3} />}
      {isError && <ErrorState description="Could not load models for this project." onRetry={() => refetch()} />}
      {models?.length === 0 && (
        <EmptyState title="No models yet" description='Upload a .pt file or run a training workflow.' />
      )}

      {models && models.length > 0 && (
        <div className="space-y-2">
          {models.map((m) => (
            <Link key={m.id} to={`/models/${m.id}`}>
              <Card className="flex items-center justify-between px-5 py-4 transition-all hover:border-iris hover:shadow-md">
                <div>
                  <p className="font-semibold text-text-primary">{m.name ?? <span className="font-mono text-sm">{m.id.slice(0, 8)}…</span>}</p>
                  <p className="mt-0.5 text-xs text-text-muted">
                    {m.base_model ?? 'Unknown base'} · {new Date(m.created_at).toLocaleDateString()}
                  </p>
                  {m.description && <p className="mt-1 text-xs text-text-secondary">{m.description}</p>}
                </div>
                {m.metrics && (
                  <div className="text-right">
                    {Object.entries(m.metrics)
                      .filter(([, v]) => v !== null && typeof v !== 'object')
                      .slice(0, 2)
                      .map(([k, v]) => (
                        <p key={k} className="text-xs text-text-muted">
                          {k}: <span className="font-medium text-text-secondary">{formatValue(v)}</span>
                        </p>
                      ))}
                  </div>
                )}
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 3: Update `pages/ModelDetail.tsx` — dataset link + MLflow edit**

Replace the full content of `services/frontend/src/pages/ModelDetail.tsx`:

```typescript
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useModel, usePatchModel, useWeightsUrl } from '../api/models'
import { usePinProject } from '../lib/useActiveProject'
import { Breadcrumbs, Button, Card, ErrorState, Field, Input, SkeletonList } from '../components/ui'
import { mlflowRunUrl } from '../lib/mlflow'
import { formatValue } from '../lib/format'
import { toast } from '../store/toast'

export default function ModelDetail() {
  const { id } = useParams<{ id: string }>()
  const { data: model, isLoading, isError, refetch } = useModel(id)
  const { data: weightsUrl } = useWeightsUrl(id)
  const patch = usePatchModel(id!)
  usePinProject(model?.project_id)

  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editMlflow, setEditMlflow] = useState('')

  function startEdit() {
    setEditName(model?.name ?? '')
    setEditDesc(model?.description ?? '')
    setEditMlflow(model?.mlflow_run_id ?? '')
    setEditing(true)
  }

  async function saveEdit() {
    try {
      await patch.mutateAsync({ name: editName, description: editDesc, mlflow_run_id: editMlflow || undefined })
      setEditing(false)
      toast.success('Model updated')
    } catch {
      toast.error('Update failed')
    }
  }

  if (isLoading) {
    return <div className="mx-auto max-w-3xl p-6"><SkeletonList rows={3} /></div>
  }
  if (isError || !model) {
    return <div className="mx-auto max-w-3xl p-6"><ErrorState description="Could not load this model." onRetry={() => refetch()} /></div>
  }

  const mlflowUrl = model.mlflow_run_id
    ? mlflowRunUrl(model.mlflow_run_id, (model.metrics?.mlflow_experiment_id as string | undefined) ?? '0')
    : null
  const displayMetrics = model.metrics
    ? Object.entries(model.metrics).filter(([k]) => !k.startsWith('mlflow_'))
    : []

  return (
    <div className="mx-auto max-w-3xl p-6">
      <Breadcrumbs
        items={[
          { label: 'Models', to: `/projects/${model.project_id}/models` },
          { label: model.name ?? id?.slice(0, 8) ?? '', mono: !model.name },
        ]}
      />

      <Card className="mb-4 p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-text-primary">
            {model.name ?? <span className="font-mono text-sm">{id?.slice(0, 8)}</span>}
          </h2>
          <div className="flex gap-2">
            {!editing && <Button variant="secondary" onClick={startEdit}>Edit</Button>}
            {weightsUrl && (
              <a
                href={weightsUrl.url}
                className="rounded-lg bg-iris px-3 py-1.5 text-xs text-text-onAccent transition-colors hover:bg-iris-hover"
              >
                Download weights
              </a>
            )}
          </div>
        </div>

        {editing ? (
          <div className="flex flex-col gap-3">
            <Field label="Name">
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
            </Field>
            <Field label="Description">
              <Input value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />
            </Field>
            <Field label="MLflow run ID">
              <Input value={editMlflow} onChange={(e) => setEditMlflow(e.target.value)} className="font-mono text-xs" />
            </Field>
            <div className="flex gap-2">
              <Button onClick={saveEdit} loading={patch.isPending}>Save</Button>
              <Button variant="secondary" onClick={() => setEditing(false)} disabled={patch.isPending}>Cancel</Button>
            </div>
          </div>
        ) : (
          <>
            {model.description && <p className="mb-4 text-sm text-text-secondary">{model.description}</p>}
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <div>
                <dt className="text-xs text-text-muted">Base model</dt>
                <dd className="mt-0.5 font-medium text-text-primary">{model.base_model ?? '—'}</dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Created</dt>
                <dd className="mt-0.5 font-medium text-text-primary">{new Date(model.created_at).toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs text-text-muted">Trained on commit</dt>
                <dd className="mt-0.5 font-mono text-xs font-medium text-text-primary">
                  {model.trained_on_commit_id ? (
                    <Link
                      to={`/projects/${model.project_id}/commits/${model.trained_on_commit_id}`}
                      className="text-iris-400 hover:opacity-80"
                    >
                      {model.trained_on_commit_id.slice(0, 8)} ↗
                    </Link>
                  ) : '—'}
                </dd>
              </div>
              {model.mlflow_run_id && (
                <div>
                  <dt className="text-xs text-text-muted">MLflow run</dt>
                  <dd className="mt-0.5 font-mono text-xs font-medium">
                    {mlflowUrl ? (
                      <a href={mlflowUrl} target="_blank" rel="noreferrer" className="text-iris-400 hover:opacity-80">
                        {model.mlflow_run_id.slice(0, 12)} ↗
                      </a>
                    ) : (
                      <span className="text-text-secondary">{model.mlflow_run_id.slice(0, 12)}</span>
                    )}
                  </dd>
                </div>
              )}
            </dl>
          </>
        )}
      </Card>

      {displayMetrics.length > 0 && (
        <Card className="mb-4 p-6">
          <h3 className="mb-3 text-sm font-bold text-text-secondary">Metrics</h3>
          <div className="grid grid-cols-3 gap-3">
            {displayMetrics.map(([k, v]) => (
              <div key={k} className="rounded-lg bg-surface-3 px-3 py-2">
                <p className="text-xs capitalize text-text-muted">{k.replace(/_/g, ' ')}</p>
                <p className="break-words text-lg font-bold text-text-primary">{formatValue(v)}</p>
              </div>
            ))}
          </div>
        </Card>
      )}

      {model.hyperparams && Object.keys(model.hyperparams).length > 0 && (
        <Card className="p-6">
          <h3 className="mb-3 text-sm font-bold text-text-secondary">Hyperparameters</h3>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
            {Object.entries(model.hyperparams).map(([k, v]) => (
              <div key={k}>
                <dt className="text-xs capitalize text-text-muted">{k.replace(/_/g, ' ')}</dt>
                <dd className="mt-0.5 break-words font-medium text-text-primary">{formatValue(v)}</dd>
              </div>
            ))}
          </dl>
        </Card>
      )}
    </div>
  )
}
```

- [ ] **Step 4: Type-check**

```bash
cd services/frontend
npm run typecheck
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add services/frontend/src/api/models.ts \
        services/frontend/src/pages/Models.tsx \
        services/frontend/src/pages/ModelDetail.tsx
git commit -m "feat: add model upload form and metadata edit UI"
```

---

## Part B — Training Artifact Gallery

### Task 4: `ModelArtifact` table + API endpoints

**Files:**
- Modify: `services/api/src/cvops_api/db/models/models.py`
- Create: `services/api/alembic/versions/0003_model_artifacts.py`
- Modify: `services/api/src/cvops_api/routers/models.py`
- Create: `services/api/tests/routers/test_model_artifacts.py`

**Interfaces:**
- Consumes: `ModelArtifactOut` from Task 1 schemas; `_get_model_version` helper in `models.py`.
- Produces: `GET /models/{id}/artifacts/upload-url`, `POST /models/{id}/artifacts`, `GET /models/{id}/artifacts` — consumed by Task 5 frontend.

- [ ] **Step 1: Add `ModelArtifact` ORM class**

In `services/api/src/cvops_api/db/models/models.py`, add at the bottom (after `ModelVersion`):

```python
class ModelArtifact(Base, EntityBase):
    """
    A file artifact (training plot, CSV, weight snapshot) attached to a
    model version. Stored as a content-addressed blob; filename is user-supplied.
    """

    __tablename__ = "model_artifacts"

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id"), nullable=False, index=True
    )
    blob_hash: Mapped[str] = mapped_column(ForeignKey("blobs.hash"), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ModelArtifact id={self.id!r} model_version_id={self.model_version_id!r} "
            f"filename={self.filename!r}>"
        )
```

Also add `from cvops_api.db.models.models import ModelArtifact` wherever the model is imported (the import block in models.py's `__init__` or the all-models import file — check `services/api/src/cvops_api/db/models/__init__.py` and add `ModelArtifact` to the exports there).

- [ ] **Step 2: Write Alembic migration**

Create `services/api/alembic/versions/0003_model_artifacts.py`:

```python
"""add model_artifacts table

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id"), nullable=False),
        sa.Column("blob_hash", sa.Text(), sa.ForeignKey("blobs.hash"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
    )
    op.create_index("ix_model_artifacts_model_version_id", "model_artifacts", ["model_version_id"])


def downgrade() -> None:
    op.drop_index("ix_model_artifacts_model_version_id")
    op.drop_table("model_artifacts")
```

- [ ] **Step 3: Write failing tests**

Create `services/api/tests/routers/test_model_artifacts.py`:

```python
"""Tests for model artifact upload and listing."""
from __future__ import annotations
import hashlib
import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_get_artifact_upload_url(client: AsyncClient, auth_headers, model_version):
    blob_hash = hashlib.sha256(b"plot-image").hexdigest()
    resp = await client.get(
        f"/api/v1/models/{model_version.id}/artifacts/upload-url",
        params={"blob_hash": blob_hash, "filename": "results.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert "upload_url" in resp.json()


@pytest.mark.anyio
async def test_create_and_list_artifacts(client: AsyncClient, auth_headers, model_version, stored_blob):
    resp = await client.post(
        f"/api/v1/models/{model_version.id}/artifacts",
        json={
            "blob_hash": stored_blob.hash,
            "filename": "results.png",
            "mime_type": "image/png",
            "size_bytes": 2048,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    artifact = resp.json()
    assert artifact["filename"] == "results.png"
    assert artifact["mime_type"] == "image/png"
    assert "url" in artifact

    # List
    resp2 = await client.get(
        f"/api/v1/models/{model_version.id}/artifacts",
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    items = resp2.json()
    assert any(a["id"] == artifact["id"] for a in items)
```

- [ ] **Step 4: Run tests — expect FAIL**

```bash
cd services/api
pytest tests/routers/test_model_artifacts.py -q
```

Expected: `404 Not Found` (endpoints don't exist yet).

- [ ] **Step 5: Add artifact endpoints to `routers/models.py`**

Add these imports at the top of `models.py` (alongside existing ones):

```python
from cvops_api.db.models.models import ModelArtifact
from cvops_api.schemas.models import ModelArtifactCreate, ModelArtifactOut
```

Add `ModelArtifactCreate` to `schemas/models.py`:

```python
class ModelArtifactCreate(BaseModel):
    blob_hash: str
    filename: str
    size_bytes: int
    mime_type: str | None = None
```

Add these endpoints to `routers/models.py`:

```python
# ── Model Artifacts ───────────────────────────────────────────────────────────

@router.get("/models/{id}/artifacts/upload-url")
async def get_artifact_upload_url(
    id: uuid.UUID,
    blob_hash: str,
    filename: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    await _get_model_version(id, current_user, session)
    url = await get_storage().get_presigned_put(
        blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    return {"upload_url": url}


@router.post("/models/{id}/artifacts", response_model=ModelArtifactOut, status_code=201)
async def create_artifact(
    id: uuid.UUID,
    body: ModelArtifactCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ModelArtifactOut:
    await _get_model_version(id, current_user, session)
    await session.execute(
        pg_insert(Blob)
        .values(
            hash=body.blob_hash,
            storage_backend=settings.S3_BACKEND,
            storage_key=StorageBackend._bucket_key(body.blob_hash),
            size_bytes=body.size_bytes,
            media_type=body.mime_type or "application/octet-stream",
        )
        .on_conflict_do_nothing(index_elements=["hash"])
    )
    artifact = ModelArtifact(
        model_version_id=id,
        blob_hash=body.blob_hash,
        filename=body.filename,
        mime_type=body.mime_type,
        created_by=current_user.id,
    )
    session.add(artifact)
    await session.commit()
    await session.refresh(artifact)
    url = await get_storage().get_presigned_get(
        body.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
    )
    out = ModelArtifactOut.model_validate(artifact)
    out.url = url
    return out


@router.get("/models/{id}/artifacts", response_model=list[ModelArtifactOut])
async def list_artifacts(
    id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ModelArtifactOut]:
    await _get_model_version(id, current_user, session)
    r = await session.execute(
        select(ModelArtifact).where(ModelArtifact.model_version_id == id)
    )
    artifacts = r.scalars().all()
    results = []
    for a in artifacts:
        url = await get_storage().get_presigned_get(
            a.blob_hash, endpoint=public_s3_endpoint(request.url.hostname)
        )
        out = ModelArtifactOut.model_validate(a)
        out.url = url
        results.append(out)
    return results
```

- [ ] **Step 6: Run tests — expect PASS**

```bash
cd services/api
pytest tests/routers/test_model_artifacts.py tests/routers/test_models_upload.py tests/routers/test_models.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/cvops_api/db/models/models.py \
        services/api/alembic/versions/0003_model_artifacts.py \
        services/api/src/cvops_api/schemas/models.py \
        services/api/src/cvops_api/routers/models.py \
        services/api/tests/routers/test_model_artifacts.py
git commit -m "feat: add model artifact upload and gallery endpoints"
```

---

### Task 5: Frontend — artifact gallery in ModelDetail

**Files:**
- Modify: `services/frontend/src/api/models.ts`
- Modify: `services/frontend/src/pages/ModelDetail.tsx`

**Interfaces:**
- Consumes: `GET /models/{id}/artifacts/upload-url`, `POST /models/{id}/artifacts`, `GET /models/{id}/artifacts` from Task 4.
- Produces: file drop zone + image gallery visible below metrics on ModelDetail page.

- [ ] **Step 1: Add artifact hooks to `api/models.ts`**

Add these to the bottom of `services/frontend/src/api/models.ts`:

```typescript
export interface ModelArtifact {
  id: string
  model_version_id: string
  blob_hash: string
  filename: string
  mime_type: string | null
  created_at: string
  url: string | null
}

export function useModelArtifacts(modelId: string | undefined) {
  return useQuery<ModelArtifact[]>({
    queryKey: ['model-artifacts', modelId],
    queryFn: async () => {
      const { data } = await client.get<ModelArtifact[]>(`/models/${modelId}/artifacts`)
      return data
    },
    enabled: !!modelId,
    staleTime: PRESIGNED_URL_STALE_MS,
    gcTime: PRESIGNED_URL_GC_MS,
  })
}

export function useUploadArtifact(modelId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (file: File) => {
      const buf = await file.arrayBuffer()
      const digest = await crypto.subtle.digest('SHA-256', buf)
      const blobHash = Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join('')

      const { data: slot } = await client.get<{ upload_url: string }>(
        `/models/${modelId}/artifacts/upload-url`,
        { params: { blob_hash: blobHash, filename: file.name } },
      )
      await fetch(slot.upload_url, {
        method: 'PUT',
        body: file,
        headers: { 'Content-Type': file.type || 'application/octet-stream' },
      })
      const { data } = await client.post<ModelArtifact>(`/models/${modelId}/artifacts`, {
        blob_hash: blobHash,
        filename: file.name,
        mime_type: file.type || null,
        size_bytes: file.size,
      })
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['model-artifacts', modelId] }),
  })
}
```

- [ ] **Step 2: Add artifact gallery section to `ModelDetail.tsx`**

Add these imports at the top of `ModelDetail.tsx`:

```typescript
import { useRef } from 'react'
import { useModelArtifacts, useUploadArtifact } from '../api/models'
```

Add the `ArtifactGallery` component and its usage inside `ModelDetail`. Place this after all existing `Card` blocks, before the closing `</div>`:

```typescript
// Inside ModelDetail(), after the existing hook calls:
const { data: artifacts } = useModelArtifacts(id)
const uploadArtifact = useUploadArtifact(id!)
const dropRef = useRef<HTMLDivElement>(null)

async function handleFiles(files: FileList | null) {
  if (!files) return
  for (const file of Array.from(files)) {
    try {
      await uploadArtifact.mutateAsync(file)
    } catch {
      toast.error(`Failed to upload ${file.name}`)
    }
  }
}

// Then add this JSX block after the hyperparams Card:

{/* Artifact gallery */}
<Card className="mt-4 p-6">
  <div className="mb-3 flex items-center justify-between">
    <h3 className="text-sm font-bold text-text-secondary">Training Artifacts</h3>
    <label className="cursor-pointer rounded-lg border border-border px-3 py-1 text-xs text-text-muted transition-colors hover:border-iris hover:text-iris-400">
      + Add files
      <input
        type="file"
        multiple
        className="hidden"
        onChange={(e) => handleFiles(e.target.files)}
      />
    </label>
  </div>

  {/* Drop zone */}
  <div
    ref={dropRef}
    onDragOver={(e) => { e.preventDefault(); dropRef.current?.classList.add('border-iris') }}
    onDragLeave={() => dropRef.current?.classList.remove('border-iris')}
    onDrop={(e) => { e.preventDefault(); dropRef.current?.classList.remove('border-iris'); handleFiles(e.dataTransfer.files) }}
    className="mb-4 rounded-lg border-2 border-dashed border-border py-6 text-center text-xs text-text-muted transition-colors"
  >
    Drop training plots, CSVs, or any run files here
  </div>

  {uploadArtifact.isPending && (
    <p className="mb-3 text-xs text-text-muted">Uploading…</p>
  )}

  {artifacts && artifacts.length === 0 && (
    <p className="text-xs text-text-muted">No artifacts yet.</p>
  )}

  {artifacts && artifacts.length > 0 && (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {artifacts.map((a) => (
        <div key={a.id} className="overflow-hidden rounded-lg border border-border">
          {a.mime_type?.startsWith('image/') && a.url ? (
            <a href={a.url} target="_blank" rel="noreferrer">
              <img src={a.url} alt={a.filename} className="h-36 w-full object-cover" />
            </a>
          ) : (
            <div className="flex h-36 items-center justify-center bg-surface-3">
              <span className="text-3xl">📄</span>
            </div>
          )}
          <div className="px-2 py-1.5">
            <p className="truncate text-xs text-text-secondary" title={a.filename}>{a.filename}</p>
            {a.url && (
              <a href={a.url} target="_blank" rel="noreferrer" className="text-xs text-iris-400 hover:opacity-80">
                Download
              </a>
            )}
          </div>
        </div>
      ))}
    </div>
  )}
</Card>
```

- [ ] **Step 3: Type-check**

```bash
cd services/frontend
npm run typecheck
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add services/frontend/src/api/models.ts \
        services/frontend/src/pages/ModelDetail.tsx
git commit -m "feat: add training artifact gallery to model detail page"
```

---

## Part C — Fix Annotate Bug + Auto-Labeling

### Task 6: Fix `cvat_annotate` body mixing bug

**Files:**
- Modify: `services/api/src/cvops_api/routers/cvat.py`
- Modify: `services/model-deployer/app.py`
- Modify: `services/model-deployer/tests/test_app.py` (un-pin the known bug)

**Interfaces:**
- Produces: working `POST /projects/{project_id}/cvat-annotate` endpoint and working `POST /annotate` on model-deployer — consumed by Task 7's `AutoLabelStep` and any frontend auto-annotation flow.

- [ ] **Step 1: Write a test that currently fails**

Add to `services/api/tests/routers/test_cvat.py` (look for the existing test file; add at the bottom):

```python
@pytest.mark.anyio
async def test_cvat_annotate_accepts_form_params(client: AsyncClient, auth_headers, project, monkeypatch):
    """cvat_annotate should accept task_name/function_id as form fields, not JSON body."""
    import io
    # The handler is never reached in test (deployer isn't running), but we want 422 gone.
    # Patching out the httpx call so we test FastAPI's form parsing, not the deployer.
    import httpx
    async def _fake_post(*args, **kwargs):
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/cvat-annotate",
        data={"task_name": "my-task", "function_id": "yolo-detector", "threshold": "0.3"},
        files={"files": ("a.jpg", io.BytesIO(b"img"), "image/jpeg")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test — expect FAIL (422)**

```bash
cd services/api
pytest tests/routers/test_cvat.py::test_cvat_annotate_accepts_form_params -v
```

Expected: `assert 422 == 200` — bug confirmed.

- [ ] **Step 3: Fix `routers/cvat.py` — replace `AnnotateRequest` body with `Form` params**

In `services/api/src/cvops_api/routers/cvat.py`, locate the `cvat_annotate` function. Replace the signature (remove the `AnnotateRequest` class and the `body: AnnotateRequest` param):

Remove:
```python
class AnnotateRequest(BaseModel):
    task_name: str
    function_id: str
    threshold: float = 0.3


@router.post("/projects/{project_id}/cvat-annotate")
async def cvat_annotate(
    project_id: uuid.UUID,
    body: AnnotateRequest,
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
```

Replace with:
```python
@router.post("/projects/{project_id}/cvat-annotate")
async def cvat_annotate(
    project_id: uuid.UUID,
    task_name: str = Form(...),
    function_id: str = Form(...),
    threshold: float = Form(0.3),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
```

Also add `from fastapi import Form` to the imports at the top of `cvat.py` (it's probably already imported — check first).

Inside the function body, update any reference from `body.task_name` → `task_name`, `body.function_id` → `function_id`, `body.threshold` → `threshold`.

- [ ] **Step 4: Fix model-deployer `app.py` — same bug**

In `services/model-deployer/app.py`, remove the `AnnotateRequest` class and fix `annotate_task`:

Remove:
```python
class AnnotateRequest(BaseModel):
    task_name: str
    function_id: str
    threshold: float = 0.3
```

Replace the function signature:
```python
@app.post("/annotate", dependencies=[Depends(_require_token)])
async def annotate_task(
    task_name: str = Form(...),
    function_id: str = Form(...),
    threshold: float = Form(0.3),
    files: list[UploadFile] = File(...),
) -> dict:
```

Update the `annotate(...)` call inside to use `task_name`, `function_id`, `threshold` directly (no more `body.` prefix).

Also add `from fastapi import Form` to the imports at the top.

- [ ] **Step 5: Update the pinned-bug test in `test_app.py`**

In `services/model-deployer/tests/test_app.py`, find `test_annotate_multipart_body_is_uncallable_422` and replace it with a test that asserts the fixed behavior:

```python
def test_annotate_calls_handler_with_form_params(client, app_module, monkeypatch) -> None:
    """annotate_task now takes form fields, so the handler is reachable."""
    import io
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
```

- [ ] **Step 6: Run all tests — expect PASS**

```bash
cd services/api
pytest tests/routers/test_cvat.py -q

cd ../../services/model-deployer
pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/cvops_api/routers/cvat.py \
        services/model-deployer/app.py \
        services/model-deployer/tests/test_app.py \
        services/api/tests/routers/test_cvat.py
git commit -m "fix: annotate endpoint form param mixing bug in router and deployer"
```

---

### Task 7: Implement `AutoLabelStep`

**Files:**
- Modify: `packages/steps/src/cvops_steps/auto_label.py`
- Modify: `packages/steps/src/cvops_steps/schemas/auto_label.json`
- Create: `services/api/tests/steps/test_auto_label.py`

**Context:**
The existing `auto_label.json` config schema uses `model_version_id` (UUID) and `confidence_threshold`. The step:
1. Downloads the `.pt` weights blob from storage for the given `model_version_id`
2. Runs YOLO inference locally (ultralytics, available on `worker-training`)
3. For each sample in `inputs.sample_ids`, downloads the image, infers, writes an `annotation_revision` row
4. Returns `{annotation_revision_ids: list[str]}`

The Nuclio handler format (`services/model-deployer/nuclio_base/main.py`) takes `{"image": "<base64>", "threshold": 0.5}` → `[{label, confidence, points: [x1,y1,x2,y2], type}]`. The `AutoLabelStep` uses the same format for local inference.

**Interfaces:**
- Consumes: `StepContext` (ctx.session, ctx.storage, ctx.project_id, ctx.run_id, ctx.actor_id, ctx.emit_event); `auto_label.json` schema already defines `model_version_id` and `confidence_threshold`.
- Produces: `{annotation_revision_ids: list[str]}` — compatible with `human_review` step inputs for downstream review gates.

- [ ] **Step 1: Update `auto_label.json` — add ontology_id**

The step must write `annotation_revisions` which require an `ontology_id`. Add it (optional, falls back to project default like `human_review` does):

Replace `services/api/src/cvops_steps/schemas/auto_label.json` (check actual path: `packages/steps/src/cvops_steps/schemas/auto_label.json`):

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Auto Label Config",
  "type": "object",
  "properties": {
    "model_version_id": {
      "type": "string", "format": "uuid",
      "description": "UUID of the model_version whose weights are used for inference"
    },
    "confidence_threshold": {
      "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.35,
      "description": "Detections below this confidence are discarded"
    },
    "ontology_id": {
      "type": "string", "format": "uuid",
      "description": "UUID of the ontology whose class_keys label detections. Defaults to project default."
    }
  },
  "required": ["model_version_id"],
  "additionalProperties": false
}
```

- [ ] **Step 2: Write failing test**

Create `services/api/tests/steps/test_auto_label.py`:

```python
"""Unit test for AutoLabelStep — mocks storage and DB; no real YOLO inference."""
from __future__ import annotations
import base64
import io
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.anyio
async def test_auto_label_writes_annotation_revisions(mock_step_context):
    """AutoLabelStep calls YOLO, writes one annotation_revision per sample."""
    from cvops_steps.auto_label import AutoLabelStep

    sample_id = str(uuid.uuid4())
    blob_hash = "a" * 64
    ont_id = str(uuid.uuid4())

    # Fake DB rows returned by raw SQL
    ctx = mock_step_context
    ctx.session.execute = AsyncMock(side_effect=[
        # model_version lookup: blob_hash
        MagicMock(first=lambda: (blob_hash,)),
        # sample rows: (id, blob_hash, width, height)
        MagicMock(all=lambda: [(sample_id, "b" * 64, 640, 480)]),
        # ontology lookup: (ont_id, 1)
        MagicMock(first=lambda: (ont_id, 1)),
        # revision_no max: 0
        MagicMock(scalar=lambda: 0),
        # INSERT annotation_revision: no return needed
        MagicMock(),
    ])

    # Fake storage: return tiny JPEG bytes
    tiny_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # minimal JPEG header
    ctx.storage.get_bytes = AsyncMock(return_value=tiny_jpg)

    # Fake YOLO: return one detection
    fake_result = MagicMock()
    fake_box = MagicMock()
    fake_box.xyxy = [[MagicMock(tolist=lambda: [10.0, 20.0, 100.0, 80.0])]]
    fake_box.conf = [[0.9]]
    fake_box.cls = [[0]]
    fake_result.boxes = [fake_box]

    fake_yolo_instance = MagicMock()
    fake_yolo_instance.return_value = [fake_result]
    fake_yolo_instance.names = {0: "car"}

    with patch("ultralytics.YOLO", return_value=fake_yolo_instance):
        step = AutoLabelStep()
        result = await step.run(
            ctx,
            config={"model_version_id": str(uuid.uuid4()), "confidence_threshold": 0.3},
            inputs={"sample_ids": [sample_id]},
        )

    assert "annotation_revision_ids" in result
    assert len(result["annotation_revision_ids"]) == 1
```

- [ ] **Step 3: Run test — expect FAIL (NotImplementedError)**

```bash
cd services/api
PYTHONPATH="$(pwd)/../../../packages/steps/src:$(pwd)/src" pytest tests/steps/test_auto_label.py -v
```

Expected: `NotImplementedError`.

- [ ] **Step 4: Implement `AutoLabelStep`**

Replace the full content of `packages/steps/src/cvops_steps/auto_label.py`:

```python
from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from cvops_api.engine.step import Step, StepContext

from cvops_steps import _load_schema

_SCHEMA = _load_schema("auto_label")


class AutoLabelStep(Step):
    type_key = "step.auto_label"
    config_schema = _SCHEMA
    queue = "training"  # ponytail: worker-training has ultralytics; preprocessing does not

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415
        from ultralytics import YOLO  # noqa: PLC0415

        session = ctx.session
        model_version_id = config["model_version_id"]
        threshold = float(config.get("confidence_threshold", 0.35))
        sample_ids = [str(s) for s in inputs.get("sample_ids", [])]

        if not sample_ids:
            return {"annotation_revision_ids": []}

        # ── Resolve model weights blob ────────────────────────────────────────
        mv_row = (
            await session.execute(
                text("SELECT blob_hash FROM model_versions WHERE id = CAST(:id AS uuid)"),
                {"id": model_version_id},
            )
        ).first()
        if mv_row is None:
            raise ValueError(f"model_version {model_version_id} not found")
        weights_blob_hash = mv_row[0]

        # ── Resolve samples ───────────────────────────────────────────────────
        sample_rows = (
            await session.execute(
                text(
                    "SELECT id, blob_hash, width, height FROM samples "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": sample_ids},
            )
        ).all()
        if not sample_rows:
            raise ValueError(f"no samples found for ids: {sample_ids}")

        # ── Resolve ontology ──────────────────────────────────────────────────
        ont_id_cfg = config.get("ontology_id")
        if ont_id_cfg:
            ont_row = (
                await session.execute(
                    text(
                        "SELECT id, version FROM ontologies "
                        "WHERE id = CAST(:oid AS uuid) AND deleted_at IS NULL"
                    ),
                    {"oid": ont_id_cfg},
                )
            ).first()
            if ont_row is None:
                raise ValueError(f"ontology {ont_id_cfg} not found")
        else:
            ont_row = (
                await session.execute(
                    text(
                        "SELECT o.id, o.version FROM ontologies o "
                        "JOIN projects p ON p.org_id = o.org_id "
                        "WHERE p.id = CAST(:pid AS uuid) AND o.deleted_at IS NULL "
                        "ORDER BY (p.default_ontology_id = o.id) DESC NULLS LAST, o.version DESC "
                        "LIMIT 1"
                    ),
                    {"pid": ctx.project_id},
                )
            ).first()
            if ont_row is None:
                raise ValueError("auto_label requires the project to have an ontology")
        ont_id, ont_version = str(ont_row[0]), ont_row[1]

        # ── Download weights and load YOLO ────────────────────────────────────
        weights_bytes = await ctx.storage.get_bytes(weights_blob_hash)
        with tempfile.TemporaryDirectory() as tmp:
            pt_path = Path(tmp) / "model.pt"
            pt_path.write_bytes(weights_bytes)
            model = YOLO(str(pt_path))

            # ── Infer per sample ──────────────────────────────────────────────
            revision_ids: list[str] = []
            for sid, blob_hash, width, height in sample_rows:
                img_bytes = await ctx.storage.get_bytes(blob_hash)
                image = np.array(Image.open(__import__("io").BytesIO(img_bytes)).convert("RGB"))
                results = model(image, conf=threshold, verbose=False)[0]

                # Normalize detections to [0,1] relative coords; store in
                # annotation_revision.payload as list of {label, points, confidence}
                payload = []
                for box in results.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    payload.append({
                        "label": model.names[int(box.cls[0])],
                        "confidence": float(box.conf[0]),
                        "points": [x1 / width, y1 / height, x2 / width, y2 / height],
                        "type": "rectangle",
                    })

                # Next revision_no for this sample
                rev_no_row = await session.execute(
                    text(
                        "SELECT COALESCE(MAX(revision_no), 0) + 1 "
                        "FROM annotation_revisions WHERE sample_id = CAST(:sid AS uuid)"
                    ),
                    {"sid": str(sid)},
                )
                rev_no = rev_no_row.scalar()

                rev_id = str(uuid.uuid4())
                await session.execute(
                    text(
                        "INSERT INTO annotation_revisions "
                        "(id, sample_id, revision_no, ontology_id, ontology_version, "
                        "payload, provenance) VALUES "
                        "(CAST(:id AS uuid), CAST(:sid AS uuid), :rev_no, "
                        "CAST(:oid AS uuid), :over, CAST(:payload AS jsonb), :prov)"
                    ),
                    {
                        "id": rev_id,
                        "sid": str(sid),
                        "rev_no": rev_no,
                        "oid": ont_id,
                        "over": ont_version,
                        "payload": json.dumps(payload),
                        "prov": "model",
                    },
                )
                revision_ids.append(rev_id)

        await session.commit()

        await ctx.emit_event(
            actor_id=ctx.actor_id,
            actor_type="system",
            entity_type="run",
            entity_id=ctx.run_id,
            action="auto_label.completed",
            payload={"sample_count": len(revision_ids), "model_version_id": model_version_id},
        )

        return {"annotation_revision_ids": revision_ids}
```

Note: if `_load_schema` doesn't exist as a helper in `cvops_steps/__init__.py`, replace `_load_schema("auto_label")` with the inline pattern from the original file:
```python
with open(Path(__file__).parent / "schemas" / "auto_label.json") as f:
    _SCHEMA = json.load(f)
```

- [ ] **Step 5: Run test — expect PASS**

```bash
cd services/api
PYTHONPATH="$(pwd)/../../../packages/steps/src:$(pwd)/src" pytest tests/steps/test_auto_label.py -v
```

Expected: PASS.

- [ ] **Step 6: Verify the step is registered**

```bash
cd services/api
python -c "
from cvops_steps import register_all
from cvops_api.core.registry import registry
register_all()
assert 'step.auto_label' in registry._steps, 'not registered'
print('OK:', list(registry._steps.keys()))
"
```

Expected: `OK: [..., 'step.auto_label']`.

- [ ] **Step 7: Commit**

```bash
git add packages/steps/src/cvops_steps/auto_label.py \
        packages/steps/src/cvops_steps/schemas/auto_label.json \
        services/api/tests/steps/test_auto_label.py
git commit -m "feat: implement auto label step with local YOLO inference"
```

---

## Self-Review

### Spec coverage

| Requirement | Task |
|---|---|
| Upload .pt files | Task 2 (API), Task 3 (UI upload form) |
| Versioning with name/description | Task 1 (DB+schema), Task 3 (form fields + edit) |
| Link to dataset (commit) | Task 1 (`trained_on_commit_id` field), Task 3 (clickable link in ModelDetail) |
| MLflow run link, editable | Task 3 (edit modal includes `mlflow_run_id`) |
| Upload runs folder / view artifacts as images | Task 4 (API), Task 5 (gallery with image preview) |
| Auto-labeling | Task 6 (fix bug), Task 7 (implement step) |

### Known gaps / future work

- **Commit detail page** — `ModelDetail` links to `/projects/{id}/commits/{commit_id}` but that route may not exist yet. Check `App.tsx`; if absent, the link still works as a URL — just won't render a page until the commits page is built.
- **Auto-label frontend trigger** — Task 7 implements the step so it runs inside a workflow. There is no one-click "auto-label this dataset" button in the UI yet; trigger it by building a workflow that chains `auto_label → human_review`.
- **`worker-training` has ultralytics** — the `AutoLabelStep` is routed to the `training` queue. If `worker-training` is not running (it's opt-in with `tilt up -- --training`), the step will queue but not execute. Document this in the step's `config_schema` description or a UI tooltip.
- **Nuclio functions vs. local YOLO** — `AutoLabelStep` downloads weights and runs inference locally. If users want to reuse an already-deployed Nuclio function (model-deployer), that's a separate code path not in this plan.
