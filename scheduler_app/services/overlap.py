""" Overlap operations for the scheduler app. """

from __future__ import annotations

from datetime import datetime

from scheduler_app.models import (
    ACTIVE_EXECUTION_STATUSES,
    ExecutionStatus,
    JobExecution,
)


def has_active_execution(
    job_id: int,
    *,
    exclude_execution_id: int | None = None,
    now: datetime | None = None,
    include_pending: bool = True,
) -> bool:
    """Return True when overlap policies should treat the job as busy.

    In-flight work (claimed/running), queued pending rows (when
    ``include_pending``), and due retry_scheduled rows block overlap ``skip``
    and ``queue`` semantics. Claiming passes ``include_pending=False`` so
    multiple pending occurrences can be claimed in FIFO order once nothing is
    in flight.
    """
    queryset = JobExecution.objects.filter(job_id=job_id, status__in=ACTIVE_EXECUTION_STATUSES)
    if exclude_execution_id is not None:
        queryset = queryset.exclude(id=exclude_execution_id)
    if queryset.exists():
        return True

    if include_pending:
        pending = JobExecution.objects.filter(job_id=job_id, status=ExecutionStatus.PENDING)
        if exclude_execution_id is not None:
            pending = pending.exclude(id=exclude_execution_id)
        if pending.exists():
            return True

    due_retries = JobExecution.objects.filter(job_id=job_id, status=ExecutionStatus.RETRY_SCHEDULED)
    if now is not None:
        due_retries = due_retries.filter(run_after__lte=now)
    if exclude_execution_id is not None:
        due_retries = due_retries.exclude(id=exclude_execution_id)
    return due_retries.exists()
