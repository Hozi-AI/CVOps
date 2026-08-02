from __future__ import annotations
import os
import tempfile
import logging
from pathlib import Path

from cvops_steps.labeling_backends.base import LabelingBackend, ReviewSample

logger = logging.getLogger(__name__)


class CvatLabelingBackend(LabelingBackend):
    name = "cvat"

    async def push(
        self,
        samples: list[ReviewSample],
        label_names: list[str],
        task_name: str,
        ctx_storage,
    ) -> dict:
        from cvops_cvat_client import ReviewImage, push_review_task, register_webhook  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            images: list[ReviewImage] = []
            for s in samples:
                img_bytes = await ctx_storage.get_bytes(s.blob_hash)
                path = Path(tmp) / f"{s.sample_id}.jpg"
                path.write_bytes(img_bytes)
                images.append(
                    ReviewImage(
                        path=path,
                        width=s.width or 0,
                        height=s.height or 0,
                        annotations=s.pre_label_annotations,
                    )
                )
            pushed = push_review_task(task_name, images, label_names=label_names)

        target = os.environ.get("CVAT_WEBHOOK_TARGET")
        secret = os.environ.get("CVAT_WEBHOOK_SECRET")
        if target and secret:
            try:
                register_webhook(pushed["task_id"], target, secret)
            except Exception:
                logger.warning(
                    "CVAT webhook registration failed for task %s; gate still opens.",
                    pushed["task_id"],
                    exc_info=True,
                )

        return {
            "job_id": str(pushed["task_id"]),
            "task_id": pushed["task_id"],
            "job_ids": pushed["job_ids"],
            "cvat_url": pushed["cvat_url"],
        }

    def gate_data(self, push_result: dict) -> dict:
        return {
            "backend": "cvat",
            "cvat_task_id": push_result["task_id"],
            "cvat_url": push_result["cvat_url"],
        }
