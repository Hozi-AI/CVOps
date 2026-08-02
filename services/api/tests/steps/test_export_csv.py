"""Unit tests for export_csv helpers."""
from cvops_steps.export_csv import _merge_rows


def test_merge_rows_labels_by_time():
    window = [
        {"timestamp_ms": 50, "value": 1.0},
        {"timestamp_ms": 600, "value": 2.0},
    ]
    annotations = [{"time_start_ms": 0, "time_end_ms": 100, "class_key": "high"}]
    result = _merge_rows(window, annotations, "train", "s1")
    assert result[0]["label"] == "high"
    assert result[1]["label"] == ""  # outside annotation range
    assert result[0]["split"] == "train"
    assert result[0]["_sample_id"] == "s1"


def test_merge_rows_no_annotations():
    window = [{"timestamp_ms": 0, "v": 1}]
    result = _merge_rows(window, [], "train", "s1")
    assert result[0]["label"] == ""


def test_merge_rows_preserves_original_fields():
    window = [{"timestamp_ms": 10, "ch1": 3.5, "ch2": 1.2}]
    result = _merge_rows(window, [], "val", "s2")
    assert result[0]["ch1"] == 3.5
    assert result[0]["ch2"] == 1.2
