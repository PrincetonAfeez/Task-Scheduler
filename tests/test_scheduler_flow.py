""" Test scheduler flow for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler_app.models import ExecutionStatus, JobExecution, MisfirePolicy, ScheduleType
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.leases import effective_lease_seconds
from scheduler_app.services.due import SchedulerService, create_execution_for_occurrence


@pytest.mark.django_db
def test_scheduler_creates_one_time_execution_and_disables_job(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    result = SchedulerService(clock=FrozenClock(now), scheduler_id="test-scheduler").tick()
    job.refresh_from_db()
    assert result.created == 1
    assert job.enabled is False
    execution = job.executions.get()
    assert execution.status == ExecutionStatus.PENDING
    assert execution.scheduled_for == now


@pytest.mark.django_db
def test_duplicate_occurrence_is_not_inserted(job_factory, now):
    job = job_factory()
    first, created_first = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    second, created_second = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert JobExecution.objects.filter(job=job, scheduled_for=now).count() == 1


@pytest.mark.django_db
def test_claim_runnable_execution_sets_lease(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="claim-me",
        status=ExecutionStatus.PENDING,
    )
    claimed = claim_runnable_executions(worker_id="worker-a", now=now, limit=1, lease_seconds=30)
    execution.refresh_from_db()
    assert [item.id for item in claimed] == [execution.id]
    assert execution.status == ExecutionStatus.CLAIMED
    assert execution.lease_expires_at == now + timedelta(
        seconds=effective_lease_seconds(timeout_seconds=job.timeout_seconds, lease_seconds=30)
    )


@pytest.mark.django_db
def test_misfire_skip_marks_old_occurrences_missed(job_factory, now):
    job = job_factory(
        misfire_policy=MisfirePolicy.SKIP,
        misfire_grace_seconds=30,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=2)).isoformat()},
        next_run_at=now - timedelta(minutes=2),
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="test-scheduler").tick()
    statuses = list(job.executions.order_by("scheduled_for").values_list("status", flat=True))
    assert statuses[:2] == [ExecutionStatus.MISSED, ExecutionStatus.MISSED]
    assert statuses[-1] == ExecutionStatus.PENDING

