"""Unit tests for the YOLO runner's coordinate conversion (no ultralytics needed)."""

from __future__ import annotations

import pytest

from cvops_steps.model_runners.yolo import xyxy_to_norm_cxcywh


def test_converts_pixel_corners_to_normalised_centre_box():
    # 100x50 image, box from (10,10) to (50,40) px.
    cx, cy, w, h = xyxy_to_norm_cxcywh(10, 10, 50, 40, 100, 50)
    assert (cx, cy, w, h) == pytest.approx((0.3, 0.5, 0.4, 0.6))


def test_full_frame_box():
    assert xyxy_to_norm_cxcywh(0, 0, 640, 480, 640, 480) == pytest.approx([0.5, 0.5, 1.0, 1.0])


def test_out_of_frame_corners_are_clamped():
    cx, cy, w, h = xyxy_to_norm_cxcywh(-5, -5, 110, 60, 100, 50)
    assert (cx, cy, w, h) == pytest.approx((0.5, 0.5, 1.0, 1.0))


def test_swapped_corners_still_give_positive_size():
    assert xyxy_to_norm_cxcywh(50, 40, 10, 10, 100, 50) == pytest.approx([0.3, 0.5, 0.4, 0.6])
