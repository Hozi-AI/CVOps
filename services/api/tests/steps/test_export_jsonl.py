"""Unit tests for export_jsonl helpers."""
from cvops_steps.export_jsonl import _to_record


def test_to_record_raw():
    record = _to_record(
        sample_id="s1",
        text="Hello world",
        split="train",
        annotations=[{"class_key": "positive", "confidence": 0.9}],
        fmt="raw",
    )
    assert record == {
        "id": "s1",
        "text": "Hello world",
        "split": "train",
        "labels": [{"class_key": "positive", "confidence": 0.9}],
    }


def test_to_record_openai():
    record = _to_record(
        sample_id="s1",
        text="Hello",
        split="train",
        annotations=[{"class_key": "positive"}],
        fmt="openai",
    )
    assert "messages" in record
    roles = [m["role"] for m in record["messages"]]
    assert "user" in roles
    assert "assistant" in roles
    assert record["messages"][-1]["content"] == "positive"


def test_to_record_hf():
    record = _to_record(
        sample_id="s1",
        text="Great product",
        split="val",
        annotations=[{"class_key": "positive"}],
        fmt="hf",
    )
    assert record["text"] == "Great product"
    assert record["label"] == "positive"


def test_to_record_hf_no_annotation():
    record = _to_record("s1", "text", "train", [], fmt="hf")
    assert record["label"] == ""
