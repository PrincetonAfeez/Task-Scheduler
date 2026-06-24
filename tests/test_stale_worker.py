""" Test stale worker for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.executors import TaskRunResult
from scheduler_app.services.leases import recover_expired_leases
from scheduler_app.services.worker import execute_claimed_execution


class SlowSuccessExecutor:
    def run(self, **kwargs):
        return TaskRunResult(status=ExecutionStatus.SUCCEEDED, output="done", duration_ms=1)


@pytest.mark.django_db
def test_stale_worker_result_discarded_after_lease_recovery(job_factory, now):
    job = job_factory(registered_task_name="always_succeed", timeout_seconds=300, retry_backoff_seconds=0)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="stale-worker",
        status=ExecutionStatus.PENDING,
    )
    worker_a = "worker-a-stale-test"
    worker_b = "worker-b-stale-test"

    claim_runnable_executions(worker_id=worker_a, now=now, limit=1, lease_seconds=5)
    execution.refresh_from_db()
    execution.status = ExecutionStatus.RUNNING
    execution.worker_id = worker_a
    execution.lease_expires_at = now - timedelta(seconds=1)
    execution.save(update_fields=["status", "worker_id", "lease_expires_at", "updated_at"])

    recover_expired_leases(now=now + timedelta(seconds=10))
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.RETRY_SCHEDULED

    claim_runnable_executions(worker_id=worker_b, now=now + timedelta(seconds=11), limit=1)
    execution.refresh_from_db()
    assert execution.worker_id == worker_b
    assert execution.status in {ExecutionStatus.CLAIMED, ExecutionStatus.RUNNING}

    result = execute_claimed_execution(
        execution.id,
        worker_id=worker_a,
        clock=FrozenClock(now + timedelta(seconds=12)),
        executor=SlowSuccessExecutor(),
    )
    execution.refresh_from_db()
    assert execution.worker_id == worker_b
    assert execution.status == ExecutionStatus.CLAIMED
    assert result.status == ExecutionStatus.CLAIMED
