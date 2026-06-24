""" Test timeout for the scheduler app. """

from __future__ import annotations

import pytest

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.executors import SubprocessExecutor, TaskRunResult
from scheduler_app.services.worker import execute_claimed_execution
from scheduler_app.tasks.registry import TaskContext


class TimingOutExecutor:
    def run(self, **kwargs):
        return TaskRunResult(status=ExecutionStatus.TIMED_OUT, error="hard timeout", duration_ms=1000)


@pytest.mark.django_db
def test_timed_out_execution_marks_timed_out_and_dead_letters(job_factory, now):
    job = job_factory(max_attempts=1)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="timeout",
        status=ExecutionStatus.PENDING,
    )
    claim_runnable_executions(worker_id="worker-a", now=now, limit=1)
    result = execute_claimed_execution(
        execution.id,
        worker_id="worker-a",
        clock=FrozenClock(now),
        executor=TimingOutExecutor(),
    )
    assert result.status == ExecutionStatus.TIMED_OUT
    assert hasattr(result, "dead_letter")


def test_subprocess_executor_enforces_hard_timeout():
    """A task that runs longer than its limit is killed via process isolation."""
    context = TaskContext(
        execution_id=1,
        job_id=1,
        attempt_number=1,
        idempotency_key="timeout-task",
        scheduled_for=None,
        worker_id="worker-a",
    )
    result = SubprocessExecutor().run(
        task_name="sleep_for_seconds",
        config={"seconds": 5},
        context=context,
        timeout_seconds=1,
    )
    assert result.status == ExecutionStatus.TIMED_OUT
