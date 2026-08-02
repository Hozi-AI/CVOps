"""Label Studio labeling backend for text, sensor, and image annotation.

Env vars:
  LABEL_STUDIO_URL      — base URL (default: http://localhost:8080)
  LABEL_STUDIO_API_KEY  — API token (required at runtime)
"""
from __future__ import annotations
import json
import os

import httpx

from cvops_steps.labeling_backends.base import LabelingBackend, ReviewSample


class LabelStudioBackend(LabelingBackend):
    name = "label_studio"

    def _headers(self) -> dict:
        key = os.environ.get("LABEL_STUDIO_API_KEY", "")
        return {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    def _base(self) -> str:
        return os.environ.get("LABEL_STUDIO_URL", "http://localhost:8080").rstrip("/")

    def _label_config(self, modality: str, label_names: list[str]) -> str:
        labels_xml = "".join(f'<Label value="{name}"/>' for name in label_names)
        if modality == "text":
            return (
                '<View><Text name="text" value="$text"/>'
                f'<Labels name="label" toName="text">{labels_xml}</Labels></View>'
            )
        if modality == "sensor":
            return (
                '<View><TimeSeries name="ts" value="$timeseries" valueType="json">'
                "</TimeSeries>"
                f'<Labels name="label" toName="ts">{labels_xml}</Labels></View>'
            )
        # default: image
        return (
            '<View><Image name="image" value="$image"/>'
            f'<RectangleLabels name="label" toName="image">{labels_xml}</RectangleLabels></View>'
        )

    async def _task_data(self, sample: ReviewSample, ctx_storage) -> dict:
        if sample.modality == "text":
            raw = await ctx_storage.get_bytes(sample.blob_hash)
            return {"text": raw.decode("utf-8", errors="replace")}
        if sample.modality == "sensor":
            raw = await ctx_storage.get_bytes(sample.blob_hash)
            return {"timeseries": json.loads(raw)}
        # image — return presigned URL; Label Studio fetches it directly
        return {"image": await ctx_storage.get_presigned_get(sample.blob_hash)}

    async def push(
        self,
        samples: list[ReviewSample],
        label_names: list[str],
        task_name: str,
        ctx_storage,
    ) -> dict:
        modality = samples[0].modality if samples else "image"
        label_config = self._label_config(modality, label_names)
        base = self._base()
        headers = self._headers()

        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(
                f"{base}/api/projects",
                headers=headers,
                json={"title": task_name, "label_config": label_config},
            )
            r.raise_for_status()
            project_id = r.json()["id"]

            tasks = []
            for s in samples:
                data = await self._task_data(s, ctx_storage)
                tasks.append({"data": data, "meta": {"sample_id": s.sample_id}})

            r = await http.post(
                f"{base}/api/projects/{project_id}/import",
                headers=headers,
                json=tasks,
            )
            r.raise_for_status()

        return {
            "job_id": str(project_id),
            "project_id": project_id,
            "task_ids": [],
            "job_ids": [],
        }

    def gate_data(self, push_result: dict) -> dict:
        base = self._base()
        project_id = push_result["project_id"]
        return {
            "backend": "label_studio",
            "project_id": project_id,
            "label_studio_url": f"{base}/projects/{project_id}/data",
        }
