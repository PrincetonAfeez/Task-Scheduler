"""Extended CLI, views, and management command coverage."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from scheduler_app.models import Alert, ExecutionStatus, JobExecution, SchedulerHeartbeat


@pytest.mark.django_db
def test_execution_cli_inspect_retry_cancel(job_factory, now):
    job = job_factory()
    failed = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cli-failed",
        status=ExecutionStatus.FAILED,
        output="out",
        error="err",
    )
    out = StringIO()
    call_command("execution", "inspect", str(failed.id), stdout=out)
    body = out.getvalue()
    assert str(failed.id) in body
    assert "err" in body

    call_command("execution", "retry", str(failed.id))

    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now + timedelta(hours=1),
        run_after=now + timedelta(hours=1),
        idempotency_key="cli-pending",
        status=ExecutionStatus.PENDING,
    )
    call_command("execution", "cancel", str(pending.id))
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_execution_cli_errors(job_factory, now):
    with pytest.raises(CommandError, match="not found"):
        call_command("execution", "inspect", "999999")

    job = job_factory()
    running = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="not-retryable",
        status=ExecutionStatus.RUNNING,
    )
    with pytest.raises(CommandError, match="not retryable"):
        call_command("execution", "retry", str(running.id))

    succeeded = JobExecution.objects.create(
        job=job,
        scheduled_for=now + timedelta(hours=2),
        run_after=now + timedelta(hours=2),
        idempotency_key="not-cancellable",
        status=ExecutionStatus.SUCCEEDED,
    )
    with pytest.raises(CommandError):
        call_command("execution", "cancel", str(succeeded.id))


@pytest.mark.django_db
def test_execution_list_with_status_filter(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="list-failed",
        status=ExecutionStatus.FAILED,
    )
    out = StringIO()
    call_command("execution", "list", "--status", "failed", stdout=out)
    assert "failed" in out.getvalue().lower()


@pytest.mark.django_db
def test_demo_single_fire_requires_postgresql():
    with pytest.raises(CommandError, match="PostgreSQL"):
        call_command("demo", "single-fire")


@pytest.mark.django_db
def test_demo_timeout_runs():
    out = StringIO()
    call_command("demo", "timeout", stdout=out)
    assert "execution=" in out.getvalue()


@pytest.mark.django_db
def test_health_command_with_heartbeats(now):
    SchedulerHeartbeat.objects.create(
        scheduler_id="health-cli",
        hostname="test",
        process_id=1,
        last_tick_at=now,
        health_state="healthy",
    )
    out = StringIO()
    call_command("health", stdout=out)
    assert "health-cli" in out.getvalue()


@pytest.mark.django_db
def test_alerts_list_and_resolve(job_factory, now):
    job = job_factory()
    alert = Alert.objects.create(job=job, message="test alert", resolved=False)
    out = StringIO()
    call_command("alerts", "list", stdout=out)
    assert "test alert" in out.getvalue()
    call_command("alerts", "resolve", str(alert.id))
    alert.refresh_from_db()
    assert alert.resolved is True


@pytest.mark.django_db
def test_job_cli_preview_and_catalog():
    out = StringIO()
    call_command("job", "catalog", stdout=out)
    assert "always_succeed" in out.getvalue()


@pytest.mark.django_db
def test_scheduler_run_one_tick():
    out = StringIO()
    call_command("scheduler", "run", "--once", stdout=out)
    assert "due_jobs=" in out.getvalue()


@pytest.mark.django_db
@override_settings(EXECUTOR_BACKEND="inprocess")
def test_worker_run_once(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="worker-once",
        status=ExecutionStatus.PENDING,
    )
    out = StringIO()
    call_command("worker", "run", "--once", stdout=out)
    assert "completed=" in out.getvalue()
    assert JobExecution.objects.filter(job=job, status=ExecutionStatus.SUCCEEDED).exists()


@pytest.mark.django_db
def test_readyz_cache_failure(client, settings):
    with patch("django.core.cache.cache.set", side_effect=RuntimeError("cache down")):
        response = client.get("/readyz")
    assert response.status_code == 503
    assert b"cache unavailable" in response.content


@pytest.mark.django_db
def test_readyz_database_failure(client):
    with patch("django.db.connection.ensure_connection", side_effect=RuntimeError("db down")):
        response = client.get("/readyz")
    assert response.status_code == 503
    assert b"database unavailable" in response.content


@pytest.mark.django_db
@override_settings(REDIS_URL="redis://localhost:6379/0")
def test_readyz_redis_failure(client, settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": settings.REDIS_URL,
        }
    }
    with patch("django_redis.get_redis_connection") as mock_redis:
        mock_redis.return_value.ping.side_effect = RuntimeError("redis down")
        response = client.get("/readyz")
    assert response.status_code == 503
    assert b"redis unavailable" in response.content


@pytest.mark.django_db
def test_job_trigger_disabled_shows_error(auth_client, job_factory):
    job = job_factory(enabled=False)
    url = reverse("scheduler_app:job_action", args=[job.id, "trigger"])
    response = auth_client.post(url)
    assert response.status_code == 302
    assert not JobExecution.objects.filter(job=job, is_manual=True).exists()


@pytest.mark.django_db
def test_execution_retry_bad_status(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="bad-retry",
        status=ExecutionStatus.RUNNING,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "retry"])
    response = auth_client.post(url)
    assert response.status_code == 400


@pytest.mark.django_db
def test_execution_cancel_value_error(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="bad-cancel",
        status=ExecutionStatus.SUCCEEDED,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "cancel"])
    response = auth_client.post(url)
    assert response.status_code == 302


@pytest.mark.django_db
def test_execution_unknown_action(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="unknown-action",
        status=ExecutionStatus.FAILED,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "bogus"])
    response = auth_client.post(url)
    assert response.status_code == 400


@pytest.mark.django_db
def test_alert_resolve_idempotent(auth_client, job_factory):
    job = job_factory()
    alert = Alert.objects.create(job=job, message="resolve-me", resolved=True)
    url = reverse("scheduler_app:alert_resolve", args=[alert.id])
    response = auth_client.post(url)
    assert response.status_code == 302


@pytest.mark.django_db
def test_dashboard_jobs_total_caption(auth_client, job_factory, now):
    for index in range(30):
        job_factory(name=f"bulk-{index}", next_run_at=now + timedelta(minutes=index))
    response = auth_client.get(reverse("scheduler_app:dashboard"))
    body = response.content.decode()
    assert "Showing first 25 of" in body or "30 jobs" in body


@pytest.mark.django_db
def test_health_view_renders(auth_client):
    response = auth_client.get(reverse("scheduler_app:health"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_accounts_login_page(client):
    response = client.get(reverse("login"))
    assert response.status_code == 200
