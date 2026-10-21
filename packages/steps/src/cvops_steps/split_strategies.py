"""Re-export of the split strategies, which live in the API package.

The strategies moved to `cvops_api.core.split_strategies` so the API's direct
commit endpoints use the exact same assignment as `commit_dataset`. This module
keeps the old import path working.
"""

from __future__ import annotations

from cvops_api.core.split_strategies import (
    SPLIT_STRATEGIES,
    SplitFn,
    SplitResult,
    _bucket,
    _unit_hash,
    assign_grouped,
    by_source_group,
    get,
    group_key,
    majority_splits,
    random_seeded,
    register,
)

__all__ = [
    "SPLIT_STRATEGIES",
    "SplitFn",
    "SplitResult",
    "_bucket",
    "_unit_hash",
    "assign_grouped",
    "by_source_group",
    "get",
    "group_key",
    "majority_splits",
    "random_seeded",
    "register",
]
