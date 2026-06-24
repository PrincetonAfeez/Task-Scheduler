""" Retry operations for the scheduler app. """

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from django.db import transaction

from scheduler_app.models import EventType, ExecutionStatus, JobExecution

from .alerts import dead_letter_execution
from .cache import invalidate_scheduler_cache
from .events import emit_event

RETRYABLE_STATUSES = frozenset(
    {
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
        ExecutionStatus.DEAD_LETTERED,
        ExecutionStatus.CANCELLED,
    }
)


def backoff_delay_seconds(execution: JobExecution) -> int:
    base = execution.job.retry_backoff_seconds
    multiplier = 2 ** max(execution.attempt_number - 1, 0)
    return base * multiplier


@transaction.atomic
def apply_failure_transition(
    execution: JobExecution,
    *,
    terminal_status: str,
    error: str,
    now: datetime,
) -> JobExecution:
    execution.error = error[:20_000]
    execution.lease_expires_at = None
    execution.claimed_by = ""
    execution.claimed_at = None

    if execution.attempt_number < execution.job.max_attempts:
        delay = backoff_delay_seconds(execution)
        execution.status = ExecutionStatus.RETRY_SCHEDULED
        execution.run_after = now + timedelta(seconds=delay)
        execution.save(
            update_fields=[
                "status",
                "run_after",
                "error",
                "lease_expires_at",
                "claimed_by",
                "claimed_at",
                "updated_at",
            ]
        )
        emit_event(
            EventType.RETRY_SCHEDULED,
            job=execution.job,
            execution=execution,
            message=f"Retry scheduled in {delay} seconds",
            data={"next_attempt": execution.attempt_number + 1, "delay_seconds": delay},
        )
    else:
        # Keep the truthful terminal status (failed/timed_out) so run history stays
        # informative; the DeadLetter record + alert are what flag it for operators.
        execution.status = terminal_status
        execution.run_after = now
        execution.save(
            update_fields=[
                "status",
                "run_after",
                "error",
                "lease_expires_at",
                "claimed_by",
                "claimed_at",
                "updated_at",
            ]
        )
        dead_letter_execution(
            execution,
            reason=f"{terminal_status} after {execution.attempt_number} attempts",
            final_error=error,
        )

    invalidate_scheduler_cache("execution failure transition", job=execution.job, execution=execution)
    return execution


@transaction.atomic
def retry_execution(original: JobExecution, *, now: datetime) -> JobExecution:
    if original.status not in RETRYABLE_STATUSES:
        raise ValueError(f"execution status {original.status} is not retryable")
    retry = JobExecution.objects.create(
        job=original.job,
        scheduled_for=now,
        run_after=now,
        status=ExecutionStatus.PENDING,
        attempt_number=1,
        idempotency_key=f"manual-retry:{original.id}:{uuid.uuid4()}",
        is_manual=True,
        output=f"Manual retry of execution {original.id}",
    )
    emit_event(
        EventType.MANUAL_RETRY,
        job=original.job,
        execution=retry,
        message=f"Manual retry created from execution {original.id}",
        data={"original_execution_id": original.id},
    )
    invalidate_scheduler_cache("execution manually retried", job=original.job, execution=retry)
    return retry

