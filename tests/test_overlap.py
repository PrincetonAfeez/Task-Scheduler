""" Test overlap for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler_app.models import ExecutionStatus, JobExecution, OverlapPolicy
from scheduler_app.services.claiming import claim_runnable_executions


def _running_and_pending(job, now):
    # An earlier occurrence is still running while a newer one becomes due.
    running = JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(seconds=60),
        run_after=now - timedelta(seconds=60),
        idempotency_key="overlap-running",
        status=ExecutionStatus.RUNNING,
    )
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="overlap-pending",
        status=ExecutionStatus.PENDING,
    )
    return running, pending


@pytest.mark.django_db
def test_overlap_allow_claims_despite_active_run(job_factory, now):
    job = job_factory(overlap_policy=OverlapPolicy.ALLOW)
    _running, pending = _running_and_pending(job, now)
    claimed = claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    assert [item.id for item in claimed] == [pending.id]
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.CLAIMED


@pytest.mark.django_db
def test_overlap_skip_marks_new_occurrence_missed(job_factory, now):
    job = job_factory(overlap_policy=OverlapPolicy.SKIP)
    _running, pending = _running_and_pending(job, now)
    claimed = claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    assert claimed == []
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.MISSED


@pytest.mark.django_db
def test_overlap_queue_leaves_occurrence_pending(job_factory, now):
    job = job_factory(overlap_policy=OverlapPolicy.QUEUE)
    _running, pending = _running_and_pending(job, now)
    claimed = claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    assert claimed == []
    pending.refresh_from_db()
    assert pending.status == ExecutionStatus.PENDING


@pytest.mark.django_db
def test_overlap_queue_claims_oldest_pending_when_multiple_queued(job_factory, now):
    job = job_factory(overlap_policy=OverlapPolicy.QUEUE)
    older = JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(seconds=120),
        run_after=now - timedelta(seconds=120),
        idempotency_key="queue-older",
        status=ExecutionStatus.PENDING,
    )
    newer = JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(seconds=60),
        run_after=now - timedelta(seconds=60),
        idempotency_key="queue-newer",
        status=ExecutionStatus.PENDING,
    )
    claimed = claim_runnable_executions(worker_id="worker-queue-fifo", now=now, limit=1)
    assert [item.id for item in claimed] == [older.id]
    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.status == ExecutionStatus.CLAIMED
    assert newer.status == ExecutionStatus.PENDING
