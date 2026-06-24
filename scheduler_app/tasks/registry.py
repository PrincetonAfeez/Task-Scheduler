""" Task registry operations for the scheduler app. """

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class TaskContext:
    execution_id: int | None
    job_id: int | None
    attempt_number: int
    idempotency_key: str
    scheduled_for: datetime | None
    worker_id: str


TaskCallable = Callable[[dict[str, Any], TaskContext], str]


@dataclass(frozen=True)
class TaskSpec:
    name: str
    description: str
    expected_config: dict[str, str] = field(default_factory=dict)
    idempotent: bool = True
    safe_for_demo: bool = True
    func: TaskCallable | None = None


_REGISTRY: dict[str, TaskSpec] = {}
_LOADED = False


def register_task(
    *,
    name: str,
    description: str,
    expected_config: dict[str, str] | None = None,
    idempotent: bool = True,
    safe_for_demo: bool = True,
) -> Callable[[TaskCallable], TaskCallable]:
    def decorator(func: TaskCallable) -> TaskCallable:
        _REGISTRY[name] = TaskSpec(
            name=name,
            description=description,
            expected_config=expected_config or {},
            idempotent=idempotent,
            safe_for_demo=safe_for_demo,
            func=func,
        )
        return func

    return decorator


def load_default_tasks() -> None:
    global _LOADED
    if _LOADED:
        return
    from . import demo_tasks  # noqa: F401

    _LOADED = True


def registered_tasks() -> dict[str, TaskSpec]:
    load_default_tasks()
    return dict(_REGISTRY)


def get_task(name: str) -> TaskSpec:
    tasks = registered_tasks()
    if name not in tasks:
        raise KeyError(f"registered task not found: {name}")
    return tasks[name]


def catalog_metadata() -> list[dict[str, object]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "expected_config": spec.expected_config,
            "idempotent": spec.idempotent,
            "safe_for_demo": spec.safe_for_demo,
        }
        for spec in sorted(registered_tasks().values(), key=lambda item: item.name)
    ]

