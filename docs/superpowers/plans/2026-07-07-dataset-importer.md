# Dataset Importer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users import existing labeled datasets (YOLO, COCO, or raw images) into CVOps via a zip upload or server-side folder path, with an optional CVAT human-review gate before committing.

**Architecture:** A new `step.import_dataset` step in `packages/steps` handles all parsing and DB writes; a new `POST /projects/{id}/imports` endpoint builds an inline DAG (`import_dataset → [human_review?] → commit_dataset`) identical to the ad-hoc train endpoint pattern; a new `ImportDataset` frontend page drives the upload and dispatches the run.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async (raw SQL), moto (S3 mock in tests), testcontainers (Postgres in tests), React 18 + TypeScript, TanStack Query, Tailwind.

## Global Constraints

- Steps may only touch `cvops_api.core` and `cvops_api.engine` — never `cvops_api.db.models` or routers. All DB access via `ctx.session.execute(text(...))`.
- Annotation payload shape: `[{"class_key": str, "geometry": {"type": "bbox", "coords": [cx, cy, w, h]}}]` — coords normalized 0–1 (cx, cy, w, h).
- Annotation provenance shape: `{"source": "import", "review_status": "unreviewed"}`.
- Sample blob_hash uniqueness per project: `ON CONFLICT (project_id, blob_hash) DO NOTHING RETURNING id`.
- No hardcoded colors in frontend — use semantic tokens only (`bg-surface-2`, `text-text-primary`, etc.).
- All public API routes mounted under `/api/v1` prefix in `main.py`.
- The `imports` router defines its own full path prefix (`/projects/{id}/imports`) and is mounted with just `API_V1` in `main.py` (same pattern as `datasets`, `data_sources`).

---

### Task 1: Step schema + pure parsing helpers

**Files:**
- Create: `packages/steps/src/cvops_steps/schemas/import_dataset.json`
- Create: `packages/steps/src/cvops_steps/import_dataset.py` (helpers + stub class)
- Create: `services/api/tests/steps/test_import_dataset_parsers.py`

**Interfaces:**
- Produces:
  - `detect_format(root: Path) -> Literal["yolo", "coco", "raw"]`
  - `parse_yolo(root: Path, class_names: list[str]) -> dict[str, list[dict]]` — maps image *stem* → annotation list
  - `parse_coco(root: Path) -> dict[str, list[dict]]` — maps image *filename* → annotation list
  - `IMAGE_EXTS: frozenset[str]`
  - `ImportDatasetStep` class (stub; `run()` filled in Task 2)

- [ ] **Step 1.1: Write failing tests for helpers**

```python
# services/api/tests/steps/test_import_dataset_parsers.py
"""Unit tests for import_dataset parsing helpers — no DB, no I/O needed."""
from __future__ import annotations
import json
import zipfile
from pathlib import Path
import pytest
from cvops_steps.import_dataset import detect_format, parse_yolo, parse_coco, IMAGE_EXTS


def _make_yolo_tree(tmp_path: Path) -> Path:
    (tmp_path / "images").mkdir()
    (tmp_path / "labels").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"fake")
    (tmp_path / "labels" / "a.txt").write_text("0 0.5 0.5 0.2 0.3\n1 0.1 0.1 0.05 0.05\n")
    (tmp_path / "data.yaml").write_text("names:\n  - cat\n  - dog\n")
    return tmp_path


def _make_coco_tree(tmp_path: Path) -> Path:
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "b.jpg").write_bytes(b"fake")
    ann = {
        "images": [{"id": 1, "file_name": "b.jpg", "width": 100, "height": 200}],
        "annotations": [{"id": 1, "image_id": 1, "category_id": 1, "bbox": [10, 20, 30, 40]}],
        "categories": [{"id": 1, "name": "car"}],
    }
    (tmp_path / "annotations.json").write_text(json.dumps(ann))
    return tmp_path


def test_detect_format_yolo(tmp_path):
    _make_yolo_tree(tmp_path)
    assert detect_format(tmp_path) == "yolo"


def test_detect_format_coco(tmp_path):
    _make_coco_tree(tmp_path)
    assert detect_format(tmp_path) == "coco"


def test_detect_format_raw(tmp_path):
    (tmp_path / "img.png").write_bytes(b"x")
    assert detect_format(tmp_path) == "raw"


def test_parse_yolo_maps_class_indices(tmp_path):
    _make_yolo_tree(tmp_path)
    result = parse_yolo(tmp_path, ["cat", "dog"])
    anns = result["a"]
    assert len(anns) == 2
    assert anns[0]["class_key"] == "cat"
    assert anns[0]["geometry"] == {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.3]}
    assert anns[1]["class_key"] == "dog"


def test_parse_coco_normalises_bbox(tmp_path):
    _make_coco_tree(tmp_path)
    result = parse_coco(tmp_path)
    anns = result["b.jpg"]
    assert len(anns) == 1
    a = anns[0]
    assert a["class_key"] == "car"
    # bbox [10, 20, 30, 40] on 100×200 → cx=(10+15)/100=0.25, cy=(20+20)/200=0.2, w=0.3, h=0.2
    coords = a["geometry"]["coords"]
    assert abs(coords[0] - 0.25) < 1e-6
    assert abs(coords[1] - 0.2) < 1e-6
    assert abs(coords[2] - 0.3) < 1e-6
    assert abs(coords[3] - 0.2) < 1e-6


def test_image_exts_covers_common_types():
    assert ".jpg" in IMAGE_EXTS
    assert ".png" in IMAGE_EXTS
    assert ".bmp" in IMAGE_EXTS
```

- [ ] **Step 1.2: Run tests to confirm they fail**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/steps/test_import_dataset_parsers.py -v
```
Expected: `ModuleNotFoundError: No module named 'cvops_steps.import_dataset'`

- [ ] **Step 1.3: Create the schema JSON**

```json
// packages/steps/src/cvops_steps/schemas/import_dataset.json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Import Dataset Config",
  "type": "object",
  "properties": {
    "format": {
      "type": "string",
      "enum": ["auto", "yolo", "coco", "raw"],
      "default": "auto",
      "description": "Dataset format. 'auto' inspects the directory."
    },
    "ontology_id": {
      "type": "string",
      "format": "uuid",
      "description": "If omitted, images are ingested as samples but annotations are skipped."
    }
  },
  "required": [],
  "additionalProperties": false
}
```

- [ ] **Step 1.4: Create import_dataset.py with helpers and stub class**

```python
# packages/steps/src/cvops_steps/import_dataset.py
"""import_dataset — ingest an existing labeled dataset (YOLO, COCO, or raw images).

Accepts either a zip blob (downloaded from MinIO) or a server-side folder path.
Creates blobs, samples, and annotation_revisions (provenance='import'), then
outputs sample_ids + annotation_revision_ids so downstream steps
(human_review, commit_dataset) can chain directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from cvops_api.engine.step import Step, StepContext

IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".bmp"})

with open(Path(__file__).parent / "schemas" / "import_dataset.json") as _f:
    _SCHEMA = json.load(_f)


def detect_format(root: Path) -> Literal["yolo", "coco", "raw"]:
    """Inspect a directory and return the dataset format."""
    if (root / "data.yaml").exists():
        return "yolo"
    for p in root.rglob("*.json"):
        try:
            doc = json.loads(p.read_text())
            if isinstance(doc, dict) and "categories" in doc:
                return "coco"
        except (json.JSONDecodeError, OSError):
            continue
    return "raw"


def parse_yolo(root: Path, class_names: list[str]) -> dict[str, list[dict]]:
    """Parse YOLO label files.

    Returns a dict mapping image *stem* → list of annotation dicts
    with keys 'class_key' and 'geometry' (bbox, normalized coords).
    """
    result: dict[str, list[dict]] = {}
    labels_dir = root / "labels"
    if not labels_dir.is_dir():
        return result
    for txt in labels_dir.glob("*.txt"):
        anns: list[dict] = []
        for line in txt.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            idx = int(parts[0])
            if idx >= len(class_names):
                continue
            cx, cy, w, h = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            anns.append({
                "class_key": class_names[idx],
                "geometry": {"type": "bbox", "coords": [cx, cy, w, h]},
            })
        if anns:
            result[txt.stem] = anns
    return result


def parse_coco(root: Path) -> dict[str, list[dict]]:
    """Parse a COCO annotations.json file.

    Returns a dict mapping image *filename* → list of annotation dicts
    with keys 'class_key' and 'geometry' (bbox, normalized coords).
    """
    coco_json: Path | None = None
    for p in root.rglob("*.json"):
        try:
            doc = json.loads(p.read_text())
            if isinstance(doc, dict) and "categories" in doc:
                coco_json = p
                break
        except (json.JSONDecodeError, OSError):
            continue
    if coco_json is None:
        return {}

    doc = json.loads(coco_json.read_text())
    cat_map = {c["id"]: c["name"] for c in doc.get("categories", [])}
    img_map = {i["id"]: i for i in doc.get("images", [])}

    result: dict[str, list[dict]] = {}
    for ann in doc.get("annotations", []):
        img = img_map.get(ann["image_id"])
        if img is None:
            continue
        cat_name = cat_map.get(ann["category_id"])
        if cat_name is None:
            continue
        x, y, bw, bh = ann["bbox"]
        iw, ih = img["width"], img["height"]
        cx = (x + bw / 2) / iw
        cy = (y + bh / 2) / ih
        w = bw / iw
        h = bh / ih
        fname = img["file_name"]
        result.setdefault(fname, []).append({
            "class_key": cat_name,
            "geometry": {"type": "bbox", "coords": [cx, cy, w, h]},
        })
    return result


class ImportDatasetStep(Step):
    type_key = "step.import_dataset"
    config_schema = _SCHEMA

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        # Implemented in Task 2
        raise NotImplementedError
```

- [ ] **Step 1.5: Run tests to confirm they pass**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/steps/test_import_dataset_parsers.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 1.6: Commit**

```bash
git add packages/steps/src/cvops_steps/schemas/import_dataset.json \
        packages/steps/src/cvops_steps/import_dataset.py \
        services/api/tests/steps/test_import_dataset_parsers.py
git commit -m "feat: add import_dataset step schema and parsing helpers"
```

---

### Task 2: Step run() implementation + registration

**Files:**
- Modify: `packages/steps/src/cvops_steps/import_dataset.py` (fill in `run()`)
- Modify: `packages/steps/src/cvops_steps/__init__.py` (register)
- Create: `services/api/tests/steps/test_import_dataset_step.py`

**Interfaces:**
- Consumes: `detect_format`, `parse_yolo`, `parse_coco`, `IMAGE_EXTS` from Task 1.
- Produces: `ImportDatasetStep.run()` returning `{"sample_ids": [...], "annotation_revision_ids": [...]}`.

- [ ] **Step 2.1: Write failing integration test**

```python
# services/api/tests/steps/test_import_dataset_step.py
"""Integration test for ImportDatasetStep.run().

Seeds org/project/ontology in testcontainers Postgres. Builds a synthetic YOLO
dataset zip in memory, stores it in moto S3, then runs the step end-to-end.
Asserts samples and annotation_revisions rows are created correctly.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.config import settings
from cvops_api.core.storage import S3Backend
from cvops_api.engine.step import StepContext
from cvops_steps.import_dataset import ImportDatasetStep


def _moto_settings():
    return (
        patch.object(settings, "S3_ENDPOINT", None),
        patch.object(settings, "S3_REGION", "us-east-1"),
        patch.object(settings, "S3_ACCESS_KEY", "testing"),
        patch.object(settings, "S3_SECRET_KEY", "testing"),
        patch.object(settings, "S3_BUCKET", "test-bucket"),
        patch.object(settings, "S3_PUBLIC_ENDPOINT", ""),
    )


def _make_yolo_zip() -> tuple[bytes, str]:
    """Build a minimal YOLO zip: 1 image + 1 label + data.yaml. Returns (bytes, sha256)."""
    import numpy as np
    import cv2

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 64x64 solid-color JPEG
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        img[:, :] = [100, 150, 200]
        ok, encoded = cv2.imencode(".jpg", img)
        assert ok
        zf.writestr("images/frame0.jpg", encoded.tobytes())
        zf.writestr("labels/frame0.txt", "0 0.5 0.5 0.2 0.3\n")
        zf.writestr("data.yaml", yaml.dump({"names": ["cat", "dog"]}))
    data = buf.getvalue()
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    return data, sha


async def _seed(session: AsyncSession) -> tuple[str, str, str]:
    """Create org/project/ontology with 'cat' label class. Returns (project_id, ontology_id, cat_class_id)."""
    org_id, proj_id, ont_id, cls_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(text("INSERT INTO orgs (id, name) VALUES (:i, :n)"),
                          {"i": org_id, "n": f"org-{uuid.uuid4().hex[:6]}"})
    await session.execute(text("INSERT INTO projects (id, org_id, name) VALUES (:i, :o, :n)"),
                          {"i": proj_id, "o": org_id, "n": "proj"})
    await session.execute(text("INSERT INTO ontologies (id, org_id, name, version) VALUES (:i, :o, 'o', 1)"),
                          {"i": ont_id, "o": org_id})
    await session.execute(
        text("INSERT INTO label_classes (id, ontology_id, class_key, display_name, sort_order) "
             "VALUES (:i, :o, 'cat', 'Cat', 0)"),
        {"i": cls_id, "o": ont_id},
    )
    await session.flush()
    return str(proj_id), str(ont_id), str(cls_id)


async def _emit(**kw) -> None:
    pass


async def test_import_yolo_zip_creates_sample_and_revision(session: AsyncSession) -> None:
    proj_id, ont_id, _cls_id = await _seed(session)
    zip_data, zip_hash = _make_yolo_zip()

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        import boto3
        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        backend = S3Backend()
        stored_hash = await backend.save_bytes(zip_data, "application/zip")
        assert stored_hash == zip_hash

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=proj_id,
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=_emit,
        )
        result = await ImportDatasetStep().run(
            ctx,
            config={"format": "auto", "ontology_id": ont_id},
            inputs={"blob_hash": zip_hash},
        )

    assert len(result["sample_ids"]) == 1
    assert len(result["annotation_revision_ids"]) == 1

    # Verify DB rows
    sample = (await session.execute(
        text("SELECT id, blob_hash FROM samples WHERE project_id = CAST(:p AS uuid)"),
        {"p": proj_id},
    )).first()
    assert sample is not None

    rev = (await session.execute(
        text("SELECT payload, provenance FROM annotation_revisions "
             "WHERE sample_id = CAST(:s AS uuid)"),
        {"s": sample[0]},
    )).first()
    assert rev is not None
    payload = rev[0] if isinstance(rev[0], list) else json.loads(rev[0])
    assert payload[0]["class_key"] == "cat"
    provenance = rev[1] if isinstance(rev[1], dict) else json.loads(rev[1])
    assert provenance["source"] == "import"


async def test_import_no_ontology_skips_revisions(session: AsyncSession) -> None:
    proj_id, _ont_id, _cls_id = await _seed(session)
    zip_data, zip_hash = _make_yolo_zip()

    s1, s2, s3, s4, s5, s6 = _moto_settings()
    with mock_aws(), s1, s2, s3, s4, s5, s6:
        import boto3
        boto3.client("s3").create_bucket(Bucket=settings.S3_BUCKET)
        backend = S3Backend()
        await backend.save_bytes(zip_data, "application/zip")

        ctx = StepContext(
            session=session,
            storage=backend,
            project_id=proj_id,
            run_id=str(uuid.uuid4()),
            actor_id=str(uuid.uuid4()),
            emit_event=_emit,
        )
        result = await ImportDatasetStep().run(
            ctx,
            config={"format": "auto"},  # no ontology_id
            inputs={"blob_hash": zip_hash},
        )

    assert len(result["sample_ids"]) == 1
    assert result["annotation_revision_ids"] == []
```

- [ ] **Step 2.2: Run test to confirm it fails**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/steps/test_import_dataset_step.py -v
```
Expected: `NotImplementedError` from the stub `run()`.

- [ ] **Step 2.3: Implement run() in import_dataset.py**

Replace the stub `run()` with the full implementation. Keep everything above the class unchanged.

```python
    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        import asyncio
        import hashlib
        import tempfile
        import uuid
        import zipfile
        from pathlib import Path

        import yaml
        from sqlalchemy import text

        from cvops_api.config import settings
        from cvops_api.core.storage import StorageBackend

        blob_hash: str | None = inputs.get("blob_hash")
        folder_path: str | None = inputs.get("folder_path")
        if bool(blob_hash) == bool(folder_path):
            raise ValueError("Exactly one of blob_hash or folder_path must be provided")

        ontology_id: str | None = config.get("ontology_id")
        fmt: str = config.get("format", "auto")

        # ── 1. Resolve source directory ───────────────────────────────────
        _tmpdir = None
        if blob_hash:
            zip_bytes = await ctx.storage.get_bytes(blob_hash)
            _tmpdir = tempfile.mkdtemp()
            root = Path(_tmpdir)
            with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
                zf.extractall(root)
            # If the zip contained a single top-level directory, descend into it.
            entries = list(root.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
        else:
            root = Path(folder_path)  # type: ignore[arg-type]

        try:
            return await self._ingest(ctx, config, root, ontology_id, fmt, settings, StorageBackend)
        finally:
            if _tmpdir:
                import shutil
                shutil.rmtree(_tmpdir, ignore_errors=True)

    async def _ingest(  # type: ignore[override]
        self,
        ctx: StepContext,
        config: dict,
        root: Path,
        ontology_id: str | None,
        fmt: str,
        settings: object,
        StorageBackend: object,
    ) -> dict:
        import asyncio
        import hashlib
        import uuid

        import cv2
        import numpy as np
        from sqlalchemy import text

        # ── 2. Format detection ────────────────────────────────────────────
        if fmt == "auto":
            fmt = detect_format(root)

        # ── 3. Load ontology label classes ────────────────────────────────
        class_key_map: dict[str, str] = {}  # class_name → class_key (same thing in this schema)
        ontology_version = 1
        if ontology_id:
            rows = (await ctx.session.execute(
                text("SELECT class_key FROM label_classes "
                     "WHERE ontology_id = CAST(:o AS uuid) ORDER BY sort_order"),
                {"o": ontology_id},
            )).all()
            class_key_map = {r[0]: r[0] for r in rows}
            ov_row = (await ctx.session.execute(
                text("SELECT version FROM ontologies WHERE id = CAST(:o AS uuid)"),
                {"o": ontology_id},
            )).first()
            if ov_row:
                ontology_version = ov_row[0]

        # ── 4. Parse annotations ──────────────────────────────────────────
        ann_by_stem: dict[str, list[dict]] = {}
        ann_by_filename: dict[str, list[dict]] = {}
        if ontology_id:
            if fmt == "yolo":
                import yaml as _yaml
                data_yaml = root / "data.yaml"
                class_names: list[str] = []
                if data_yaml.exists():
                    doc = _yaml.safe_load(data_yaml.read_text())
                    class_names = doc.get("names", [])
                ann_by_stem = parse_yolo(root, class_names)
            elif fmt == "coco":
                ann_by_filename = parse_coco(root)

        # ── 5. Walk images, create blobs + samples ────────────────────────
        def _make_thumb(img_bytes: bytes) -> bytes:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return img_bytes
            h, w = img.shape[:2]
            scale = 256 / max(h, w)
            thumb = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
            ok, buf = cv2.imencode(".jpg", thumb)
            return buf.tobytes() if ok else img_bytes

        def _image_dims(img_bytes: bytes) -> tuple[int, int]:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return 0, 0
            return img.shape[1], img.shape[0]  # width, height

        async def _register_blob(h: str, data: bytes, mt: str) -> None:
            await ctx.session.execute(
                text(
                    "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, media_type) "
                    "VALUES (:h, :sb, :sk, :sz, :mt) ON CONFLICT (hash) DO NOTHING"
                ),
                {"h": h, "sb": settings.S3_BACKEND,
                 "sk": StorageBackend._bucket_key(h),
                 "sz": len(data), "mt": mt},
            )

        image_files = sorted(
            p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTS
        )

        sample_ids: list[str] = []
        revision_ids: list[str] = []

        for img_path in image_files:
            img_bytes = img_path.read_bytes()
            img_hash = "sha256:" + hashlib.sha256(img_bytes).hexdigest()

            thumb_bytes, img_dims = await asyncio.to_thread(
                lambda b=img_bytes: (_make_thumb(b), _image_dims(b))
            )
            w, h = img_dims

            await ctx.storage.save_bytes(img_bytes, "image/jpeg")
            thumb_hash = await ctx.storage.save_bytes(thumb_bytes, "image/jpeg")
            await _register_blob(img_hash, img_bytes, "image/jpeg")
            await _register_blob(thumb_hash, thumb_bytes, "image/jpeg")

            sid = str(uuid.uuid4())
            res = await ctx.session.execute(
                text(
                    "INSERT INTO samples (id, project_id, blob_hash, width, height, thumbnail_hash) "
                    "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), :bh, :w, :h, :th) "
                    "ON CONFLICT (project_id, blob_hash) DO NOTHING RETURNING id"
                ),
                {"id": sid, "pid": ctx.project_id, "bh": img_hash, "w": w, "h": h, "th": thumb_hash},
            )
            new = res.first()
            if new is None:
                existing = (await ctx.session.execute(
                    text("SELECT id FROM samples WHERE project_id = CAST(:pid AS uuid) AND blob_hash = :bh"),
                    {"pid": ctx.project_id, "bh": img_hash},
                )).first()
                sid = str(existing[0]) if existing else sid
            else:
                sid = str(new[0])
            sample_ids.append(sid)

            if not ontology_id:
                continue

            # Resolve annotations for this image
            anns = (
                ann_by_stem.get(img_path.stem)
                or ann_by_filename.get(img_path.name)
                or []
            )
            # Filter to known class_keys only; warn on unknown
            valid_anns = []
            for ann in anns:
                if ann["class_key"] in class_key_map:
                    valid_anns.append(ann)
            if not valid_anns:
                continue

            # revision_no = MAX existing + 1 (1 for new samples)
            rno_row = (await ctx.session.execute(
                text("SELECT COALESCE(MAX(revision_no), 0) + 1 FROM annotation_revisions "
                     "WHERE sample_id = CAST(:s AS uuid)"),
                {"s": sid},
            )).first()
            rno = rno_row[0] if rno_row else 1

            rid = str(uuid.uuid4())
            await ctx.session.execute(
                text(
                    "INSERT INTO annotation_revisions "
                    "(id, project_id, sample_id, ontology_id, ontology_version, "
                    "revision_no, payload, provenance) "
                    "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), CAST(:sid AS uuid), "
                    "CAST(:oid AS uuid), :ov, :rno, CAST(:pl AS jsonb), CAST(:pv AS jsonb))"
                ),
                {
                    "id": rid,
                    "pid": ctx.project_id,
                    "sid": sid,
                    "oid": ontology_id,
                    "ov": ontology_version,
                    "rno": rno,
                    "pl": __import__("json").dumps(valid_anns),
                    "pv": __import__("json").dumps({"source": "import", "review_status": "unreviewed"}),
                },
            )
            revision_ids.append(rid)

        return {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids}
```

Also add `from typing import Any` at the top of the file alongside the existing imports.

- [ ] **Step 2.4: Register the step in `__init__.py`**

```python
# packages/steps/src/cvops_steps/__init__.py
from cvops_api.core.registry import registry
from cvops_steps.extract_frames import ExtractFramesStep
from cvops_steps.auto_label import AutoLabelStep
from cvops_steps.human_review import HumanReviewStep
from cvops_steps.commit_dataset import CommitDatasetStep
from cvops_steps.export_yolo import ExportYoloStep
from cvops_steps.train import TrainStep
from cvops_steps.import_dataset import ImportDatasetStep

def register_all() -> None:
    """Called at API startup to populate the in-memory registry."""
    for step in [
        ExtractFramesStep(),
        AutoLabelStep(),
        HumanReviewStep(),
        CommitDatasetStep(),
        ExportYoloStep(),
        TrainStep(),
        ImportDatasetStep(),
    ]:
        registry.register(step)
```

- [ ] **Step 2.5: Run integration tests**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/steps/test_import_dataset_step.py -v
```
Expected: both tests PASS.

- [ ] **Step 2.6: Run full suite to check no regressions**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/ -q
```
Expected: all tests pass.

- [ ] **Step 2.7: Commit**

```bash
git add packages/steps/src/cvops_steps/import_dataset.py \
        packages/steps/src/cvops_steps/__init__.py \
        services/api/tests/steps/test_import_dataset_step.py
git commit -m "feat: implement ImportDatasetStep.run() and register step"
```

---

### Task 3: API schema + endpoint + mount

**Files:**
- Modify: `services/api/src/cvops_api/schemas/runs.py` (add `ImportRequest`)
- Create: `services/api/src/cvops_api/routers/imports.py`
- Modify: `services/api/src/cvops_api/main.py` (mount router)

**Interfaces:**
- Consumes: `create_adhoc_run`, `advance_workflow` from `engine/dispatch.py` and `engine/coordinator.py`; `get_current_user`, `get_session`, `get_storage`; `RunOut` from `schemas/runs.py`.
- Produces: `POST /api/v1/projects/{id}/imports/upload-url` → `{"upload_url": str}`; `POST /api/v1/projects/{id}/imports` → `RunOut`.

- [ ] **Step 3.1: Add ImportRequest to schemas/runs.py**

Append to the end of `services/api/src/cvops_api/schemas/runs.py`:

```python
class ImportRequest(BaseModel):
    blob_hash: str | None = None
    folder_path: str | None = None
    format: str = "auto"
    ontology_id: uuid.UUID | None = None
    dataset_name: str = "Imported Dataset"
    review: bool = False
```

- [ ] **Step 3.2: Create imports router**

```python
# services/api/src/cvops_api/routers/imports.py
"""POST /projects/{id}/imports — import an existing labeled dataset.

Accepts a zip blob (uploaded via /imports/upload-url) or a server-side folder
path and dispatches an inline import_dataset → [human_review?] → commit_dataset
DAG, exactly like the ad-hoc train endpoint in datasets.py.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cvops_api.core.auth import get_current_user
from cvops_api.core.storage import get_storage
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import User
from cvops_api.db.models.projects import Project
from cvops_api.engine.coordinator import advance_workflow
from cvops_api.engine.dispatch import create_adhoc_run
from cvops_api.schemas.runs import ImportRequest, RunOut

router = APIRouter()


async def _check_project(
    project_id: uuid.UUID,
    user: User,
    session: AsyncSession,
) -> Project:
    r = await session.execute(
        select(Project).where(
            Project.id == project_id,
            Project.org_id == user.org_id,
            Project.deleted_at.is_(None),
        )
    )
    proj = r.scalar_one_or_none()
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


def _public_endpoint(request: Request) -> str:
    import os
    pub = os.environ.get("S3_PUBLIC_ENDPOINT", "")
    if pub:
        return pub
    port = os.environ.get("S3_PUBLIC_PORT", "3900")
    return f"http://{request.url.hostname}:{port}"


@router.post(
    "/projects/{project_id}/imports/upload-url",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def get_import_upload_url(
    project_id: uuid.UUID,
    body: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a presigned PUT URL for uploading a zip blob.

    Body: {"blob_hash": "sha256:..."}
    Response: {"upload_url": "https://..."}
    """
    await _check_project(project_id, current_user, session)
    blob_hash = body.get("blob_hash", "")
    if not blob_hash.startswith("sha256:"):
        raise HTTPException(status_code=422, detail="blob_hash must start with 'sha256:'")
    url = await get_storage().get_presigned_put(
        blob_hash, endpoint=_public_endpoint(request)
    )
    return {"upload_url": url}


@router.post(
    "/projects/{project_id}/imports",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_dataset(
    project_id: uuid.UUID,
    body: ImportRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    """Dispatch an import_dataset → [human_review?] → commit_dataset run."""
    await _check_project(project_id, current_user, session)

    if bool(body.blob_hash) == bool(body.folder_path):
        raise HTTPException(
            status_code=422,
            detail="Exactly one of blob_hash or folder_path must be provided",
        )
    if body.ontology_id is None:
        raise HTTPException(
            status_code=422,
            detail="ontology_id is required (commit_dataset needs it to create the commit)",
        )

    import_config: dict[str, Any] = {"format": body.format}
    if body.ontology_id is not None:
        import_config["ontology_id"] = str(body.ontology_id)

    import_inputs: dict[str, Any] = {}
    if body.blob_hash:
        import_inputs["blob_hash"] = body.blob_hash
    else:
        import_inputs["folder_path"] = body.folder_path

    commit_config: dict[str, Any] = {
        "dataset_name": body.dataset_name,
        "branch_name": "main",
        "message": "Imported dataset",
    }
    if body.ontology_id is not None:
        commit_config["ontology_id"] = str(body.ontology_id)

    if body.review:
        steps = [
            {
                "id": "import",
                "type": "step.import_dataset",
                "config": import_config,
                "inputs": import_inputs,
            },
            {
                "id": "review",
                "type": "step.human_review",
                "config": {},
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.import.outputs.annotation_revision_ids",
                },
            },
            {
                "id": "commit",
                "type": "step.commit_dataset",
                "config": commit_config,
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.review.outputs.annotation_revision_ids",
                },
            },
        ]
        edges = [{"from": "import", "to": "review"}, {"from": "review", "to": "commit"}]
    else:
        steps = [
            {
                "id": "import",
                "type": "step.import_dataset",
                "config": import_config,
                "inputs": import_inputs,
            },
            {
                "id": "commit",
                "type": "step.commit_dataset",
                "config": commit_config,
                "inputs": {
                    "sample_ids": "$steps.import.outputs.sample_ids",
                    "annotation_revision_ids": "$steps.import.outputs.annotation_revision_ids",
                },
            },
        ]
        edges = [{"from": "import", "to": "commit"}]

    definition = {"steps": steps, "edges": edges}
    run = await create_adhoc_run(session, project_id, definition, {}, current_user.id)
    await advance_workflow(session, run.id, current_user.id)
    await session.refresh(run)
    return RunOut.model_validate(run)
```

- [ ] **Step 3.3: Mount the router in main.py**

In `services/api/src/cvops_api/main.py`, add `imports` to the import list:

```python
from cvops_api.routers import (
    auth,
    orgs,
    projects,
    data_sources,
    samples,
    collections,
    tags,
    ontologies,
    datasets,
    workflows,
    runs,
    models,
    training_containers,
    registry as registry_router,
    internal,
    cvat,
    viewer,
    events,
    imports,
)
```

And add the include_router call after the existing ones (before the viewer router):

```python
app.include_router(imports.router, prefix=API_V1, tags=["imports"])
```

- [ ] **Step 3.4: Verify the server starts**

```bash
cd services/api
uvicorn cvops_api.main:app --reload --port 8000 &
sleep 3
curl -s http://localhost:8000/health
kill %1
```
Expected: `{"status": "ok"}` (or similar) — no import errors.

- [ ] **Step 3.5: Commit**

```bash
git add services/api/src/cvops_api/schemas/runs.py \
        services/api/src/cvops_api/routers/imports.py \
        services/api/src/cvops_api/main.py
git commit -m "feat: add POST /projects/{id}/imports endpoint and schema"
```

---

### Task 4: API router tests

**Files:**
- Create: `services/api/tests/routers/test_imports.py`

**Interfaces:**
- Consumes: `imports.router`, `ImportRequest` from `schemas/runs.py`, `ImportDatasetStep` + `CommitDatasetStep` from `cvops_steps`.

- [ ] **Step 4.1: Write the test**

```python
# services/api/tests/routers/test_imports.py
"""Router test for POST /projects/{id}/imports.

Follows the same minimal-app pattern as test_datasets_train.py: mount only the
imports router, override session/current_user, register the real import_dataset
and commit_dataset steps so config validation + queue routing run for real.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cvops_api.core.auth import get_current_user
from cvops_api.core.registry import registry
from cvops_api.db.session import get_session
from cvops_api.db.models.auth import Org, User
from cvops_api.db.models.projects import Project
from cvops_api.db.models.ontologies import Ontology
from cvops_api.db.models.runs import Run
from cvops_api.routers import imports


@pytest_asyncio.fixture
async def factory(postgres_url: str):
    engine = create_async_engine(postgres_url, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


@pytest.fixture
def real_steps():
    from cvops_steps.import_dataset import ImportDatasetStep
    from cvops_steps.commit_dataset import CommitDatasetStep
    from cvops_steps.human_review import HumanReviewStep

    steps = [ImportDatasetStep(), CommitDatasetStep(), HumanReviewStep()]
    for s in steps:
        registry.register(s)
    yield
    for s in steps:
        registry._store.pop(s.type_key, None)


def _client(factory, user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(imports.router)

    async def _get_session_dep():
        async with factory() as sess:
            yield sess

    app.dependency_overrides[get_session] = _get_session_dep
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(factory) -> tuple[User, Project, Ontology]:
    suffix = uuid.uuid4().hex[:8]
    async with factory() as s:
        org = Org(name=f"org-{suffix}")
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, email=f"u-{suffix}@test.com")
        s.add(user)
        project = Project(org_id=org.id, name=f"proj-{suffix}")
        s.add(project)
        await s.flush()
        ont = Ontology(org_id=org.id, name=f"ont-{suffix}", version=1)
        s.add(ont)
        await s.commit()
        await s.refresh(user)
        await s.refresh(project)
        await s.refresh(ont)
        return user, project, ont


async def test_import_creates_run_with_inline_dag(factory, fake_redis, real_steps) -> None:
    user, project, ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={
                "blob_hash": f"sha256:{'a' * 64}",
                "format": "auto",
                "ontology_id": str(ont.id),
                "dataset_name": "My Import",
            },
        )

    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    async with factory() as s:
        parent = await s.get(Run, uuid.UUID(run_id))
        assert parent is not None
        assert parent.workflow_id is None
        definition = parent.config["definition"]
        step_types = {st["id"]: st["type"] for st in definition["steps"]}
        assert step_types == {"import": "step.import_dataset", "commit": "step.commit_dataset"}
        assert definition["edges"] == [{"from": "import", "to": "commit"}]

        children = (
            (await s.execute(select(Run).where(Run.parent_run_id == parent.id)))
            .scalars().all()
        )
        assert len(children) == 1
        child = children[0]
        assert child.step_type == "step.import_dataset"
        assert child.status == "pending"

    assert await fake_redis.xlen("preprocessing") == 1


async def test_import_with_review_inserts_human_review_gate(factory, fake_redis, real_steps) -> None:
    user, project, ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={
                "blob_hash": f"sha256:{'b' * 64}",
                "ontology_id": str(ont.id),
                "dataset_name": "My Import",
                "review": True,
            },
        )

    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    async with factory() as s:
        parent = await s.get(Run, uuid.UUID(run_id))
        definition = parent.config["definition"]
        step_types = {st["id"]: st["type"] for st in definition["steps"]}
        assert step_types == {
            "import": "step.import_dataset",
            "review": "step.human_review",
            "commit": "step.commit_dataset",
        }


async def test_import_both_blob_and_folder_returns_422(factory, real_steps) -> None:
    user, project, _ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={
                "blob_hash": f"sha256:{'c' * 64}",
                "folder_path": "/data/foo",
            },
        )

    assert res.status_code == 422


async def test_import_neither_blob_nor_folder_returns_422(factory, real_steps) -> None:
    user, project, _ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{project.id}/imports",
            json={"dataset_name": "test"},
        )

    assert res.status_code == 422


async def test_import_unknown_project_returns_404(factory, real_steps) -> None:
    user, _project, _ont = await _seed(factory)

    async with _client(factory, user) as c:
        res = await c.post(
            f"/projects/{uuid.uuid4()}/imports",
            json={"blob_hash": f"sha256:{'d' * 64}"},
        )

    assert res.status_code == 404
```

- [ ] **Step 4.2: Run tests**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/routers/test_imports.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 4.3: Run full suite**

```bash
cd services/api
PYTHONPATH="../../packages/steps/src:src" pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 4.4: Commit**

```bash
git add services/api/tests/routers/test_imports.py
git commit -m "test: add router tests for POST /projects/{id}/imports"
```

---

### Task 5: Frontend API hook

**Files:**
- Create: `services/frontend/src/api/imports.ts`

**Interfaces:**
- Produces:
  - `useImportUploadUrl(projectId)` — mutation that POSTs `{blob_hash}` → `{upload_url: string}`
  - `useImportDataset(projectId)` — mutation that POSTs `ImportDatasetRequest` → `RunOut`
  - `ImportDatasetRequest` type

- [ ] **Step 5.1: Create imports.ts**

```typescript
// services/frontend/src/api/imports.ts
import { useMutation } from '@tanstack/react-query'
import { client } from '../lib/client'
import type { RunOut } from './runs'

export interface ImportDatasetRequest {
  blob_hash?: string
  folder_path?: string
  format?: 'auto' | 'yolo' | 'coco' | 'raw'
  ontology_id?: string
  dataset_name?: string
  review?: boolean
}

export function useImportUploadUrl(projectId: string | undefined) {
  return useMutation({
    mutationFn: async (blobHash: string): Promise<{ upload_url: string }> => {
      const { data } = await client.post<{ upload_url: string }>(
        `/projects/${projectId}/imports/upload-url`,
        { blob_hash: blobHash },
      )
      return data
    },
  })
}

export function useImportDataset(projectId: string | undefined) {
  return useMutation({
    mutationFn: async (body: ImportDatasetRequest): Promise<RunOut> => {
      const { data } = await client.post<RunOut>(
        `/projects/${projectId}/imports`,
        body,
      )
      return data
    },
  })
}
```

- [ ] **Step 5.2: Typecheck**

```bash
cd services/frontend
npm run typecheck
```
Expected: no errors.

- [ ] **Step 5.3: Commit**

```bash
git add services/frontend/src/api/imports.ts
git commit -m "feat: add useImportDataset and useImportUploadUrl hooks"
```

---

### Task 6: Frontend page + routing

**Files:**
- Create: `services/frontend/src/pages/ImportDataset.tsx`
- Modify: `services/frontend/src/App.tsx` (add route)
- Modify: `services/frontend/src/components/layout/Sidebar.tsx` (add nav item)

**Interfaces:**
- Consumes: `useImportUploadUrl`, `useImportDataset` from `../api/imports`; `useOntologies` from `../api/ontologies`; `sha256Hex` from `../lib/hash`; `client` from `../lib/client` (for presigned PUT).

- [ ] **Step 6.1: Create ImportDataset.tsx**

```tsx
// services/frontend/src/pages/ImportDataset.tsx
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useOntologies } from '../api/ontologies'
import { useImportUploadUrl, useImportDataset, type ImportDatasetRequest } from '../api/imports'
import { sha256Hex } from '../lib/hash'
import { Button, Field, Input, Select } from '../components/ui'

type Source = 'zip' | 'folder'
type Format = 'auto' | 'yolo' | 'coco' | 'raw'

export default function ImportDataset() {
  const { id: projectId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { data: ontologies } = useOntologies()

  const [source, setSource] = useState<Source>('zip')
  const [file, setFile] = useState<File | null>(null)
  const [folderPath, setFolderPath] = useState('')
  const [format, setFormat] = useState<Format>('auto')
  const [ontologyId, setOntologyId] = useState('')
  const [datasetName, setDatasetName] = useState('Imported Dataset')
  const [review, setReview] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const getUploadUrl = useImportUploadUrl(projectId)
  const importDataset = useImportDataset(projectId)

  const canSubmit =
    !uploading &&
    !importDataset.isPending &&
    datasetName.trim().length > 0 &&
    ontologyId.length > 0 &&
    (source === 'zip' ? file !== null : folderPath.trim().length > 0)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const body: ImportDatasetRequest = {
        format,
        dataset_name: datasetName.trim() || 'Imported Dataset',
        review,
        ...(ontologyId ? { ontology_id: ontologyId } : {}),
      }

      if (source === 'zip' && file) {
        setUploading(true)
        const hex = await sha256Hex(file)
        const blobHash = `sha256:${hex}`
        const { upload_url } = await getUploadUrl.mutateAsync(blobHash)
        const put = await fetch(upload_url, { method: 'PUT', body: file })
        if (!put.ok) throw new Error(`Upload failed: ${put.status}`)
        setUploading(false)
        body.blob_hash = blobHash
      } else {
        body.folder_path = folderPath.trim()
      }

      const run = await importDataset.mutateAsync(body)
      navigate(`/runs/${run.id}`)
    } catch (err) {
      setUploading(false)
      setError(err instanceof Error ? err.message : 'Import failed')
    }
  }

  return (
    <div className="max-w-lg mx-auto py-10 px-4">
      <h1 className="text-xl font-semibold text-text-primary mb-6">Import Dataset</h1>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Source */}
        <fieldset className="space-y-3">
          <legend className="text-sm font-medium text-text-primary">Source</legend>
          <div className="flex gap-4">
            <label className="flex items-center gap-2 cursor-pointer text-sm text-text-secondary">
              <input
                type="radio"
                name="source"
                value="zip"
                checked={source === 'zip'}
                onChange={() => setSource('zip')}
                className="accent-iris"
              />
              Upload zip
            </label>
            <label className="flex items-center gap-2 cursor-pointer text-sm text-text-secondary">
              <input
                type="radio"
                name="source"
                value="folder"
                checked={source === 'folder'}
                onChange={() => setSource('folder')}
                className="accent-iris"
              />
              Folder path
            </label>
          </div>

          {source === 'zip' ? (
            <Field label="Zip file" htmlFor="zip-file">
              <input
                id="zip-file"
                type="file"
                accept=".zip"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                className="text-sm text-text-secondary file:mr-3 file:rounded-lg file:border-0
                  file:bg-surface-3 file:px-3 file:py-1.5 file:text-sm file:text-text-primary
                  file:cursor-pointer hover:file:bg-iris/20"
              />
            </Field>
          ) : (
            <Field label="Folder path" htmlFor="folder-path">
              <Input
                id="folder-path"
                placeholder="/data/my-dataset"
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
              />
            </Field>
          )}
        </fieldset>

        {/* Format & Ontology */}
        <div className="space-y-4">
          <Field label="Format" htmlFor="format">
            <Select
              id="format"
              value={format}
              onChange={(e) => setFormat(e.target.value as Format)}
            >
              <option value="auto">Auto-detect</option>
              <option value="yolo">YOLO</option>
              <option value="coco">COCO</option>
              <option value="raw">Raw images (no labels)</option>
            </Select>
          </Field>

          <Field label="Ontology" htmlFor="ontology">
            <Select
              id="ontology"
              value={ontologyId}
              onChange={(e) => setOntologyId(e.target.value)}
            >
              <option value="">Select an ontology…</option>
              {(ontologies ?? []).map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name} (v{o.version})
                </option>
              ))}
            </Select>
            {format === 'raw' && (
              <p className="mt-1 text-xs text-text-muted">Raw images have no annotations to map.</p>
            )}
          </Field>
        </div>

        {/* Options */}
        <div className="space-y-4">
          <Field label="Dataset name" htmlFor="dataset-name">
            <Input
              id="dataset-name"
              value={datasetName}
              onChange={(e) => setDatasetName(e.target.value)}
              placeholder="Imported Dataset"
            />
          </Field>

          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={review}
              onChange={(e) => setReview(e.target.checked)}
              className="h-4 w-4 rounded accent-iris"
            />
            <span className="text-sm text-text-secondary">
              Route through CVAT for human review before committing
            </span>
          </label>
        </div>

        {error && (
          <p className="text-sm text-error">{error}</p>
        )}

        <Button
          type="submit"
          disabled={!canSubmit}
          loading={uploading || importDataset.isPending}
        >
          {uploading ? 'Uploading…' : importDataset.isPending ? 'Dispatching…' : 'Start import'}
        </Button>
      </form>
    </div>
  )
}
```

- [ ] **Step 6.2: Add route in App.tsx**

Add the import at the top with the other page imports:
```typescript
import ImportDataset from './pages/ImportDataset'
```

Add the route inside the `<Route element={<RequireAuth><Layout /></RequireAuth>}>` block:
```tsx
<Route path="/projects/:id/import" element={<ImportDataset />} />
```
Place it after the `/projects/:id/training-containers` route.

- [ ] **Step 6.3: Add nav item in Sidebar.tsx**

In the `items` array inside `ProjectNav`, add after the `Workflows` entry:

```typescript
{ to: `/projects/${projectId}/import`,           label: 'Import',       end: false },
```

Full updated `items` array:
```typescript
const items = [
  { to: `/projects/${projectId}`,              label: 'Dashboard',    end: true  },
  { to: `/projects/${projectId}/data-sources`, label: 'Data Sources', end: false },
  { to: `/projects/${projectId}/samples`,      label: 'Samples',      end: false },
  { to: `/projects/${projectId}/datasets`,     label: 'Datasets',     end: false },
  { to: `/projects/${projectId}/workflows`,    label: 'Workflows',    end: false },
  { to: `/projects/${projectId}/import`,       label: 'Import',       end: false },
  { to: `/projects/${projectId}/runs`,         label: 'Runs',         end: false },
  { to: `/projects/${projectId}/models`,       label: 'Models',       end: false },
  { to: `/projects/${projectId}/training-containers`, label: 'Training', end: false },
  { to: `/projects/${projectId}/settings`,     label: 'Settings',     end: false },
]
```

- [ ] **Step 6.4: Typecheck and lint**

```bash
cd services/frontend
npm run typecheck
npm run lint
```
Expected: no errors, no warnings.

- [ ] **Step 6.5: Commit**

```bash
git add services/frontend/src/pages/ImportDataset.tsx \
        services/frontend/src/App.tsx \
        services/frontend/src/components/layout/Sidebar.tsx
git commit -m "feat: add ImportDataset page, route, and sidebar nav item"
```
