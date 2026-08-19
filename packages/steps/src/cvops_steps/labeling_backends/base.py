from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ReviewSample:
    sample_id: str
    blob_hash: str
    width: int | None
    height: int | None
    modality: str
    pre_label_annotations: list = field(default_factory=list)


class LabelingBackend(ABC):
    name: str = ""

    @abstractmethod
    async def push(
        self,
        samples: list[ReviewSample],
        label_names: list[str],
        task_name: str,
        ctx_storage,
    ) -> dict:
        """
        Push samples for human review.
        Returns a dict always containing 'job_id' (str) plus any
        backend-specific fields used by gate_data() and the labeling_jobs insert.
        """

    @abstractmethod
    def gate_data(self, push_result: dict) -> dict:
        """Return the dict stored in runs.output_refs['gate_data'] for the UI."""
