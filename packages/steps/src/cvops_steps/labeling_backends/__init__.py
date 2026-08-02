from cvops_steps.labeling_backends.base import LabelingBackend, ReviewSample

_registry: dict[str, LabelingBackend] = {}


def register_backend(backend: LabelingBackend) -> None:
    _registry[backend.name] = backend


def get_backend(name: str) -> LabelingBackend:
    if name not in _registry:
        raise KeyError(
            f"Unknown labeling backend: {name!r}. Available: {list(_registry)}"
        )
    return _registry[name]
