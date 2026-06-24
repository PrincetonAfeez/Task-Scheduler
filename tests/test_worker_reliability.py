""" Test worker reliability for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler_app.models import Alert, ExecutionStatus, JobExecution, WorkerHeartbeat
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.executors import InProcessExecutor, TaskRunResult
from scheduler_app.services.leases import recover_expired_leases
from scheduler_app.services.worker import execute_claimed_execution


class FailingExecutor:
    def run(self, **kwargs):
        return TaskRunResult(status=ExecutionStatus.FAILED, error="boom", duration_ms=1)


@pytest.mark.django_db
def test_worker_success_records_output(job_factory, now):
    job = job_factory(registered_task_name="always_succeed")
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="success",
        status=ExecutionStatus.PENDING,
    )
    claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    result = execute_claimed_execution(
        execution.id,
        worker_id="worker-a",
        clock=FrozenClock(now),
        executor=InProcessExecutor(),
    )
    assert result.status == ExecutionStatus.SUCCEEDED
    assert "Task succeeded" in result.output


@pytest.mark.django_db
def test_worker_heartbeat_accumulates_completed_count(job_factory, now):
    for index in range(2):
        job = job_factory(name=f"accumulate-{index}", registered_task_name="always_succeed")
        execution = JobExecution.objects.create(
            job=job,
            scheduled_for=now,
            run_after=now,
            idempotency_key=f"accumulate-{index}",
            status=ExecutionStatus.PENDING,
        )
        claim_runnable_executions(worker_id="worker-acc", now=now, limit=1)
        execute_claimed_execution(
            execution.id,
            worker_id="worker-acc",
            clock=FrozenClock(now),
            executor=InProcessExecutor(),
        )
    heartbeat = WorkerHeartbeat.objects.get(worker_id="worker-acc")
    assert heartbeat.completed_count == 2


@pytest.mark.django_db
def test_failed_execution_schedules_retry(job_factory, now):
    job = job_factory(max_attempts=2, retry_backoff_seconds=15)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="retry",
        status=ExecutionStatus.PENDING,
    )
    claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    result = execute_claimed_execution(
        execution.id,
        worker_id="worker-a",
        clock=FrozenClock(now),
        executor=FailingExecutor(),
    )
    assert result.status == ExecutionStatus.RETRY_SCHEDULED
    assert result.run_after == now + timedelta(seconds=15)


@pytest.mark.django_db
def test_failed_execution_dead_letters_after_attempts_exhausted(job_factory, now):
    job = job_factory(max_attempts=1)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="dead",
        status=ExecutionStatus.PENDING,
    )
    claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    result = execute_claimed_execution(
        execution.id,
        worker_id="worker-a",
        clock=FrozenClock(now),
        executor=FailingExecutor(),
    )
    # Retries exhausted: the row keeps its truthful terminal status (failed),
    # and a DeadLetter record + alert flag it for operators.
    assert result.status == ExecutionStatus.FAILED
    assert hasattr(result, "dead_letter")
    assert result.dead_letter.attempts_used == 1


@pytest.mark.django_db
def test_expired_lease_requeues_when_attempts_remain(job_factory, now):
    job = job_factory(max_attempts=2)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="lease",
        status=ExecutionStatus.RUNNING,
        attempt_number=1,
        lease_expires_at=now - timedelta(seconds=1),
    )
    recovered = recover_expired_leases(now=now)
    execution.refresh_from_db()
    assert recovered == 1
    assert execution.status == ExecutionStatus.RETRY_SCHEDULED


@pytest.mark.django_db
def test_expired_lease_dead_letters_when_attempts_exhausted(job_factory, now):
    job = job_factory(max_attempts=1)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="lease-dead",
        status=ExecutionStatus.RUNNING,
        attempt_number=1,
        lease_expires_at=now - timedelta(seconds=1),
    )
    recovered = recover_expired_leases(now=now)
    execution.refresh_from_db()
    assert recovered == 1
    assert execution.status == ExecutionStatus.DEAD_LETTERED
    assert hasattr(execution, "dead_letter")


@pytest.mark.django_db
def test_dead_letter_raises_web_alert(job_factory, now):
    job = job_factory(max_attempts=1, alert_mode="web")
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="alert-web",
        status=ExecutionStatus.RUNNING,
        lease_expires_at=now - timedelta(seconds=1),
    )
    recover_expired_leases(now=now)
    assert Alert.objects.filter(execution=execution).exists()


@pytest.mark.django_db
def test_log_only_alert_mode_skips_alert_row(job_factory, now):
    job = job_factory(max_attempts=1, alert_mode="log_only")
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="alert-log",
        status=ExecutionStatus.RUNNING,
        lease_expires_at=now - timedelta(seconds=1),
    )
    recover_expired_leases(now=now)
    execution.refresh_from_db()
    # log_only still records the dead letter, but no operator-visible Alert row.
    assert hasattr(execution, "dead_letter")
    assert not Alert.objects.filter(execution=execution).exists()

