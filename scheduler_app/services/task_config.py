""" Task config operations for the scheduler app. """

from __future__ import annotations

import re
from numbers import Real
from typing import Any

from scheduler_app.tasks.registry import registered_tasks

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _positive_number(value: Any, *, field: str, as_int: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a number")
    if as_int:
        int_value = int(float(value))
        if int_value <= 0:
            raise ValueError(f"{field} must be greater than zero")
        return int_value
    float_value = float(value)
    if float_value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return float_value


def _validate_safe_stem(value: Any, *, field: str) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    if ".." in text or "/" in text or "\\" in text:
        raise ValueError(f"{field} must not contain path separators or ..")
    if not _SAFE_FILENAME.match(text):
        raise ValueError(f"{field} must use only letters, numbers, _, ., -")


def _validate_sleep_for_seconds(config: dict[str, Any]) -> None:
    if "seconds" not in config:
        return
    seconds = _positive_number(config["seconds"], field="seconds")
    if seconds > 300:
        raise ValueError("seconds must be at most 300")


def _validate_http_health_check_local(config: dict[str, Any]) -> None:
    if "url" not in config:
        return
    url = str(config["url"])
    if not (url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
        raise ValueError("url must target localhost or 127.0.0.1")


def _validate_write_file_artifact(config: dict[str, Any]) -> None:
    if "content" in config and not isinstance(config["content"], str):
        raise ValueError("content must be a string")
    if "file_name" in config:
        _validate_safe_stem(config["file_name"], field="file_name")


def _validate_generate_report(config: dict[str, Any]) -> None:
    if "name" in config:
        _validate_safe_stem(config["name"], field="name")


_TASK_VALUE_VALIDATORS = {
    "sleep_for_seconds": _validate_sleep_for_seconds,
    "http_health_check_local": _validate_http_health_check_local,
    "write_file_artifact": _validate_write_file_artifact,
    "generate_report": _validate_generate_report,
}


def validate_task_config(task_name: str, config: Any) -> dict[str, Any]:
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("task_config must be a JSON object")

    if task_name not in registered_tasks():
        raise ValueError(f"unknown registered task: {task_name}")

    spec = registered_tasks()[task_name]
    if spec.expected_config:
        unknown = set(config) - set(spec.expected_config)
        if unknown:
            allowed = ", ".join(sorted(spec.expected_config))
            raise ValueError(
                f"unknown config keys for {task_name}: {', '.join(sorted(unknown))}. Allowed: {allowed}"
            )

    validator = _TASK_VALUE_VALIDATORS.get(task_name)
    if validator is not None:
        validator(config)

    return config
