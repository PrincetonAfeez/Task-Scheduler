"""Additional coverage for job CLI, retries, registry, settings, and auth helpers."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.retries import retry_execution
from scheduler_app.tasks.registry import catalog_metadata, load_default_tasks, registered_tasks


@pytest.mark.django_db
def test_job_cli_add_interval_cron_onetime(now):
    call_command("job", "add", "cli-interval", "always_succeed", "interval", "--every", "45s")
    call_command("job", "add", "cli-cron", "always_succeed", "cron", "--cron", "0 * * * *")
    run_at = now.isoformat()
    call_command("job", "add", "cli-onetime", "always_succeed", "one_time", "--run-at", run_at)
    out = StringIO()
    call_command("job", "list", stdout=out)
    body = out.getvalue()
    assert "cli-interval" in body
    assert "cli-cron" in body
    assert "cli-onetime" in body


@pytest.mark.django_db
def test_job_cli_preview_enable_disable_delete(job_factory, now):
    job = job_factory(name="cli-lifecycle")
    out = StringIO()
    call_command("job", "preview", str(job.id), stdout=out)
    assert out.getvalue().strip()

    call_command("job", "disable", str(job.id))
    job.refresh_from_db()
    assert job.enabled is False
    call_command("job", "enable", str(job.id))
    job.refresh_from_db()
    assert job.enabled is True
    call_command("job", "delete", str(job.id), "--yes")
    from scheduler_app.models import Job

    assert not Job.objects.filter(pk=job.id).exists()


@pytest.mark.django_db
def test_job_cli_trigger(job_factory, now):
    job = job_factory(enabled=True)
    call_command("job", "trigger", str(job.id))
    assert JobExecution.objects.filter(job=job, is_manual=True).exists()


@pytest.mark.django_db
def test_retry_execution_rejects_non_retryable(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="non-retryable",
        status=ExecutionStatus.RUNNING,
    )
    with pytest.raises(ValueError, match="not retryable"):
        retry_execution(execution, now=now)


def test_registry_load_idempotent_and_catalog():
    load_default_tasks()
    load_default_tasks()
    tasks = registered_tasks()
    assert "always_succeed" in tasks
    metadata = catalog_metadata()
    names = {item["name"] for item in metadata}
    assert "always_succeed" in names


@pytest.mark.django_db
def test_alerts_resolve_already_resolved(job_factory):
    from scheduler_app.models import Alert

    job = job_factory()
    alert = Alert.objects.create(job=job, message="done", resolved=True)
    out = StringIO()
    call_command("alerts", "resolve", str(alert.id), stdout=out)
    assert "already resolved" in out.getvalue()


@pytest.mark.django_db
def test_alerts_resolve_not_found():
    with pytest.raises(CommandError, match="not found"):
        call_command("alerts", "resolve", "999999")


@pytest.mark.django_db
@pytest.mark.parametrize("command", ["scheduler", "worker", "demo", "execution", "alerts"])
def test_management_command_rejects_unknown_subcommand(command):
    with pytest.raises(CommandError):
        call_command(command, "bogus")
