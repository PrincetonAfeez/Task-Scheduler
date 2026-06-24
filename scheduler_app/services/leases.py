""" Lease operations for the scheduler app. """

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction

from scheduler_app.models import ACTIVE_EXECUTION_STATUSES, EventType, ExecutionStatus, JobExecution

from .alerts import dead_letter_execution
from .cache import invalidate_scheduler_cache
from .events import emit_event
from .retries import backoff_delay_seconds


def effective_lease_seconds(*, timeout_seconds: int, lease_seconds: int | None = None) -> int:
    """Lease must cover the full subprocess timeout plus a small buffer."""
    base = lease_seconds if lease_seconds is not None else getattr(settings, "LEASE_SECONDS", 120)
    buffer_seconds = getattr(settings, "LEASE_BUFFER_SECONDS", 30)
    return max(base, timeout_seconds + buffer_seconds)


@transaction.atomic
def recover_expired_leases(*, now: datetime, limit: int = 100) -> int:
    executions = list(
        JobExecution.objects.select_for_update(skip_locked=True)
        .select_related("job")
        .filter(status__in=ACTIVE_EXECUTION_STATUSES, lease_expires_at__lt=now)
        .order_by("lease_expires_at", "id")[:limit]
    )
    recovered = 0
    affected_job_ids: set[int] = set()
    for execution in executions:
        expired_at = execution.lease_expires_at
        if execution.attempt_number < execution.job.max_attempts:
            delay = backoff_delay_seconds(execution)
            execution.status = ExecutionStatus.RETRY_SCHEDULED
            execution.run_after = now + timedelta(seconds=delay)
            execution.claimed_by = ""
            execution.claimed_at = None
            execution.lease_expires_at = None
            execution.worker_id = ""
            execution.started_at = None
            execution.finished_at = None
            execution.duration_ms = None
            execution.error = (
                execution.error
                or f"Lease expired at {expired_at}; execution requeued for retry"
            )
            execution.save(
                update_fields=[
                    "status",
                    "run_after",
                    "claimed_by",
                    "claimed_at",
                    "lease_expires_at",
                    "worker_id",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "error",
                    "updated_at",
                ]
            )
        else:
            execution.status = ExecutionStatus.DEAD_LETTERED
            execution.claimed_by = ""
            execution.claimed_at = None
            execution.lease_expires_at = None
            execution.worker_id = ""
            execution.started_at = None
            execution.finished_at = None
            execution.duration_ms = None
            execution.error = execution.error or "Lease expired and attempts were exhausted"
            execution.save(
                update_fields=[
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "lease_expires_at",
                    "worker_id",
                    "started_at",
                    "finished_at",
                    "duration_ms",
                    "error",
                    "updated_at",
                ]
            )
            dead_letter_execution(execution, reason="expired lease", final_error=execution.error)
        emit_event(
            EventType.LEASE_RECOVERY,
            job=execution.job,
            execution=execution,
            message=f"Recovered expired lease for execution {execution.id}",
            data={"new_status": execution.status},
        )
        affected_job_ids.add(execution.job_id)
        recovered += 1

    if recovered:
        invalidate_scheduler_cache("expired leases recovered", job_ids=affected_job_ids)
    return recovered
