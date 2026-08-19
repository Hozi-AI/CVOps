from cvops_steps.model_runners.base import ModelRunner

_registry: dict[str, ModelRunner] = {}


def register_runner(runner: ModelRunner) -> None:
    _registry[runner.name] = runner


def get_runner(name: str) -> ModelRunner:
    if name not in _registry:
        raise KeyError(f"Unknown model runner: {name!r}. Available: {list(_registry)}")
    return _registry[name]
