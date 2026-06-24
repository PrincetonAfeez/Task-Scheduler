""" Test retention for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.retention import prune_job_history


def _make_execution(job, key, *, status=ExecutionStatus.SUCCEEDED):
    return JobExecution.objects.create(
        job=job,
        scheduled_for=timezone.now(),
        run_after=timezone.now(),
        idempotency_key=key,
        status=status,
    )


@pytest.mark.django_db
def test_retention_prunes_by_count(job_factory):
    job = job_factory(retention_count=2, retention_days=0)
    for index in range(5):
        _make_execution(job, f"count-{index}")

    deleted = prune_job_history(job)

    assert deleted == 3
    assert job.executions.count() == 2


@pytest.mark.django_db
def test_retention_prunes_by_age(job_factory):
    job = job_factory(retention_count=0, retention_days=7)
    old = _make_execution(job, "old")
    JobExecution.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
    recent = _make_execution(job, "recent")

    deleted = prune_job_history(job)

    assert deleted == 1
    assert job.executions.filter(pk=recent.pk).exists()
    assert not job.executions.filter(pk=old.pk).exists()


@pytest.mark.django_db
def test_retention_preserves_dead_lettered_executions(job_factory):
    from scheduler_app.services.alerts import dead_letter_execution

    job = job_factory(retention_count=1, retention_days=0)
    flagged = _make_execution(job, "dead", status=ExecutionStatus.FAILED)
    dead_letter_execution(flagged, reason="test")
    _make_execution(job, "ok-1")
    _make_execution(job, "ok-2")

    prune_job_history(job)

    # The dead-letter audit trail must survive retention pruning.
    assert job.executions.filter(pk=flagged.pk).exists()


@pytest.mark.django_db
def test_retention_keeps_active_executions(job_factory):
    job = job_factory(retention_count=1, retention_days=0)
    _make_execution(job, "done-1")
    _make_execution(job, "done-2")
    running = _make_execution(job, "running", status=ExecutionStatus.RUNNING)

    prune_job_history(job)

    # Non-terminal executions are never pruned regardless of retention count.
    assert job.executions.filter(pk=running.pk).exists()
