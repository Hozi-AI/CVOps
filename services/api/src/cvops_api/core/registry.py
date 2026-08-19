"""
Registry — maps type_key → (JSON Schema, Step implementation).
Backed by the type_schemas DB table for persistence and UI exposure.
Core never imports concrete step implementations; steps register themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cvops_api.engine.step import Step


@dataclass
class StepRegistration:
    type_key: str
    category: str
    json_schema: dict[str, Any]
    ui_hints: dict[str, Any]
    impl: "Step"


class Registry:
    """In-memory singleton — populated at startup via register()."""

    def __init__(self) -> None:
        self._store: dict[str, StepRegistration] = {}

    def register(self, step: "Step") -> None:
        reg = StepRegistration(
            type_key=step.type_key,
            category=getattr(step, "category", "step"),
            json_schema=step.config_schema,
            ui_hints={},
            impl=step,
        )
        self._store[step.type_key] = reg

    def register_type(
        self,
        type_key: str,
        category: str,
        json_schema: dict[str, Any],
        ui_hints: dict[str, Any] | None = None,
    ) -> None:
        """Register a non-step type (annotation_type, source_type, etc.)."""
        from cvops_api.engine.step import Step  # noqa: PLC0415

        class _Placeholder(Step):
            async def run(self, ctx, config, inputs):  # pragma: no cover
                raise NotImplementedError

        placeholder = _Placeholder()
        placeholder.type_key = type_key
        placeholder.config_schema = json_schema
        reg = StepRegistration(
            type_key=type_key,
            category=category,
            json_schema=json_schema,
            ui_hints=ui_hints or {},
            impl=placeholder,
        )
        self._store[type_key] = reg

    def resolve(self, type_key: str) -> StepRegistration:
        if type_key not in self._store:
            raise KeyError(f"Unknown type_key: {type_key!r}")
        return self._store[type_key]

    def list_by_category(self, category: str) -> list[StepRegistration]:
        return [r for r in self._store.values() if r.category == category]

    def all(self) -> list[StepRegistration]:
        return list(self._store.values())

    def validate_config(self, type_key: str, config: dict[str, Any]) -> None:
        import jsonschema

        schema = self.resolve(type_key).json_schema
        jsonschema.validate(instance=config, schema=schema)


registry = Registry()
