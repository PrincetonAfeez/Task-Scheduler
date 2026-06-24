"""In-process tests for every registered demo task."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings

from scheduler_app.tasks.registry import TaskContext, registered_tasks


def _ctx(**overrides) -> TaskContext:
    defaults = {
        "execution_id": 99,
        "job_id": 1,
        "attempt_number": 1,
        "idempotency_key": "demo-key",
        "scheduled_for": None,
        "worker_id": "test-worker",
    }
    defaults.update(overrides)
    return TaskContext(**defaults)


@pytest.mark.parametrize("name", sorted(registered_tasks()))
def test_registered_task_has_callable(name):
    spec = registered_tasks()[name]
    assert spec.func is not None
    assert spec.description


def test_always_succeed_and_fail():
    tasks = registered_tasks()
    assert "succeeded" in tasks["always_succeed"].func({}, _ctx()).lower()
    with pytest.raises(RuntimeError, match="Intentional"):
        tasks["always_fail"].func({}, _ctx())


def test_sleep_for_seconds():
    tasks = registered_tasks()
    result = tasks["sleep_for_seconds"].func({"seconds": 0.01}, _ctx())
    assert "Slept" in result


def test_fail_once_then_succeed():
    tasks = registered_tasks()
    ctx = _ctx(idempotency_key="fail-once-test-key")
    with pytest.raises(RuntimeError, match="first-attempt"):
        tasks["fail_once_then_succeed"].func({}, ctx)
    result = tasks["fail_once_then_succeed"].func({}, ctx)
    assert "Succeeded" in result


def test_generate_report():
    tasks = registered_tasks()
    result = tasks["generate_report"].func({"name": "unit-test-report"}, _ctx())
    assert "Generated report" in result
    path = Path(settings.BASE_DIR) / "reports" / "unit-test-report.txt"
    assert path.exists()


def test_write_file_artifact():
    tasks = registered_tasks()
    result = tasks["write_file_artifact"].func(
        {"file_name": "unit-artifact.txt", "content": "hello"},
        _ctx(execution_id=42),
    )
    assert "Wrote artifact" in result
    path = Path(settings.BASE_DIR) / "artifacts" / "unit-artifact.txt"
    assert path.read_text(encoding="utf-8") == "hello"


@pytest.mark.django_db
def test_cleanup_old_runs():
    tasks = registered_tasks()
    result = tasks["cleanup_old_runs"].func({}, _ctx())
    assert "Pruned" in result


def test_http_health_check_local_success():
    tasks = registered_tasks()
    response = MagicMock()
    response.status = 200
    response.read.return_value = b"ok"
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    with patch("scheduler_app.tasks.demo_tasks.urllib.request.urlopen", return_value=response):
        result = tasks["http_health_check_local"].func({"url": "http://127.0.0.1:8000/healthz"}, _ctx())
    assert "HTTP 200" in result


def test_http_health_check_local_rejects_remote():
    tasks = registered_tasks()
    with pytest.raises(ValueError, match="localhost"):
        tasks["http_health_check_local"].func({"url": "http://evil.example/"}, _ctx())


def test_fail_once_custom_key():
    tasks = registered_tasks()
    ctx = _ctx(idempotency_key="ignored")
    config = {"key": "custom/key value"}
    with pytest.raises(RuntimeError):
        tasks["fail_once_then_succeed"].func(config, ctx)
    marker = Path(settings.BASE_DIR) / "artifacts" / "fail_once_markers" / "custom_key_value.json"
    assert marker.exists()
    tasks["fail_once_then_succeed"].func(config, ctx)
    assert not marker.exists()
