# 16 · Dataset Import — getting pre-labelled data in

**Status:** Current (2026-10-10). Describes shipped behaviour.

The normal ingest path is video/images → `extract_frames` → CVAT labelling →
commit. If you already have labels (YOLO, COCO), there are two ways in:

| Path | Entry point | Use when |
|---|---|---|
| **Import run** | UI *Import* page / `POST /projects/{id}/imports` | You have a dataset folder or zip (YOLO / COCO / raw). Runs as a workflow; can gate on CVAT review; ends in a commit. |
| **Annotated upload** | `POST /projects/{id}/annotated-uploads/confirm` | A programmatic client already has the images + YOLO boxes in memory. Synchronous; strict class validation; no commit. |

**Not supported:** Pascal VOC (XML), segmentation masks/polygons (COCO `segmentation`
is ignored — boxes only), keypoints.

---

## 1. Import run (UI)

Sidebar → **Import** (`/projects/:id/import`), or **Import** on the Datasets page
(`ImportDatasetDialog`, same form plus an optional commit message).

1. **Source** — one of two tabs:
   - **Upload** — *Select zip* (`.zip`) or *Select folder*. A folder is zipped in
     the browser (`fflate`) first. The client SHA-256s the zip, gets a presigned
     PUT from `POST /projects/{id}/imports/upload-url` (`{blob_hash}` →
     `{upload_url}`), and PUTs straight to Garage.
   - **Server folder path** — an absolute path (e.g. `/data/my-dataset`) read by the
     **worker** at run time. See the [caveat](#server-path-caveat).
2. **Dataset name** — the dataset the commit lands in (created if missing; branch `main`).
3. **Label set (ontology)** — required. Class names in your data must equal its
   `class_key`s exactly.
4. **Format** — `Auto-detect` / `YOLO` / `COCO` / `Raw images`.
5. **Split** — Train % / Val % (Test = remainder). UI default 70 / 15 / 15.
   Ignored when the data already has split folders (see [Splits](#splits)).
6. **Send to CVAT for human review before committing** — inserts a `human_review` gate.

Submit dispatches the run and navigates to the project's Runs page.

## 2. Import run (API)

```http
POST /api/v1/projects/{id}/imports
{
  "blob_hash": "sha256:…",          // exactly one of blob_hash | folder_path
  "folder_path": null,
  "format": "auto",                 // auto | yolo | coco | raw
  "ontology_id": "<uuid>",          // required (422 without it)
  "dataset_name": "Imported Dataset",
  "commit_message": "Imported dataset",
  "review": false,
  "split_strategy": {"train_ratio": 0.8, "val_ratio": 0.2}   // API default 0.8 / 0.2
}
→ 201 RunOut
```

It builds an ad-hoc DAG and calls `advance_workflow` in-request:

```
import_dataset ──► [human_review] ──► commit_dataset
```

## 3. Formats and expected layout

A zip with a single top-level directory is descended into. Images are any
`*.jpg|*.jpeg|*.png|*.bmp` anywhere under the root.

**Auto-detect** order: `data.yaml` at root → YOLO; a `labels/` dir at root or under
`train|val|valid|test/` → YOLO; any `*.json` containing `"categories"` → COCO;
otherwise raw.

**YOLO**

```
my-dataset/
  data.yaml              # names: [car, person]  or  {0: car, 1: person}
  images/train/a.jpg
  images/val/b.jpg
  labels/train/a.txt     # <class_id> <cx> <cy> <w> <h>, normalised 0..1
  labels/val/b.txt
```

- Labels are read from `<root>/labels/**/*.txt` and matched to images **by file stem**.
- `class_id` indexes `data.yaml` `names`; with no `data.yaml`, it indexes the
  ontology's classes in `sort_order`.
- The split-first layout (`train/images`, `train/labels`, as Roboflow exports) is
  *detected* as YOLO but its labels are **not read** (only `<root>/labels/` is
  scanned). Re-arrange to `images/<split>` + `labels/<split>` first.
- Stems must be unique across splits (`images/train/a.jpg` and `images/val/a.jpg`
  collide).

**COCO** — the first `*.json` with a `categories` key. Boxes (`bbox`, absolute
`x,y,w,h`) are converted to normalised centre form. Matched to images by
**bare filename** (`file_name` must not carry a directory). Category `name` must
equal the `class_key`.

**Raw** — images only, no annotations. See the [commit caveat](#what-gets-created).

## 4. What gets created

`import_dataset` (`packages/steps/src/cvops_steps/import_dataset.py`, `preprocessing` queue):

- a synthetic `data_sources` row (`type='import'`, `status='ingested'`);
- per image: the blob + a 256px thumbnail blob, and a `samples` row. An image
  already in the project (same SHA-256) reuses its sample;
- per image with at least one known-class box: one `annotation_revisions` row
  (`revision_no` = next for that sample, provenance
  `{"source": "import", "review_status": "unreviewed"}`);
- outputs `sample_ids`, `annotation_revision_ids`, optional `splits`, and
  `import_stats` (`format`, `images`, `labels_matched`, `class_mismatch`,
  `annotations_created`, YAML names vs ontology keys) — check these in the run
  detail when counts look wrong.

`commit_dataset` then commits **only samples that have a revision**; the rest are
skipped. So a `raw` import (or one where no class matched) without the review gate
fails with *"no sample has an annotation revision … nothing to commit"*. The
samples still exist and can be labelled later.

With **review on**, `human_review` pushes the samples (and imported boxes) to CVAT;
the run waits (`waiting`) until the gate is resolved, then commits the reviewed
revisions.

### Splits

If **every** image sits under a `train` / `val|valid|validation` / `test` path
segment, those folder splits are used as-is. Otherwise (none, or a mix) the
`train_ratio` / `val_ratio` split strategy assigns them.

### Unknown classes

- **Import run:** boxes whose class is not in the ontology are **dropped silently**;
  an image whose boxes are all unknown gets no revision. Only `import_stats.class_mismatch`
  tells you. YOLO `class_id`s beyond `names` are dropped too.
- **Annotated upload:** the whole batch is rejected with **422** (see below).

Auto-creating missing classes on import is tracked in
[#138](https://github.com/Hozi-AI/CVOps/issues/138).

### Server-path caveat

`folder_path` is opened by the worker process with **no allow-list or sandbox** —
any path readable by the worker can be imported by any project member. Treat it as a
trusted-operator feature on dev boxes. Tracked in
[#185](https://github.com/Hozi-AI/CVOps/issues/#185).

---

## 5. Annotated upload (programmatic YOLO)

For clients that parse YOLO themselves. Synchronous, no workflow, no commit.

1. **Presign** — `POST /api/v1/projects/{id}/image-uploads/presign`
   ```json
   {"items": [{"filename": "a.jpg", "content_type": "image/jpeg", "sha256": "sha256:<hex>"}]}
   ```
   → `{"items": [{"filename", "blob_hash", "put_url"}]}`. PUT each image to its `put_url`.
2. **Confirm** — `POST /api/v1/projects/{id}/annotated-uploads/confirm`
   ```json
   {
     "ontology_id": null,                  // null → project's default ontology
     "class_names": ["car", "person"],     // ordered, as in classes.txt / data.yaml
     "group": "batch-2026-10-10",          // optional; defaults to "Upload <ts>"
     "items": [{
       "blob_hash": "sha256:<hex>", "width": 1920, "height": 1080,
       "content_type": "image/jpeg", "size_bytes": 123456,
       "boxes": [{"class_id": 0, "cx": 0.5, "cy": 0.5, "w": 0.2, "h": 0.1, "confidence": null}]
     }]
   }
   ```
   → `201 {"source_id", "created", "annotated", "sample_ids"}`. Max 1000 items per call.

Behaviour (`services/api/src/cvops_api/core/annotation_import.py`):

- Validates the whole batch first: a `class_id` outside `class_names`, or a name
  not in the ontology, → **422**, nothing persisted.
- New images become samples under the project's uploads source, each with one
  revision (provenance `{"source": "import:yolo", "review_status": "unreviewed"}`).
- An image already in the project keeps its sample and gets **no** extra revision.
- 422 if no `ontology_id` and the project has no default ontology.

To commit, call `POST /api/v1/datasets/{id}/commits/from-samples` with the returned
`sample_ids` (it resolves each sample's latest revision).
