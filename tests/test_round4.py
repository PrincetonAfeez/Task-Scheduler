""" Test round4 for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from scheduler_app.admin import JobAdmin
from scheduler_app.models import Alert, ExecutionStatus, Job, JobExecution, SchedulerHeartbeat
from scheduler_app.services.claiming import cancel_execution
from scheduler_app.services.due import create_manual_execution
from scheduler_app.services.health import prune_stale_scheduler_heartbeats
from scheduler_app.services.leases import recover_expired_leases


def test_home_page_is_public(client):
    assert client.get(reverse("scheduler_app:home")).status_code == 200


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_dashboard_moved_to_dashboard_path(client):
    assert client.get(reverse("scheduler_app:dashboard")).status_code == 302
    assert client.get("/").status_code == 200


@pytest.mark.django_db
def test_create_manual_execution_rejects_disabled_job(job_factory, now):
    job = job_factory(enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        create_manual_execution(job, now=now)


@pytest.mark.django_db
def test_cancel_execution_clears_claimed_at(job_factory, now):
    job = job_factory(enabled=True)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cancel-claimed-at",
        status=ExecutionStatus.CLAIMED,
        claimed_by="worker-a",
        claimed_at=now,
    )
    cancel_execution(execution)
    execution.refresh_from_db()
    assert execution.claimed_at is None
    assert execution.worker_id == ""


@pytest.mark.django_db
def test_lease_recovery_clears_started_at(job_factory, now):
    job = job_factory(enabled=True, max_attempts=3)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="lease-clear-times",
        status=ExecutionStatus.RUNNING,
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=4),
        duration_ms=1000,
        lease_expires_at=now - timedelta(seconds=1),
        attempt_number=1,
    )
    recover_expired_leases(now=now)
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.RETRY_SCHEDULED
    assert execution.started_at is None
    assert execution.finished_at is None
    assert execution.duration_ms is None


@pytest.mark.django_db
def test_admin_job_enabled_is_readonly_and_not_addable():
    site = AdminSite()
    model_admin = JobAdmin(Job, site)
    assert "enabled" in model_admin.readonly_fields
    assert "name" in model_admin.readonly_fields
    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False


@pytest.mark.django_db
def test_cli_enable_requires_secret_when_configured(job_factory, settings):
    settings.SCHEDULER_CLI_SECRET = "enable-secret"
    job = job_factory(enabled=False)
    with pytest.raises(CommandError, match="cli-secret"):
        call_command("job", "enable", str(job.id))
    call_command("job", "enable", str(job.id), "--cli-secret", "enable-secret")
    job.refresh_from_db()
    assert job.enabled is True


@pytest.mark.django_db
def test_cli_alerts_resolve_requires_secret(settings):
    settings.SCHEDULER_CLI_SECRET = "alert-secret"
    alert = Alert.objects.create(message="resolve me")
    with pytest.raises(CommandError, match="cli-secret"):
        call_command("alerts", "resolve", str(alert.id))
    call_command("alerts", "resolve", str(alert.id), "--cli-secret", "alert-secret")
    alert.refresh_from_db()
    assert alert.resolved is True


@pytest.mark.django_db
def test_prune_stale_scheduler_heartbeats(now):
    SchedulerHeartbeat.objects.create(
        scheduler_id="old-scheduler",
        hostname="test",
        process_id=1,
        last_tick_at=now - timedelta(days=2),
        health_state="healthy",
    )
    SchedulerHeartbeat.objects.create(
        scheduler_id="fresh-scheduler",
        hostname="test",
        process_id=2,
        last_tick_at=now,
        health_state="healthy",
    )
    deleted = prune_stale_scheduler_heartbeats(now=now, max_age_seconds=86_400)
    assert deleted == 1
    assert SchedulerHeartbeat.objects.filter(scheduler_id="fresh-scheduler").exists()


@pytest.mark.django_db
def test_scheduler_run_once_smoke():
    call_command("scheduler", "run", "--once")


@pytest.mark.django_db
@override_settings(EXECUTOR_BACKEND="inprocess")
def test_worker_run_once_smoke(job_factory, now):
    job = job_factory(enabled=True, next_run_at=now)
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="worker-smoke",
        status=ExecutionStatus.PENDING,
    )
    call_command("worker", "run", "--once", "--workers", "1")


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_public_job_list_hides_edit(client, job_factory):
    job_factory()
    response = client.get(reverse("scheduler_app:job_list"))
    content = response.content.decode()
    assert "Edit" not in content
    assert "Open" in content


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_public_execution_list_hides_inspect(client, job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="public-list",
        status=ExecutionStatus.SUCCEEDED,
    )
    response = client.get(reverse("scheduler_app:execution_list"))
    content = response.content.decode()
    assert "Sign in to inspect" in content
    assert reverse("scheduler_app:execution_detail", args=[JobExecution.objects.get().id]) not in content
