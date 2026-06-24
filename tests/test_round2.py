""" Test round2 for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.claiming import cancel_queued_executions_for_job
from scheduler_app.services.task_config import validate_task_config


def test_task_config_rejects_non_numeric_seconds():
    with pytest.raises(ValueError, match="seconds must be a number"):
        validate_task_config("sleep_for_seconds", {"seconds": "slow"})


def test_task_config_rejects_seconds_above_cap():
    with pytest.raises(ValueError, match="at most 300"):
        validate_task_config("sleep_for_seconds", {"seconds": 900})


@pytest.mark.django_db
def test_disable_preserves_next_run_at(job_factory, now):
    job = job_factory(enabled=True, next_run_at=now + timedelta(hours=1))
    original = job.next_run_at
    job.enabled = False
    job.save(update_fields=["enabled"])
    cancel_queued_executions_for_job(job, reason="job disabled")
    job.refresh_from_db()
    assert job.next_run_at == original


@pytest.mark.django_db
def test_disable_cancels_claimed_execution(job_factory, now):
    job = job_factory(enabled=True)
    claimed = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="claimed-on-disable",
        status=ExecutionStatus.CLAIMED,
    )
    cancel_queued_executions_for_job(job, reason="job disabled")
    claimed.refresh_from_db()
    assert claimed.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_cli_edit_task_revalidates_existing_config(job_factory):
    job = job_factory(
        registered_task_name="sleep_for_seconds",
        task_config={"seconds": 1},
    )
    with pytest.raises(CommandError, match="unknown config keys"):
        call_command("job", "edit", str(job.id), "--task", "http_health_check_local")


@pytest.mark.django_db
def test_cli_trigger_rejects_disabled_job(job_factory):
    job = job_factory(enabled=False)
    with pytest.raises(CommandError, match="disabled"):
        call_command("job", "trigger", str(job.id))


@pytest.mark.django_db
def test_cli_secret_required_for_trigger(job_factory, settings):
    settings.SCHEDULER_CLI_SECRET = "test-secret"
    job = job_factory(enabled=True)
    with pytest.raises(CommandError, match="cli-secret"):
        call_command("job", "trigger", str(job.id))
    call_command("job", "trigger", str(job.id), "--cli-secret", "test-secret")


@pytest.mark.django_db
def test_fragments_require_auth_when_enabled(client):
    with override_settings(WEBUI_AUTH_ENABLED=True):
        redirect = client.get("/fragments/summary")
        assert redirect.status_code == 302
        assert "/accounts/login/" in redirect.url
        htmx = client.get("/fragments/summary", HTTP_HX_REQUEST="true")
        assert htmx.status_code == 401
        assert "Sign in" in htmx.content.decode()


@pytest.mark.django_db
def test_readyz_succeeds_with_locmem_cache(client):
    assert client.get("/readyz").status_code == 200
