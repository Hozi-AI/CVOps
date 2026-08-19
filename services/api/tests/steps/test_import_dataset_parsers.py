"""Unit tests for import_dataset parsing helpers — no DB, no I/O needed."""
from __future__ import annotations
import json
from pathlib import Path
from cvops_steps.import_dataset import detect_format, parse_yolo, parse_coco, IMAGE_EXTS


def _make_yolo_tree(tmp_path: Path) -> Path:
    (tmp_path / "images").mkdir()
    (tmp_path / "labels").mkdir()
    (tmp_path / "images" / "a.jpg").write_bytes(b"fake")
    (tmp_path / "labels" / "a.txt").write_text("0 0.5 0.5 0.2 0.3\n1 0.1 0.1 0.05 0.05\n")
    (tmp_path / "data.yaml").write_text("names:\n  - cat\n  - dog\n")
    return tmp_path


def _make_coco_tree(tmp_path: Path) -> Path:
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "b.jpg").write_bytes(b"fake")
    ann = {
        "images": [{"id": 1, "file_name": "b.jpg", "width": 100, "height": 200}],
        "annotations": [{"id": 1, "image_id": 1, "category_id": 1, "bbox": [10, 20, 30, 40]}],
        "categories": [{"id": 1, "name": "car"}],
    }
    (tmp_path / "annotations.json").write_text(json.dumps(ann))
    return tmp_path


def test_detect_format_yolo(tmp_path):
    _make_yolo_tree(tmp_path)
    assert detect_format(tmp_path) == "yolo"


def test_detect_format_coco(tmp_path):
    _make_coco_tree(tmp_path)
    assert detect_format(tmp_path) == "coco"


def test_detect_format_raw(tmp_path):
    (tmp_path / "img.png").write_bytes(b"x")
    assert detect_format(tmp_path) == "raw"


def test_parse_yolo_maps_class_indices(tmp_path):
    _make_yolo_tree(tmp_path)
    result = parse_yolo(tmp_path, ["cat", "dog"])
    anns = result["a"]
    assert len(anns) == 2
    assert anns[0]["class_key"] == "cat"
    assert anns[0]["geometry"] == {"type": "bbox", "coords": [0.5, 0.5, 0.2, 0.3]}
    assert anns[1]["class_key"] == "dog"


def test_parse_coco_normalises_bbox(tmp_path):
    _make_coco_tree(tmp_path)
    result = parse_coco(tmp_path)
    anns = result["b.jpg"]
    assert len(anns) == 1
    a = anns[0]
    assert a["class_key"] == "car"
    # bbox [10, 20, 30, 40] on 100×200 → cx=(10+15)/100=0.25, cy=(20+20)/200=0.2, w=0.3, h=0.2
    coords = a["geometry"]["coords"]
    assert abs(coords[0] - 0.25) < 1e-6
    assert abs(coords[1] - 0.2) < 1e-6
    assert abs(coords[2] - 0.3) < 1e-6
    assert abs(coords[3] - 0.2) < 1e-6


def test_image_exts_covers_common_types():
    assert ".jpg" in IMAGE_EXTS
    assert ".png" in IMAGE_EXTS
    assert ".bmp" in IMAGE_EXTS
