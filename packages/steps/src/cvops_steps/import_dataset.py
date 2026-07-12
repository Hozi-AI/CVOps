"""import_dataset — ingest an existing labeled dataset (YOLO, COCO, or raw images).

Accepts either a zip blob (downloaded from MinIO) or a server-side folder path.
Creates blobs, samples, and annotation_revisions (provenance='import'), then
outputs sample_ids + annotation_revision_ids so downstream steps
(human_review, commit_dataset) can chain directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

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
        import asyncio
        import hashlib
        import io
        import shutil
        import tempfile
        import uuid
        import zipfile

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

        # ── 1. Resolve source directory ────────────────────────────────────
        _tmpdir = None
        if blob_hash:
            zip_bytes = await ctx.storage.get_bytes(blob_hash)
            _tmpdir = tempfile.mkdtemp()
            root = Path(_tmpdir)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(root)
            # If the zip contained a single top-level directory, descend into it.
            entries = list(root.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                root = entries[0]
        else:
            root = Path(folder_path)  # type: ignore[arg-type]

        try:
            return await self._ingest(ctx, root, ontology_id, fmt, settings, StorageBackend)
        finally:
            if _tmpdir:
                shutil.rmtree(_tmpdir, ignore_errors=True)

    async def _ingest(
        self,
        ctx: StepContext,
        root: Path,
        ontology_id: str | None,
        fmt: str,
        settings: object,
        StorageBackend: object,
    ) -> dict:
        import asyncio
        import hashlib
        import json
        import uuid

        import cv2
        import numpy as np
        import yaml
        from sqlalchemy import text

        # ── 2. Format detection ────────────────────────────────────────────
        if fmt == "auto":
            fmt = detect_format(root)

        # ── 3. Load ontology label classes ────────────────────────────────
        class_key_map: dict[str, str] = {}
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
                data_yaml = root / "data.yaml"
                class_names: list[str] = []
                if data_yaml.exists():
                    doc = yaml.safe_load(data_yaml.read_text())
                    class_names = doc.get("names", [])
                ann_by_stem = parse_yolo(root, class_names)
            elif fmt == "coco":
                ann_by_filename = parse_coco(root)

        # ── 5. Create a synthetic data_source row to satisfy the FK ─────────
        ds_id = str(uuid.uuid4())
        await ctx.session.execute(
            text(
                "INSERT INTO data_sources (id, project_id, type, status) "
                "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), 'import', 'ingested')"
            ),
            {"id": ds_id, "pid": ctx.project_id},
        )

        # ── 6. Walk images, create blobs + samples ────────────────────────
        def _process_image(img_bytes: bytes) -> tuple[bytes, int, int]:
            arr = np.frombuffer(img_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return img_bytes, 0, 0
            h, w = img.shape[:2]
            scale = 256 / max(h, w)
            thumb = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))))
            ok, buf = cv2.imencode(".jpg", thumb)
            thumb_bytes = buf.tobytes() if ok else img_bytes
            return thumb_bytes, w, h

        async def _register_blob(h: str, data: bytes, mt: str) -> None:
            await ctx.session.execute(
                text(
                    "INSERT INTO blobs (hash, storage_backend, storage_key, size_bytes, media_type) "
                    "VALUES (:h, :sb, :sk, :sz, :mt) ON CONFLICT (hash) DO NOTHING"
                ),
                {
                    "h": h,
                    "sb": settings.S3_BACKEND,  # type: ignore[attr-defined]
                    "sk": StorageBackend._bucket_key(h),  # type: ignore[attr-defined]
                    "sz": len(data),
                    "mt": mt,
                },
            )

        image_files = sorted(
            p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTS
        )

        sample_ids: list[str] = []
        revision_ids: list[str] = []

        for img_path in image_files:
            img_bytes = img_path.read_bytes()
            img_hash = "sha256:" + hashlib.sha256(img_bytes).hexdigest()

            thumb_bytes, w, h = await asyncio.to_thread(_process_image, img_bytes)

            await ctx.storage.save_bytes(img_bytes, "image/jpeg")
            thumb_hash = await ctx.storage.save_bytes(thumb_bytes, "image/jpeg")
            await _register_blob(img_hash, img_bytes, "image/jpeg")
            await _register_blob(thumb_hash, thumb_bytes, "image/jpeg")

            sid = str(uuid.uuid4())
            res = await ctx.session.execute(
                text(
                    "INSERT INTO samples (id, project_id, blob_hash, source_id, width, height, thumbnail_hash) "
                    "VALUES (CAST(:id AS uuid), CAST(:pid AS uuid), :bh, CAST(:src AS uuid), :w, :h, :th) "
                    "ON CONFLICT (project_id, blob_hash) DO NOTHING RETURNING id"
                ),
                {"id": sid, "pid": ctx.project_id, "bh": img_hash, "src": ds_id, "w": w, "h": h, "th": thumb_hash},
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

            anns = ann_by_stem.get(img_path.stem) or ann_by_filename.get(img_path.name) or []
            valid_anns = [a for a in anns if a["class_key"] in class_key_map]
            if not valid_anns:
                continue

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
                    "pl": json.dumps(valid_anns),
                    "pv": json.dumps({"source": "import", "review_status": "unreviewed"}),
                },
            )
            revision_ids.append(rid)

        return {"sample_ids": sample_ids, "annotation_revision_ids": revision_ids}
