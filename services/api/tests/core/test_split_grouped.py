"""Unit tests for the grouped, frame-count-aware split assignment (pure logic)."""

from __future__ import annotations

from collections import Counter

from cvops_api.core.split_strategies import assign_grouped, group_key


def _videos(sizes: dict[str, int]) -> tuple[list[str], dict[str, str]]:
    ids: list[str] = []
    group_of: dict[str, str] = {}
    for video, n in sizes.items():
        for i in range(n):
            sid = f"{video}-{i}"
            ids.append(sid)
            group_of[sid] = group_key(sid, video, i)
    return ids, group_of


def _frames(result_splits: dict[str, str]) -> Counter[str]:
    return Counter(result_splits.values())


def test_group_key_video_frames_share_a_group_images_do_not() -> None:
    assert group_key("a", "src1", 0) == group_key("b", "src1", 7)
    assert group_key("a", "src1", None) != group_key("b", "src1", None)


def test_a_video_is_never_split() -> None:
    ids, group_of = _videos({f"v{i}": 30 + i for i in range(9)})
    res = assign_grouped(ids, group_of, 0.7, 0.15, 42)
    for video in {g for g in group_of.values()}:
        assert len({res.splits[s] for s in ids if group_of[s] == video}) == 1


def test_import_of_single_images_spreads_across_splits() -> None:
    # One data source, no frame_index: every image is its own group.
    ids = [f"img-{i}" for i in range(200)]
    group_of = {sid: group_key(sid, "one-import-source", None) for sid in ids}
    res = assign_grouped(ids, group_of, 0.7, 0.15, 42)
    counts = _frames(res.splits)
    assert set(counts) == {"train", "val", "test"}
    assert 130 <= counts["train"] <= 150
    assert res.warnings == []


def test_frame_ratios_land_near_target_with_many_videos() -> None:
    sizes = {f"v{i}": 20 + (i * 37) % 80 for i in range(40)}
    ids, group_of = _videos(sizes)
    counts = _frames(assign_grouped(ids, group_of, 0.7, 0.15, 42).splits)
    total = len(ids)
    assert abs(counts["train"] / total - 0.70) < 0.08
    assert abs(counts["val"] / total - 0.15) < 0.08


def test_lone_video_goes_whole_to_one_split_and_warns() -> None:
    ids, group_of = _videos({"only": 50})
    res = assign_grouped(ids, group_of, 0.7, 0.15, 42)
    assert len(set(res.splits.values())) == 1
    assert len(res.warnings) == 2  # two of the three splits are empty
    assert {w.split(" ")[0] for w in res.warnings} <= {"train", "val", "test"}


def test_deterministic_and_independent_of_input_order() -> None:
    ids, group_of = _videos({f"v{i}": 10 + i for i in range(8)})
    a = assign_grouped(ids, group_of, 0.7, 0.15, 42)
    b = assign_grouped(list(reversed(ids)), group_of, 0.7, 0.15, 42)
    assert a.splits == b.splits


def test_seed_changes_the_shuffle() -> None:
    ids, group_of = _videos({f"v{i}": 10 + i for i in range(12)})
    a = assign_grouped(ids, group_of, 0.7, 0.15, 1).splits
    b = assign_grouped(ids, group_of, 0.7, 0.15, 2).splits
    assert a != b


def test_pinned_group_keeps_its_split() -> None:
    ids, group_of = _videos({"a": 10, "b": 10})
    res = assign_grouped(ids, group_of, 0.7, 0.15, 42, pinned={group_of["a-0"]: "test"})
    assert {res.splits[s] for s in ids if s.startswith("a-")} == {"test"}


def test_existing_counts_steer_new_groups_to_underfilled_splits() -> None:
    # The parent already holds 700 train frames; a single new 100-frame video must
    # not pile onto train (without `existing` it would be the first, train).
    ids, group_of = _videos({"new": 100})
    res = assign_grouped(ids, group_of, 0.7, 0.15, 42, existing={"train": 700, "val": 0, "test": 0})
    assert set(res.splits.values()) == {"val"}


def test_zero_ratio_split_is_skipped_and_not_warned() -> None:
    ids, group_of = _videos({f"v{i}": 10 for i in range(6)})
    res = assign_grouped(ids, group_of, 0.8, 0.2, 42)  # test ratio is 0
    assert "test" not in set(res.splits.values())
    assert all(not w.startswith("test") for w in res.warnings)
