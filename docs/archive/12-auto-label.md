> **Archived 2026-10-10.** Superseded by the step implementations in [`packages/steps`](../../packages/steps/src/cvops_steps/) (`auto_label.py`); the standalone CLI prototype was removed (commit 64460d7). Kept for history; do not treat as current.

# auto_label.py

> **This script is a legacy option and is no longer part of the active workflow.**
> It is kept as a fallback for offline environments where CVAT is not available.
> The current approach is to run auto-labeling directly inside CVAT using a deployed
> Nuclio function — see [14-cvat-autolabel.md](14-cvat-autolabel.md).

---

**Legacy Step 2 of 3 — local inference only**`

Runs YOLO12 nano locally on extracted frames and generates YOLO-format label files (`.txt`) for each image.

---

## What it does

1. Lists available session folders inside `./frames`
2. Asks the user to select a session and set a confidence threshold
3. Runs YOLO12n inference on every image in the session
4. Saves one `.txt` label file per image in YOLO format

**Output structure:**
```
labels/
  <session_name>/
    <session_name>_1.txt
    <session_name>_2.txt
    ...
```

Each `.txt` file contains one detection per line:
```
<class_id> <x_center> <y_center> <width> <height>
```
All coordinates are normalized (0.0–1.0). Files are empty when no objects are detected.

---

## Usage

```bash
cd tools/frame-extractor
python auto_label.py
```

The script prompts interactively:

| Prompt | Example |
|--------|---------|
| Session to label | select from numbered list |
| Confidence threshold | `0.25` (default), range `0.0–1.0` |

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DEFAULT_CONFIDENCE` | `0.25` | Minimum detection confidence |
| Model file | `yolo12n.pt` | Must be present in `tools/frame-extractor/` |

---

## When to use this script

Use `auto_label.py` only when:
- CVAT is not running and cannot be started
- You need a quick offline check of what the model detects
- You want raw YOLO `.txt` files for a pipeline that does not use CVAT

In all other cases, use `cvat_autolabel.py` instead.

---

## Notes

- Requires `ultralytics` to be installed (`pip install ultralytics`)
- Must be run after `extract_frames.py`
- Higher confidence = fewer but more reliable detections
- Lower confidence = more detections but potentially noisier results
