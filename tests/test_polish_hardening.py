"""Polish and hardening regression tests (overlap, deploy checks, audit trail, shutdown)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse

from scheduler_app.forms import JobForm
from scheduler_app.models import EventType, ExecutionStatus, Job, JobExecution, MisfirePolicy, OverlapPolicy, ScheduleType
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService
from scheduler_app.services.overlap import has_active_execution
from scheduler_app.services.shutdown import interruptible_sleep, reset_shutdown_flag


@pytest.mark.django_db
def test_overlap_skip_considers_pending_at_tick(job_factory, now):
    job = job_factory(
        overlap_policy=OverlapPolicy.SKIP,
        next_run_at=now,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
    )
    JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(minutes=1),
        run_after=now - timedelta(minutes=1),
        idempotency_key="existing-pending",
        status=ExecutionStatus.PENDING,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="overlap-pending-tick").tick()
    assert job.executions.filter(status=ExecutionStatus.MISSED).exists()


@pytest.mark.django_db
def test_overlap_skip_catch_up_creates_one_pending_per_tick(job_factory, now):
    job = job_factory(
        overlap_policy=OverlapPolicy.SKIP,
        misfire_policy=MisfirePolicy.CATCH_UP,
        next_run_at=now - timedelta(minutes=3),
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=10)).isoformat()},
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="overlap-catchup").tick()
    pending = job.executions.filter(status=ExecutionStatus.PENDING)
    missed = job.executions.filter(status=ExecutionStatus.MISSED)
    assert pending.count() == 1
    assert missed.count() >= 1


@pytest.mark.django_db
def test_has_active_execution_includes_due_retry_scheduled(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(minutes=5),
        run_after=now - timedelta(minutes=1),
        idempotency_key="due-retry",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    assert has_active_execution(job.id, now=now) is True


@pytest.mark.django_db
def test_has_active_execution_ignores_future_retry_scheduled(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(minutes=5),
        run_after=now + timedelta(minutes=10),
        idempotency_key="future-retry",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    assert has_active_execution(job.id, now=now) is False


def test_interruptible_sleep_returns_true_when_shutdown_requested():
    import scheduler_app.services.shutdown as shutdown_module

    reset_shutdown_flag()
    shutdown_module._shutdown_requested = True
    try:
        assert interruptible_sleep(5.0) is True
    finally:
        reset_shutdown_flag()


@pytest.mark.django_db
def test_cli_secret_missing_emits_deploy_warning():
    from scheduler_app.checks import scheduler_cli_secret_missing_check

    with override_settings(DEBUG=True, SCHEDULER_CLI_SECRET=""):
        assert not scheduler_cli_secret_missing_check(None)
    with override_settings(DEBUG=False, SCHEDULER_CLI_SECRET=""):
        warnings = scheduler_cli_secret_missing_check(None)
        assert len(warnings) == 1
        assert warnings[0].id == "scheduler_app.W002"


@pytest.mark.django_db
def test_job_form_rejects_invalid_timezone():
    form = JobForm(
        data={
            "name": "tz-test",
            "description": "",
            "registered_task_name": "always_succeed",
            "task_config": "{}",
            "schedule_type": ScheduleType.INTERVAL,
            "schedule_value": '{"every": "30s"}',
            "timezone": "Not/A_Real_Zone",
            "enabled": "on",
            "overlap_policy": "skip",
            "misfire_policy": "coalesce",
            "misfire_grace_seconds": "60",
            "max_attempts": "3",
            "retry_backoff_seconds": "10",
            "timeout_seconds": "30",
            "retention_count": "500",
            "retention_days": "30",
            "alert_mode": "web",
        }
    )
    assert form.is_valid() is False
    assert "timezone" in form.errors


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_htmx_fragment_includes_sign_in_link(client):
    response = client.get(
        reverse("scheduler_app:summary_fragment"),
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 401
    assert "/accounts/login/" in response.content.decode()


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_job_create_emits_operator_audit_event(auth_client):
    data = {
        "name": "audited-job",
        "description": "",
        "registered_task_name": "always_succeed",
        "task_config": "{}",
        "schedule_type": "interval",
        "schedule_value": '{"every": "30s"}',
        "timezone": "UTC",
        "enabled": "on",
        "overlap_policy": "skip",
        "misfire_policy": "coalesce",
        "misfire_grace_seconds": "60",
        "max_attempts": "3",
        "retry_backoff_seconds": "10",
        "timeout_seconds": "30",
        "retention_count": "500",
        "retention_days": "30",
        "alert_mode": "web",
    }
    response = auth_client.post(reverse("scheduler_app:job_create"), data)
    assert response.status_code == 302
    job = Job.objects.get(name="audited-job")
    assert job.events.filter(event_type=EventType.OPERATOR_ACTION).exists()


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_alert_resolve_bulk(auth_client):
    from scheduler_app.models import Alert

    alerts = [
        Alert.objects.create(message=f"bulk-{index}", resolved=False)
        for index in range(2)
    ]
    response = auth_client.post(
        reverse("scheduler_app:alert_resolve_bulk"),
        {"alert_ids": [str(alert.id) for alert in alerts]},
    )
    assert response.status_code == 302
    for alert in alerts:
        alert.refresh_from_db()
        assert alert.resolved is True


@pytest.mark.django_db
def test_has_active_execution_ignores_pending_when_disabled_for_claim(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="pending-only",
        status=ExecutionStatus.PENDING,
    )
    assert has_active_execution(job.id, now=now, include_pending=True) is True
    assert has_active_execution(job.id, now=now, include_pending=False) is False


@pytest.mark.django_db
def test_cli_job_add_emits_operator_audit_event():
    from django.core.management import call_command

    from scheduler_app.models import EventType, Job

    call_command(
        "job",
        "add",
        "cli-audited",
        "always_succeed",
        "interval",
        "--every",
        "60s",
    )
    job = Job.objects.get(name="cli-audited")
    event = job.events.filter(event_type=EventType.OPERATOR_ACTION).get()
    assert event.data["source"] == "cli"
    assert event.data["action"] == "job_create"


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_execution_cancel_rejects_running(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="running-cancel",
        status=ExecutionStatus.RUNNING,
    )
    response = auth_client.post(
        reverse("scheduler_app:execution_action", args=[execution.id, "cancel"]),
    )
    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.RUNNING
