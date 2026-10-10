# ICD — Step Contract (shared library)

**Owner:** Itai
**Last updated:** 2026-06-11

---

## What it is

`packages/steps` is a **Python shared library**, not a running service. It contains every step implementation and registers them in the registry at startup. All three worker services import it.

The Step ABC (abstract base class) is the single contract every step must implement. The executor never knows what a step does — it only calls `step.run()`.

---

## Step Base Class

Location: `packages/api/src/cvops_api/engine/step.py`

```python
@dataclass
class StepContext:
    session:    AsyncSession      # DB session — read/write PG
    storage:    StorageBackend    # MinIO abstraction — read/write blobs
    project_id: str               # UUID
    run_id:     str               # UUID of this step's runs row
    actor_id:   str               # UUID of the user who triggered the workflow
    audit:      Callable          # bound emit_event(action, payload) coroutine

class GateException(Exception):
    """
    Raised by gate steps to pause the workflow.
    gate_data is stored in runs.output_refs.
    The workflow run transitions to status = 'waiting'.
    """
    def __init__(self, gate_data: dict):
        self.gate_data = gate_data

class Step:
    type_key:      str  = ""     # registered key, e.g. "step.extract_frames"
    queue:         str  = ""     # "preprocessing" | "labeling" | "training"
    config_schema: dict = {}     # JSON Schema — validated before run; drives UI config form
    is_gate:       bool = False  # True → raises GateException to pause workflow
    ui_hints:      dict = {}
    # ui_hints shape:
    # {
    #   "group":       "Data Preprocessing",  ← palette section in workflow builder
    #   "icon":        "video",               ← icon name
    #   "description": "Human-readable description shown in palette",
    #   "order":       1                      ← sort position within the group
    # }

    async def run(
        self,
        ctx:    StepContext,
        config: dict,            # validated against config_schema before this is called
        inputs: dict,            # resolved artifact references from prior steps
    ) -> dict:                   # output artifact references passed to next steps
        raise NotImplementedError

    def idempotency_key(self, config: dict, inputs: dict) -> str:
        """
        SHA-256 of (type_key + config + inputs).
        If a prior step run with this key and status='succeeded' exists
        in this project, the executor reuses its output_refs without re-running.
        """
        import hashlib, json
        return hashlib.sha256(
            json.dumps(
                {"type": self.type_key, "config": config, "inputs": inputs},
                sort_keys=True
            ).encode()
        ).hexdigest()
```

---

## Step Input / Output Contract

All inputs and outputs are **artifact references** — UUIDs and blob hashes. Raw bytes never pass through the engine.

| step_type | queue | inputs | outputs |
|---|---|---|---|
| `step.extract_frames` | `preprocessing` | `{source_id: uuid}` | `{sample_ids: [uuid, ...], frame_count}` |
| `step.import_dataset` | `preprocessing` | `{blob_hash}` or `{folder_path}` | `{sample_ids, annotation_revision_ids, splits?, import_stats}` |
| `step.auto_label` | `training` | `{sample_ids: [uuid, ...]}` (config: `model_version_id` required) | `{annotation_revision_ids: [uuid, ...]}` |
| `step.human_review` | `cvat` | `{sample_ids: [uuid, ...], annotation_revision_ids?: [uuid, ...]}` | `{annotation_revision_ids: [uuid, ...]}` (after gate) |
| `step.commit_dataset` | `preprocessing` | `{sample_ids: [uuid, ...], annotation_revision_ids: [uuid, ...], splits?}` | `{commit_id: uuid, ref_id: uuid, dataset_id: uuid}` |
| `step.export_yolo` | `preprocessing` | `{commit_id: uuid}` | `{export_blob_hash: "sha256:...", commit_id: uuid}` |
| `step.train` | `training` | `{export_blob_hash: "sha256:...", commit_id: uuid}` | `{model_version_id: uuid}` |

---

## Registration

All steps register themselves at API startup. Workers call `register_all()` on boot.

```python
# packages/steps/src/cvops_steps/__init__.py

from cvops_api.core.registry import registry
from .extract_frames import ExtractFramesStep
from .auto_label     import AutoLabelStep
from .human_review   import HumanReviewStep
from .commit_dataset import CommitDatasetStep
from .export_yolo    import ExportYoloStep
from .train          import TrainStep

def register_all():
    for step in [
        ExtractFramesStep(),
        AutoLabelStep(),
        HumanReviewStep(),
        CommitDatasetStep(),
        ExportYoloStep(),
        TrainStep(),
    ]:
        registry.register(step)
        # also upserts type_schemas row in PG so the UI can read it
```

---

## Adding a New Step (e.g. RF denoising)

```python
# packages/steps/src/cvops_steps/denoise_rf.py

class DenoiseRfStep(Step):
    type_key      = "step.denoise_rf"
    queue         = "preprocessing"
    config_schema = {
        "type": "object",
        "properties": {
            "filter_type":    {"type": "string", "enum": ["bandpass", "notch", "wiener"]},
            "bandwidth_hz":   {"type": "number"},
            "center_freq_hz": {"type": "number"}
        },
        "required": ["filter_type"]
    }
    ui_hints = {
        "group":       "Data Preprocessing",
        "icon":        "radio",
        "description": "Remove noise from RF IQ capture data",
        "order":       3
    }

    async def run(self, ctx: StepContext, config: dict, inputs: dict) -> dict:
        data_item_ids = inputs["data_item_ids"]
        # ... processing logic ...
        return {"data_item_ids": cleaned_item_ids}
```

Then add `DenoiseRfStep()` to `register_all()`. Done. The palette, executor, and worker all pick it up automatically — zero other changes.

---

## Rules Every Step Must Follow

```
✓ inputs and outputs are artifact references only — never raw bytes
✓ use ctx.storage to read/write blobs (never boto3 directly)
✓ use ctx.session for DB reads/writes (never raw asyncpg)
✓ call ctx.audit() for meaningful state changes
✓ implement idempotency_key() correctly — same inputs must produce same key
✓ gate steps must raise GateException, not return
✓ set queue to match which worker should run this step
✓ set ui_hints.group to place the step in the correct palette section
```
