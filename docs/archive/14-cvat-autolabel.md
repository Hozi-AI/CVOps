> **Archived 2026-10-10.** Superseded by the step implementations in [`packages/steps`](../../packages/steps/src/cvops_steps/) (`auto_label.py`); the standalone CLI prototype was removed (commit 64460d7), CVAT sync is `worker-cvat`. Kept for history; do not treat as current.

# cvat_autolabel.py

**Current Step 2 — upload frames to CVAT and auto-label with YOLO12n**

Uploads a folder of extracted frames to a running CVAT instance, creates a new task,
and triggers automatic annotation using the YOLO12n model deployed inside CVAT via Nuclio.
Every run creates a new task — existing tasks are never modified or overwritten.

---

## Prerequisites

The CVAT environment must be running before this script is used.
If it is not, run the startup script first:

```bash
bash scripts/start_env.sh
```

See [start_env.sh](../../scripts/start_env.sh) for details on what the startup script does.

---

## Usage

```bash
cd tools/frame-extractor
python3 cvat_autolabel.py frames/<session_name>
```

With a custom confidence threshold:

```bash
python3 cvat_autolabel.py frames/<session_name> --threshold 0.2
```

### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `folder` | yes | — | Path to the folder containing frame images |
| `--threshold` | no | `0.3` | Confidence threshold (0.0–1.0) |

---

## What it does

1. Reads all `.jpg` / `.jpeg` / `.png` images from the given folder
2. Connects to CVAT and creates a new task named after the folder
3. Creates the task with all 80 COCO class labels
4. Uploads the images to the task
5. Sends an auto-annotation request to the YOLO12n Nuclio function
6. Polls until the annotation job finishes
7. Prints a direct link to the completed task in CVAT

**Example output:**
```
[*] Folder     : frames/yyyy
[*] Images     : 20
[*] Task name  : yyyy
[*] Threshold  : 0.3
[*] CVAT       : http://localhost:8080

[*] Creating task...
[✓] Task 'yyyy' created (ID=12)
[*] Uploading 20 images...
[✓] Upload complete
[*] Starting auto-annotation with YOLO12n...
[*] Waiting for results.............. done ✓

═════════════════════════════════════════════
  Task      : yyyy  (ID=12)
  Threshold : 0.3
  Open at   : http://localhost:8080/tasks/12/jobs/11
═════════════════════════════════════════════
```

---

## Configuration

Connection settings are read from environment variables with sensible defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `CVAT_HOST` | `http://localhost` | CVAT server address |
| `CVAT_PORT` | `8080` | CVAT server port |
| `CVAT_USERNAME` | `admin` | CVAT account username |
| `CVAT_PASSWORD` | `Admin1234!` | CVAT account password |

Override via environment:
```bash
CVAT_PASSWORD=mypassword python3 cvat_autolabel.py frames/yyyy
```

---

## Confidence threshold

| Threshold | Effect |
|-----------|--------|
| `0.5` | Fewer detections, higher certainty (CVAT default) |
| `0.3` | Recommended — good balance for most footage |
| `0.1–0.2` | Many detections, more false positives |

> **Note:** The YOLO12n model was trained on ground-level COCO imagery.
> For aerial or drone footage, lower thresholds (0.2–0.3) tend to work better
> since objects are small and confidence scores are generally lower.

---

## Model

The script uses the `pth-ultralytics-yolo12n` function deployed in Nuclio,
which runs the YOLO12n (nano) model with 80 COCO classes.
The model file (`yolo12n.pt`) is baked into the `cvat/yolo12n-serverless:latest`
Docker image and does not need to be present on disk at runtime.

Original model file: `models/yolo12n (1).pt`

---

## Requirements

- `cvat-sdk` must be installed (`pip install cvat-sdk`)
- CVAT must be running (see `scripts/start_env.sh`)
- The `pth-ultralytics-yolo12n` Nuclio function must be deployed and healthy
- Must be run after `extract_frames.py`