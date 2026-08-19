from __future__ import annotations
from abc import ABC, abstractmethod


class ModelRunner(ABC):
    name: str = ""

    @abstractmethod
    async def predict(
        self,
        sample_id: str,
        blob_hash: str,
        modality: str,
        model_bytes: bytes,
        config: dict,
        storage,
    ) -> list[dict]:
        """Run inference on one sample. Returns annotation dicts."""
