""" Test audit fixes for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.cache import job_stats_key, upcoming_job_key
from scheduler_app.services.claiming import cancel_execution, cancel_queued_executions_for_job
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService
from scheduler_app.services.leases import effective_lease_seconds
from scheduler_app.services.task_config import validate_task_config


@pytest.mark.django_db
def test_scheduler_tick_invalidates_per_job_cache(job_factory, now, django_capture_on_commit_callbacks):
    job = job_factory(next_run_at=now)
    cache.set(job_stats_key(job.id), {"stale": True})
    cache.set(upcoming_job_key(job.id), ["stale"])
    with django_capture_on_commit_callbacks(execute=True):
        SchedulerService(clock=FrozenClock(now), scheduler_id="cache-test").tick()
    assert cache.get(job_stats_key(job.id)) is None
    assert cache.get(upcoming_job_key(job.id)) is None


def test_effective_lease_covers_task_timeout():
    assert effective_lease_seconds(timeout_seconds=300, lease_seconds=120) == 330


def test_task_config_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown config keys"):
        validate_task_config("sleep_for_seconds", {"seconds": 1, "bogus": True})


@pytest.mark.django_db
def test_disable_job_cancels_queued_executions(job_factory, now):
    job = job_factory(enabled=True)
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="queued-pending",
        status=ExecutionStatus.PENDING,
    )
    retry = JobExecution.objects.create(
        job=job,
        scheduled_for=now + timedelta(seconds=1),
        run_after=now + timedelta(seconds=30),
        idempotency_key="queued-retry",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    cancelled = cancel_queued_executions_for_job(job, reason="job disabled")
    pending.refresh_from_db()
    retry.refresh_from_db()
    assert cancelled == 2
    assert pending.status == ExecutionStatus.CANCELLED
    assert retry.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_cancel_retry_scheduled_execution(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cancel-retry",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    cancel_execution(execution)
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_readyz_reports_database(client, db):
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").content == b"ready"


@pytest.mark.django_db
def test_healthz_is_liveness_only(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").content == b"ok"
