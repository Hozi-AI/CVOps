"""Pluggable train/val/test split strategies for commit_dataset.

A split strategy decides which split ("train"/"val"/"test") each sample lands
in. Strategies register themselves by key so commit_dataset resolves them by
name from config (`type + registry` design principle) and the core engine never
hardcodes split logic.

Every strategy is *deterministic*: the same inputs always produce the same
assignment, so a commit can be reproduced. Determinism comes from hashing stable
ids — never from a RNG seeded at call time — so there is no hidden global state.

Contract — each strategy is::

    assign(sample_ids, source_of, train_ratio, val_ratio, seed) -> {sample_id: split}

- sample_ids:   ordered list of sample UUID strings to place.
- source_of:    {sample_id: source_id} — the data source each sample came from.
- train_ratio:  fraction for "train" (0..1).
- val_ratio:    fraction for "val". The remainder (1 - train - val) is "test";
                if that remainder is ~0 no sample is assigned "test".
- seed:         integer making per-sample strategies reproducible-yet-shuffled.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Iterable

# strategy key -> assign() callable
SplitFn = Callable[[list[str], dict[str, str], float, float, int], dict[str, str]]
SPLIT_STRATEGIES: dict[str, SplitFn] = {}


def register(key: str) -> Callable[[SplitFn], SplitFn]:
    def _wrap(fn: SplitFn) -> SplitFn:
        SPLIT_STRATEGIES[key] = fn
        return fn

    return _wrap


def get(key: str) -> SplitFn:
    try:
        return SPLIT_STRATEGIES[key]
    except KeyError:
        raise ValueError(
            f"unknown split_strategy {key!r}; registered: {sorted(SPLIT_STRATEGIES)}"
        ) from None


def _unit_hash(*parts: str) -> float:
    """Stable float in [0, 1) from the given parts — our source of determinism."""
    h = hashlib.sha256(":".join(parts).encode()).digest()
    # 8 bytes is ample resolution and avoids big-int work.
    return int.from_bytes(h[:8], "big") / 2**64


def _bucket(u: float, train_ratio: float, val_ratio: float) -> str:
    if u < train_ratio:
        return "train"
    if u < train_ratio + val_ratio:
        return "val"
    return "test"


@register("by_source_group")
def by_source_group(
    sample_ids: list[str],
    source_of: dict[str, str],
    train_ratio: float,
    val_ratio: float,
    seed: int,
) -> dict[str, str]:
    """Assign every sample from one data source to the *same* split.

    This is the safe default for video: frames from a single clip are highly
    correlated, so splitting them per-frame leaks near-identical images across
    train/val/test and inflates metrics. Hashing the source id places the whole
    source deterministically, keeping each clip wholly within one split.
    """
    return {
        sid: _bucket(_unit_hash(str(seed), source_of[sid]), train_ratio, val_ratio)
        for sid in sample_ids
    }


@register("random_seeded")
def random_seeded(
    sample_ids: list[str],
    source_of: dict[str, str],
    train_ratio: float,
    val_ratio: float,
    seed: int,
) -> dict[str, str]:
    """Assign each sample independently by hashing (seed, sample_id).

    Per-item split. Risks source-correlated leakage for video — callers opt in
    explicitly. Deterministic for a given seed without a stateful RNG.
    """
    return {sid: _bucket(_unit_hash(str(seed), sid), train_ratio, val_ratio) for sid in sample_ids}


# ── Grouped, frame-count-aware assignment ───────────────────────────────────

SPLIT_ORDER = ("train", "val", "test")


def group_key(sample_id: str, source_id: str, frame_index: int | None) -> str:
    """The unit that must never be split: a video, or a single image.

    Frames of a video (`frame_index` set) share one group per data source. Anything
    else (imports, single images) is its own group — an import is ONE data source
    for the whole zip, so grouping it by source would put every image in one split.
    """
    if frame_index is not None:
        return f"src:{source_id}"
    return f"img:{sample_id}"


@dataclass(frozen=True)
class SplitResult:
    """Outcome of `assign_grouped`: the split of every sample plus any warnings.

    Attributes:
        splits: {sample_id: "train" | "val" | "test"} for every sample passed to
            `assign_grouped`. All samples of one group share the same value.
        warnings: Human-readable messages for problems the caller should surface
            to the user instead of failing, e.g. a split with a ratio above zero
            that ended up with no samples. Empty when the assignment is clean.
    """

    splits: dict[str, str]
    warnings: list[str] = field(default_factory=list)


def assign_grouped(
    sample_ids: list[str],
    group_of: dict[str, str],
    train_ratio: float,
    val_ratio: float,
    seed: int,
    *,
    pinned: dict[str, str] | None = None,
    existing: dict[str, int] | None = None,
) -> SplitResult:
    """Place whole groups into train/val/test, filling toward the target frame counts.

    A group (a video, or a single image — see `group_key`) is never split across
    sets, because near-identical frames in both train and val would leak and inflate
    the metrics. Instead whole groups are dealt out until each split reaches its
    share of the cumulative frame count (this batch plus the parent commit).

    Algorithm:
        1. Samples of groups in `pinned` keep the split the parent commit gave them.
        2. The remaining groups are visited in `hash(seed, group)` order, which is
           deterministic and independent of group size and upload order.
        3. Each group goes to the split furthest below its target (largest deficit),
           ties to the earlier of train -> val -> test. Filling train first instead
           would let it swallow whole videos past its share: three equal videos
           would all land in train and leave val empty.

    Because groups are indivisible the ratios are only approximate, and with few
    groups a split can stay empty; that is reported in `SplitResult.warnings`
    rather than by cutting a group.

    Args:
        sample_ids: Ids of the samples in this batch (strings, as in `group_of`).
        group_of: {sample_id: group} for every id in `sample_ids`; build the values
            with `group_key`.
        train_ratio: Fraction of frames for train, in [0, 1].
        val_ratio: Fraction of frames for val, in [0, 1]. The test ratio is the
            remainder, `1 - train_ratio - val_ratio`. A split whose ratio is zero is
            never assigned and never warned about.
        seed: Seeds the group ordering; the same seed and inputs always give the same
            result.
        pinned: {group: split} for groups already present in the parent commit.
            Their samples take that split regardless of the ratios.
        existing: {split: frame count} already in the parent commit, excluding the
            samples of this batch (the batch overrides them). Counted towards both
            the targets and the running totals, so repeated small batches still
            fill val and test instead of each piling onto train.

    Returns:
        A `SplitResult` with a split for every id in `sample_ids` and a warning for
        each active split that ended up empty.

    Raises:
        KeyError: A sample id in `sample_ids` is missing from `group_of`, or a
            `pinned` split is not one of train/val/test.
    """
    pinned = pinned or {}
    counts = {name: (existing or {}).get(name, 0) for name in SPLIT_ORDER}
    ratios = {
        "train": train_ratio,
        "val": val_ratio,
        "test": max(0.0, 1.0 - train_ratio - val_ratio),
    }
    active = [name for name in SPLIT_ORDER if ratios[name] > 1e-9]
    total = sum(counts.values()) + len(sample_ids)
    target = {name: ratios[name] * total for name in SPLIT_ORDER}

    members: dict[str, list[str]] = {}
    for sid in sample_ids:
        members.setdefault(group_of[sid], []).append(sid)

    splits: dict[str, str] = {}
    for group, sids in members.items():
        if group in pinned:
            for sid in sids:
                splits[sid] = pinned[group]
            counts[pinned[group]] += len(sids)

    for group in sorted(
        (g for g in members if g not in pinned), key=lambda g: _unit_hash(str(seed), g)
    ):
        sids = members[group]
        # Rounded so float noise (1 - 0.7 - 0.15 > 0.15) can't break a val/test tie.
        choice = max(active, key=lambda n: round(target[n] - counts[n], 6))
        for sid in sids:
            splits[sid] = choice
        counts[choice] += len(sids)

    warnings = [
        f"{name} is empty: a video is never split across sets, and there are "
        f"too few videos/images to fill every split"
        for name in active
        if counts[name] == 0
    ]
    return SplitResult(splits=splits, warnings=warnings)


def majority_splits(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Pick one split per group from the splits its samples already have.

    A parent commit made before group-aware splitting may have one video spread over
    several splits. This resolves each such group to the split holding most of its
    samples so new frames of the video follow the majority. Ties go to the earlier
    of train, val, test, which keeps the result deterministic.

    Args:
        pairs: One (group, split) pair per sample already committed, where `group`
            comes from `group_key` and `split` is "train", "val" or "test".

    Returns:
        {group: split} with one entry for each group that appears in `pairs`.
        Groups with no pairs are absent; an empty input gives an empty dict.
    """
    votes: dict[str, dict[str, int]] = {}
    for group, split in pairs:
        bucket = votes.setdefault(group, {})
        bucket[split] = bucket.get(split, 0) + 1
    order = {name: i for i, name in enumerate(SPLIT_ORDER)}
    return {
        group: min(b.items(), key=lambda kv: (-kv[1], order.get(kv[0], len(order))))[0]
        for group, b in votes.items()
    }
