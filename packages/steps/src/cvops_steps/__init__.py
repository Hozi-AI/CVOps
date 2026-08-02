import json
from pathlib import Path

from cvops_api.core.registry import registry
from cvops_steps.extract_frames import ExtractFramesStep
from cvops_steps.auto_label import AutoLabelStep
from cvops_steps.human_review import HumanReviewStep
from cvops_steps.commit_dataset import CommitDatasetStep
from cvops_steps.export_yolo import ExportYoloStep
from cvops_steps.train import TrainStep
from cvops_steps.import_dataset import ImportDatasetStep

_ANNOTATION_TYPES = {
    "annotation.text.span":           "text_span.json",
    "annotation.text.classification": "text_classification.json",
    "annotation.sensor.region":       "sensor_region.json",
    "annotation.sensor.point":        "sensor_point.json",
}
_ANN_SCHEMA_DIR = Path(__file__).parent / "schemas" / "annotation_types"


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

    from cvops_steps.labeling_backends import register_backend  # noqa: PLC0415
    from cvops_steps.labeling_backends.cvat import CvatLabelingBackend  # noqa: PLC0415
    register_backend(CvatLabelingBackend())

    for type_key, filename in _ANNOTATION_TYPES.items():
        with open(_ANN_SCHEMA_DIR / filename) as f:
            schema = json.load(f)
        registry.register_type(type_key, "annotation_type", schema)
