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
from typing import Callable

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
