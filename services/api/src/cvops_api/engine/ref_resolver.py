"""
Resolves $-prefixed reference strings in step inputs.
  "$steps.<step_id>.outputs.<name>"  →  step_outputs[step_id][name]
  "$run.params.<name>"               →  run_params[name]
Recurses into dicts and lists so references can be nested anywhere.
"""

from __future__ import annotations

import re
from typing import Any

_STEP_REF = re.compile(r"^\$steps\.(.+)\.outputs\.([^.]+)$")
_PARAM_REF = re.compile(r"^\$run\.params\.(.+)$")


class ResolutionError(Exception):
    pass


def resolve_refs(
    value: Any,
    step_outputs: dict[str, dict[str, Any]],
    run_params: dict[str, Any],
) -> Any:
    """Recursively resolve $-reference strings in value."""
    if isinstance(value, str):
        m = _STEP_REF.match(value)
        if m:
            step_id, output_name = m.group(1), m.group(2)
            if step_id not in step_outputs:
                raise ResolutionError(f"Step '{step_id}' has no recorded outputs")
            outputs = step_outputs[step_id]
            if output_name not in outputs:
                raise ResolutionError(f"Step '{step_id}' output '{output_name}' not found")
            return outputs[output_name]
        m = _PARAM_REF.match(value)
        if m:
            name = m.group(1)
            if name not in run_params:
                raise ResolutionError(f"Run param '{name}' not found")
            return run_params[name]
        if value.startswith("$"):
            raise ResolutionError(f"Unresolved ref: {value!r}")
        return value
    if isinstance(value, dict):
        return {k: resolve_refs(v, step_outputs, run_params) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, step_outputs, run_params) for item in value]
    return value
