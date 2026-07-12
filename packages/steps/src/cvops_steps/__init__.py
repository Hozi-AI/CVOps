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
