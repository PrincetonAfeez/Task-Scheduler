""" Test CLI audit for the scheduler app. """

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from scheduler_app.models import Alert


@pytest.mark.django_db
def test_cli_edit_preserves_next_run_when_only_name_changes(job_factory):
    job = job_factory(name="before")
    original_next = job.next_run_at
    call_command("job", "edit", str(job.id), "--name", "after")
    job.refresh_from_db()
    assert job.name == "after"
    assert job.next_run_at == original_next


@pytest.mark.django_db
def test_cli_alerts_resolve():
    alert = Alert.objects.create(message="test alert")
    call_command("alerts", "resolve", str(alert.id), stdout=StringIO())
    alert.refresh_from_db()
    assert alert.resolved is True


@pytest.mark.django_db
def test_cli_job_add_rejects_invalid_task_config():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="unknown config keys"):
        call_command(
            "job",
            "add",
            "bad-config",
            "sleep_for_seconds",
            "interval",
            "--every",
            "30s",
            "--config",
            '{"not_allowed": true}',
        )


@pytest.mark.django_db
def test_cli_disable_cancels_queued(job_factory, now):
    from scheduler_app.models import ExecutionStatus, JobExecution

    job = job_factory(enabled=True, next_run_at=now)
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="disable-me",
        status=ExecutionStatus.PENDING,
    )
    call_command("job", "disable", str(job.id))
    assert not JobExecution.objects.filter(job=job, status=ExecutionStatus.PENDING).exists()
