> **Archived 2026-10-10.** Superseded by the step implementations in [`packages/steps`](../../packages/steps/src/cvops_steps/) (`extract_frames.py`); the standalone CLI prototype was removed (commit c6c5b85). Kept for history; do not treat as current.

# extract_frames.py

**Step 1 of 3 in the YOLO workflow**

Extracts frames from video files into a named session folder.

---

## What it does

1. Scans `./videos` for supported video files
2. Asks the user for a session name (used as the folder name and filename prefix)
3. Asks for an extraction interval: time-based (seconds) or frame-based (every N frames)
4. Extracts frames from all videos and saves them as JPEG files

**Output structure:**
```
frames/
  <session_name>/
    <session_name>_1.jpg
    <session_name>_2.jpg
    ...
```

---

## Usage

```bash
cd tools/frame-extractor
python extract_frames.py
```

The script prompts interactively:

| Prompt | Example |
|--------|---------|
| Session name | `test`, `highway_run`, `run1` |
| Interval mode | `1` = time-based, `2` = frame-based |
| Interval value | `1.0` seconds / `10` frames |

---

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `input_dir` | `./videos` | Folder containing input videos |
| `base_output_dir` | `./frames` | Root folder for extracted frames |
| `SUPPORTED_EXTENSIONS` | mp4, avi, mov, mkv, wmv, flv, webm | Accepted video formats |

---

## Notes

- If multiple videos exist in `./videos`, all are processed in alphabetical order and frames are numbered continuously across videos
- Frame filenames start at index 1 (not 0)
- Spaces in the session name are automatically replaced with underscores