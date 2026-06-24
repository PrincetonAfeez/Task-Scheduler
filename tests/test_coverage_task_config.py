"""Tests for task_config validation and JobForm edge cases."""

from __future__ import annotations

import pytest

from scheduler_app.forms import JobForm
from scheduler_app.models import ScheduleType
from scheduler_app.services.task_config import validate_task_config


def test_validate_task_config_unknown_task():
    with pytest.raises(ValueError, match="unknown registered task"):
        validate_task_config("not_registered", {})


def test_validate_task_config_not_object():
    with pytest.raises(ValueError, match="JSON object"):
        validate_task_config("always_succeed", "bad")


def test_validate_task_config_none_becomes_empty():
    assert validate_task_config("always_succeed", None) == {}


def test_validate_task_config_unknown_keys():
    with pytest.raises(ValueError, match="unknown config keys"):
        validate_task_config("sleep_for_seconds", {"seconds": 1, "extra": True})


def test_validate_sleep_for_seconds_bounds():
    validate_task_config("sleep_for_seconds", {"seconds": 1})
    with pytest.raises(ValueError, match="at most 300"):
        validate_task_config("sleep_for_seconds", {"seconds": 400})
    with pytest.raises(ValueError, match="greater than zero"):
        validate_task_config("sleep_for_seconds", {"seconds": 0})
    with pytest.raises(ValueError, match="must be a number"):
        validate_task_config("sleep_for_seconds", {"seconds": "bad"})


def test_validate_http_health_check_local():
    validate_task_config("http_health_check_local", {"url": "http://127.0.0.1:8000/healthz"})
    with pytest.raises(ValueError, match="localhost"):
        validate_task_config("http_health_check_local", {"url": "http://example.com"})


def test_validate_write_file_artifact():
    validate_task_config("write_file_artifact", {"file_name": "demo.txt", "content": "hi"})
    with pytest.raises(ValueError, match="content must be a string"):
        validate_task_config("write_file_artifact", {"content": 123})
    with pytest.raises(ValueError, match="path separators"):
        validate_task_config("write_file_artifact", {"file_name": "../evil.txt"})
    with pytest.raises(ValueError, match="letters, numbers"):
        validate_task_config("write_file_artifact", {"file_name": "bad name"})


def test_validate_generate_report_name():
    validate_task_config("generate_report", {"name": "report-1"})
    with pytest.raises(ValueError, match="path separators"):
        validate_task_config("generate_report", {"name": "a/b"})


@pytest.mark.django_db
def test_job_form_rejects_unknown_task():
    form = JobForm(
        data={
            "name": "bad-task-form",
            "description": "",
            "registered_task_name": "totally_fake_task_xyz",
            "task_config": "{}",
            "schedule_type": ScheduleType.INTERVAL,
            "schedule_value": '{"every": "30s"}',
            "timezone": "UTC",
            "enabled": True,
            "overlap_policy": "allow",
            "misfire_policy": "coalesce",
            "misfire_grace_seconds": 60,
            "max_attempts": 3,
            "retry_backoff_seconds": 10,
            "timeout_seconds": 30,
            "retention_count": 500,
            "retention_days": 30,
            "alert_mode": "web",
        }
    )
    assert not form.is_valid()
    assert "registered_task_name" in form.errors


@pytest.mark.django_db
def test_job_form_rejects_bad_schedule_value():
    form = JobForm(
        data={
            "name": "bad-schedule",
            "description": "",
            "registered_task_name": "always_succeed",
            "task_config": "{}",
            "schedule_type": ScheduleType.CRON,
            "schedule_value": '{"expression": "not valid cron"}',
            "timezone": "UTC",
            "enabled": True,
            "overlap_policy": "allow",
            "misfire_policy": "coalesce",
            "misfire_grace_seconds": 60,
            "max_attempts": 3,
            "retry_backoff_seconds": 10,
            "timeout_seconds": 30,
            "retention_count": 500,
            "retention_days": 30,
            "alert_mode": "web",
        }
    )
    assert not form.is_valid()
    assert "schedule_value" in form.errors


@pytest.mark.django_db
def test_job_form_disable_cancels_pending(job_factory, now):
    from scheduler_app.models import ExecutionStatus, JobExecution

    job = job_factory(enabled=True)
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="form-disable",
        status=ExecutionStatus.PENDING,
    )
    form = JobForm(
        data={
            "name": job.name,
            "description": "",
            "registered_task_name": job.registered_task_name,
            "task_config": "{}",
            "schedule_type": job.schedule_type,
            "schedule_value": str(job.schedule_value).replace("'", '"'),
            "timezone": job.timezone,
            "enabled": False,
            "overlap_policy": job.overlap_policy,
            "misfire_policy": job.misfire_policy,
            "misfire_grace_seconds": job.misfire_grace_seconds,
            "max_attempts": job.max_attempts,
            "retry_backoff_seconds": job.retry_backoff_seconds,
            "timeout_seconds": job.timeout_seconds,
            "retention_count": job.retention_count,
            "retention_days": job.retention_days,
            "alert_mode": job.alert_mode,
        },
        instance=job,
    )
    # schedule_value is JSONField - use actual dict serialization
    import json

    form = JobForm(
        data={
            "name": job.name,
            "description": "",
            "registered_task_name": job.registered_task_name,
            "task_config": json.dumps(job.task_config or {}),
            "schedule_type": job.schedule_type,
            "schedule_value": json.dumps(job.schedule_value),
            "timezone": job.timezone,
            "enabled": False,
            "overlap_policy": job.overlap_policy,
            "misfire_policy": job.misfire_policy,
            "misfire_grace_seconds": job.misfire_grace_seconds,
            "max_attempts": job.max_attempts,
            "retry_backoff_seconds": job.retry_backoff_seconds,
            "timeout_seconds": job.timeout_seconds,
            "retention_count": job.retention_count,
            "retention_days": job.retention_days,
            "alert_mode": job.alert_mode,
        },
        instance=job,
    )
    assert form.is_valid(), form.errors
    form.save()
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.CANCELLED


def test_validate_task_config_bool_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        validate_task_config("sleep_for_seconds", {"seconds": True})


def test_validate_empty_optional_stems():
    validate_task_config("write_file_artifact", {"file_name": ""})
    validate_task_config("generate_report", {})
