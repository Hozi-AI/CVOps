# Dataset Importer Design

**Date:** 2026-07-07
**Status:** Approved

## Problem

The only way to get data into CVOps today is the raw upload flow: video/image → extract frames → CVAT labeling → commit. Users with existing labeled datasets (YOLO, COCO, or raw images) have no path in.

## Solution

Option C — inline ad-hoc DAG dispatched from a dedicated import endpoint, consistent with the `POST /datasets/{id}/commits/{id}/train` pattern already in the codebase.

---

## Step: `step.import_dataset`

**Location:** `packages/steps/src/cvops_steps/import_dataset.py`
**Queue:** `preprocessing` (same as `extract_frames`)
**Registered in:** `cvops_steps.register_all()`

### Config

| Field | Type | Required | Description |
|---|---|---|---|
| `format` | `"auto"\|"yolo"\|"coco"\|"raw"` | No (default `"auto"`) | Dataset format. Auto-detect inspects for `data.yaml` (YOLO) or a JSON with a `categories` key (COCO); falls back to raw images. |
| `ontology_id` | UUID string | No | If omitted, images are ingested as samples but annotations are skipped. |

### Inputs

| Field | Description |
|---|---|
| `blob_hash` | SHA-256 hash of an uploaded zip file (MinIO). Mutually exclusive with `folder_path`. |
| `folder_path` | Absolute path on the worker host filesystem. Mutually exclusive with `blob_hash`. Requires the path to be mounted into the `worker-preprocessing` container (e.g. via a `volumes:` entry in `docker-compose.yml`). |

Exactly one of `blob_hash` / `folder_path` must be present; the step raises an error otherwise.

### Execution

1. **Source resolution:** If `blob_hash`, download the zip from MinIO and extract to a temp dir. If `folder_path`, read directly.
2. **Format detection:** If `format == "auto"`, inspect directory for `data.yaml` → YOLO; `.json` containing `categories` key → COCO; neither → raw.
3. **Image ingestion:** Walk all image files (`.jpg`, `.jpeg`, `.png`, `.bmp`). For each: SHA-256 hash → upsert `Blob` row → insert `Sample` row. Same logic as `extract_frames`.
4. **Annotation ingestion:** If `ontology_id` is set:
   - Load the ontology's label classes from DB.
   - YOLO: read `data.yaml` for class name list; for each `.txt` label file, map class index → label UUID by name.
   - COCO: read `annotations.json`; map `category_id` → label UUID by `name` field.
   - Unmatched class names are skipped with a warning written to the run log (not a failure).
   - Write one `annotation_revision` per sample that has at least one annotation (`provenance = "import"`).
5. **Output:** Return `sample_ids` and `annotation_revision_ids` (same shape as CVAT sync output).

### Outputs

| Field | Description |
|---|---|
| `sample_ids` | List of UUIDs of created/upserted samples |
| `annotation_revision_ids` | List of UUIDs of created annotation revisions (empty if no ontology or no annotations matched) |

---

## API Endpoint

```
POST /api/v1/projects/{id}/imports
```

**Auth:** standard `get_current_user` JWT dependency.

### Request Body

```json
{
  "blob_hash": "sha256:abc...",
  "folder_path": "/data/my-dataset",
  "format": "auto",
  "ontology_id": "uuid...",
  "dataset_name": "My Dataset",
  "review": false
}
```

- Exactly one of `blob_hash` / `folder_path` required — 422 otherwise.
- `format` defaults to `"auto"`.
- `ontology_id` optional — if omitted, annotations are skipped.
- `dataset_name` defaults to `"Imported Dataset"`. `commit_dataset` creates the dataset if it doesn't exist.
- `review` defaults to `false`.

### DAG Built

```
import_dataset → [human_review?] → commit_dataset
```

- `commit_dataset` config: `dataset_name` from request body (default `"Imported Dataset"`), `branch_name = "main"`, `message = "Imported dataset"`, `ontology_id` from request body.
- If `review = true`, a `human_review` gate is inserted between `import_dataset` and `commit_dataset`, wired via `$steps.import_dataset.outputs.*`.
- If `review = false`, `commit_dataset` is chained directly after `import_dataset`.

### Response

```json
{ "run_id": "uuid..." }
```

Frontend polls `GET /runs/{run_id}` for progress — no new polling endpoint needed.

---

## Frontend

**New page:** `/projects/:projectId/import`
**Link:** Project sidebar, below "Workflows".

### Form (single page, three groups)

**1. Source**
- Radio: "Upload zip" / "Folder path"
- Upload zip: file picker → client SHA-256 hash → presigned PUT to MinIO → store hash in state (same pattern as `CommitDetail.tsx` train form)
- Folder path: plain text input

**2. Format & Ontology**
- Format select: `Auto-detect` / `YOLO` / `COCO` / `Raw images`
- Ontology picker: dropdown of project ontologies (optional). If `Raw images` selected, greyed out with note "Raw images have no annotations to map."

**3. Options**
- Text input: "Dataset name" (defaults to `"Imported Dataset"`)
- Checkbox: "Route through CVAT for human review before committing"

**Submit:** `POST /projects/{id}/imports` → on success, redirect to `/runs/{run_id}` (existing `RunDetail` page).

---

## What Is Not In Scope

- Perceptual deduplication of imported images against existing samples (exact-hash dedup via `Blob` upsert is sufficient).
- Importing split information (train/val/test) from `data.yaml` — CVOps manages splits at commit time.
- Creating a new ontology from dataset class names — user must pick an existing ontology.
- Per-frame video import — zip must contain extracted image files, not video.
