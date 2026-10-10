> **Archived 2026-10-10.** Superseded by the step implementations in [`packages/steps`](../../packages/steps/src/cvops_steps/) (`labeling_backends/` + `human_review.py`); the standalone CLI prototype was removed (commit 64460d7), CVAT sync is `worker-cvat`. Kept for history; do not treat as current.

# upload_to_cvat.py

**Step 3 of 3 in the YOLO workflow**

Uploads extracted frames and auto-generated labels to a local CVAT instance as a new task with pre-annotations.

---

## What it does

1. Lists available session folders and asks the user to select one
2. Reads all label files to auto-detect which COCO classes were found
3. Asks for the CVAT password
4. Creates a new CVAT task with the session name and detected labels
5. Uploads all images to the task
6. Packages the label files into a zip archive and imports them as pre-annotations in **Ultralytics YOLO Detection 1.0** format

---

## Usage

```bash
cd tools/frame-extractor
python upload_to_cvat.py
```

The script prompts interactively:

| Prompt | Example |
|--------|---------|
| Session to upload | select from numbered list |
| CVAT password | your CVAT account password |

After a successful run, the terminal prints a direct link to the new task:
```
Open CVAT  : http://localhost:8080/tasks/<task_id>/jobs
```

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CVAT_HOST` | `http://localhost` | CVAT server address |
| `CVAT_PORT` | `8080` | CVAT server port |
| `CVAT_USERNAME` | `Nati` | CVAT account username |

---

## Class detection

The script automatically reads class IDs from all `.txt` label files in the session and maps them to COCO class names using a built-in lookup table. No manual label entry is required.

Example: if the labels contain class IDs `2` and `8`, the CVAT task is created with labels `car` and `boat`.

---

## Zip structure for annotation import

CVAT's datumaro importer requires both images and label files inside the zip to correctly map annotations to frames:

```
data.yaml          ← class name mapping
train/
  images/          ← frame images (required by datumaro for filename matching)
  labels/          ← YOLO .txt label files
```

---

## Requirements

- `cvat-sdk` must be installed (`pip install cvat-sdk`)
- CVAT must be running at the configured host/port
- Must be run after both `extract_frames.py` and `auto_label.py`