""" Test round3 for the scheduler app. """

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from scheduler_app.models import EventType, ExecutionStatus, JobExecution
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.task_config import validate_task_config


def test_task_config_rejects_unsafe_report_name():
    with pytest.raises(ValueError, match="path separators"):
        validate_task_config("generate_report", {"name": "../etc/passwd"})


def test_task_config_rejects_unsafe_artifact_name():
    with pytest.raises(ValueError, match="path separators"):
        validate_task_config("write_file_artifact", {"file_name": "foo/bar"})


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_form_disable_cancels_queued_executions(job_factory, now, auth_client):
    job = job_factory(enabled=True)
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="form-disable-pending",
        status=ExecutionStatus.PENDING,
    )
    data = {
        "name": job.name,
        "description": job.description,
        "registered_task_name": job.registered_task_name,
        "task_config": "{}",
        "schedule_type": job.schedule_type,
        "schedule_value": '{"every": "60s"}',
        "timezone": job.timezone,
        "overlap_policy": job.overlap_policy,
        "misfire_policy": job.misfire_policy,
        "misfire_grace_seconds": str(job.misfire_grace_seconds),
        "max_attempts": str(job.max_attempts),
        "retry_backoff_seconds": str(job.retry_backoff_seconds),
        "timeout_seconds": str(job.timeout_seconds),
        "retention_count": str(job.retention_count),
        "retention_days": str(job.retention_days),
        "alert_mode": job.alert_mode,
    }
    response = auth_client.post(reverse("scheduler_app:job_edit", args=[job.id]), data)
    assert response.status_code == 302
    pending.refresh_from_db()
    job.refresh_from_db()
    assert job.enabled is False
    assert pending.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_cli_edit_disable_cancels_queued(job_factory, now):
    job = job_factory(enabled=True)
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cli-edit-disable",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    call_command("job", "edit", str(job.id), "--disabled")
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_claim_skips_disabled_job(job_factory, now):
    job = job_factory(enabled=False)
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="disabled-claim",
        status=ExecutionStatus.PENDING,
    )
    claimed = claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    assert claimed == []
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_dashboard_requires_login(client):
    response = client.get(reverse("scheduler_app:dashboard"))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
@pytest.mark.parametrize(
    "name",
    [
        "scheduler_app:health",
        "scheduler_app:alert_list",
    ],
)
def test_operational_pages_require_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_execution_detail_requires_login(client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="auth-detail",
        status=ExecutionStatus.SUCCEEDED,
    )
    response = client.get(reverse("scheduler_app:execution_detail", args=[execution.id]))
    assert response.status_code == 302


@pytest.mark.django_db
def test_cli_execution_cancel(job_factory, now):
    job = job_factory(enabled=True)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cli-cancel",
        status=ExecutionStatus.PENDING,
    )
    call_command("execution", "cancel", str(execution.id))
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_cli_edit_requires_secret_when_configured(job_factory, settings):
    settings.SCHEDULER_CLI_SECRET = "edit-secret"
    job = job_factory()
    with pytest.raises(CommandError, match="cli-secret"):
        call_command("job", "edit", str(job.id), "--name", "renamed")
    call_command("job", "edit", str(job.id), "--name", "renamed", "--cli-secret", "edit-secret")
    job.refresh_from_db()
    assert job.name == "renamed"


@pytest.mark.django_db
def test_manual_retry_emits_manual_retry_event(job_factory, now):
    from scheduler_app.services.retries import retry_execution

    job = job_factory()
    failed = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="manual-retry-event",
        status=ExecutionStatus.FAILED,
    )
    retry_execution(failed, now=now)
    assert job.events.filter(event_type=EventType.MANUAL_RETRY).exists()


@pytest.mark.django_db
def test_pagination_on_execution_list(client, job_factory, now):
    job = job_factory()
    for index in range(55):
        JobExecution.objects.create(
            job=job,
            scheduled_for=now + timedelta(seconds=index),
            run_after=now,
            idempotency_key=f"page-{index}",
            status=ExecutionStatus.SUCCEEDED,
        )
    response = client.get(reverse("scheduler_app:execution_list"))
    assert response.status_code == 200
    assert b"Page 1 of 2" in response.content


@pytest.mark.django_db
@pytest.mark.skipif(not os.getenv("REDIS_URL"), reason="REDIS_URL not configured")
def test_readyz_with_redis(client, settings):
    redis_url = os.environ["REDIS_URL"]
    settings.REDIS_URL = redis_url
    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": redis_url,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        }
    }
    assert client.get("/readyz").status_code == 200
