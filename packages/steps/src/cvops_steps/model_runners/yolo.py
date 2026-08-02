from __future__ import annotations
import tempfile
from pathlib import Path
from cvops_steps.model_runners.base import ModelRunner


class YoloModelRunner(ModelRunner):
    name = "yolo"

    async def predict(self, sample_id, blob_hash, modality, model_bytes, config, storage) -> list[dict]:
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
        from ultralytics import YOLO  # noqa: PLC0415

        threshold = float(config.get("confidence_threshold", 0.35))

        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            f.write(model_bytes)
            pt_path = f.name

        img_bytes = await storage.get_bytes(blob_hash)
        image = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
        model = YOLO(pt_path)
        results = model(image, conf=threshold, verbose=False)[0]

        annotations = []
        if results.boxes:
            names = results.names
            h, w = image.shape[:2]
            for box in results.boxes:
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                annotations.append({
                    "class_key": names[cls_id],
                    "geometry": {"type": "bbox", "coords": [x1 / w, y1 / h, x2 / w, y2 / h]},
                    "confidence": float(box.conf[0]),
                    "attributes": {},
                })
        return annotations
