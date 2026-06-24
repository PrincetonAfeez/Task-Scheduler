""" Claiming operations for the scheduler app. """

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction

from scheduler_app.models import (
    EventType,
    ExecutionStatus,
    Job,
    JobExecution,
    OverlapPolicy,
    RUNNABLE_EXECUTION_STATUSES,
)

from .cache import invalidate_scheduler_cache
from .events import emit_event
from .leases import effective_lease_seconds
from .overlap import has_active_execution

CANCELLABLE_STATUSES = [
    ExecutionStatus.PENDING,
    ExecutionStatus.CLAIMED,
    ExecutionStatus.RETRY_SCHEDULED,
]

QUEUED_CANCELLABLE_STATUSES = [
    ExecutionStatus.PENDING,
    ExecutionStatus.RETRY_SCHEDULED,
    ExecutionStatus.CLAIMED,
]


@transaction.atomic
def claim_runnable_executions(
    *,
    worker_id: str,
    now: datetime,
    limit: int = 1,
    lease_seconds: int | None = None,
) -> list[JobExecution]:
    base_lease = lease_seconds or getattr(settings, "LEASE_SECONDS", 120)
    candidates = list(
        JobExecution.objects.select_for_update(skip_locked=True)
        .select_related("job")
        .filter(status__in=RUNNABLE_EXECUTION_STATUSES, run_after__lte=now)
        .order_by("run_after", "id")[: max(limit * 3, limit)]
    )
    claimed: list[JobExecution] = []
    skipped: list[JobExecution] = []
    for execution in candidates:
        if len(claimed) >= limit:
            break
        if not execution.job.enabled:
            if execution.status in QUEUED_CANCELLABLE_STATUSES:
                execution.status = ExecutionStatus.CANCELLED
                execution.output = "job disabled"
                execution.lease_expires_at = None
                execution.claimed_by = ""
                execution.claimed_at = None
                execution.worker_id = ""
                execution.save(
                    update_fields=[
                        "status",
                        "output",
                        "lease_expires_at",
                        "claimed_by",
                        "claimed_at",
                        "worker_id",
                        "updated_at",
                    ]
                )
                emit_event(
                    EventType.CANCELLED,
                    job=execution.job,
                    execution=execution,
                    message="job disabled",
                )
                skipped.append(execution)
            continue
        if execution.job.overlap_policy != OverlapPolicy.ALLOW and has_active_execution(
            execution.job_id,
            exclude_execution_id=execution.id,
            now=now,
            include_pending=False,
        ):
            # queue: leave the occurrence pending so it runs once the active run ends.
            # skip: the previous run is still active, so this occurrence is dropped.
            if execution.job.overlap_policy == OverlapPolicy.SKIP:
                execution.status = ExecutionStatus.MISSED
                execution.output = "overlap skipped: a previous run was still active at claim time"
                execution.save(update_fields=["status", "output", "updated_at"])
                emit_event(
                    EventType.MISFIRE,
                    job=execution.job,
                    execution=execution,
                    message=f"overlap skipped execution {execution.id} at claim time",
                )
                skipped.append(execution)
            continue

        previous_status = execution.status
        if previous_status == ExecutionStatus.RETRY_SCHEDULED:
            execution.attempt_number += 1
        execution.status = ExecutionStatus.CLAIMED
        execution.claimed_by = worker_id
        execution.claimed_at = now
        execution.lease_expires_at = now + timedelta(
            seconds=effective_lease_seconds(
                timeout_seconds=execution.job.timeout_seconds,
                lease_seconds=base_lease,
            )
        )
        execution.worker_id = worker_id
        execution.save(
            update_fields=[
                "status",
                "attempt_number",
                "claimed_by",
                "claimed_at",
                "lease_expires_at",
                "worker_id",
                "updated_at",
            ]
        )
        emit_event(
            EventType.CLAIM,
            job=execution.job,
            execution=execution,
            message=f"{worker_id} claimed execution {execution.id}",
            data={"attempt_number": execution.attempt_number},
        )
        claimed.append(execution)

    if claimed or skipped:
        affected = {execution.job_id for execution in claimed + skipped}
        invalidate_scheduler_cache(
            "executions claimed",
            job_ids=affected,
        )
    return claimed


@transaction.atomic
def cancel_execution(execution: JobExecution, *, reason: str = "operator cancelled") -> JobExecution:
    execution = JobExecution.objects.select_for_update().select_related("job").get(pk=execution.pk)
    if execution.status not in CANCELLABLE_STATUSES:
        raise ValueError(f"cannot cancel execution in status {execution.status}")
    execution.status = ExecutionStatus.CANCELLED
    execution.output = reason
    execution.lease_expires_at = None
    execution.claimed_by = ""
    execution.claimed_at = None
    execution.worker_id = ""
    execution.save(
        update_fields=[
            "status",
            "output",
            "lease_expires_at",
            "claimed_by",
            "claimed_at",
            "worker_id",
            "updated_at",
        ]
    )
    emit_event(
        EventType.CANCELLED,
        job=execution.job,
        execution=execution,
        message=reason,
    )
    invalidate_scheduler_cache("execution cancelled", job=execution.job, execution=execution)
    return execution


@transaction.atomic
def cancel_queued_executions_for_job(job: Job, *, reason: str = "job disabled") -> int:
    executions = list(
        JobExecution.objects.select_for_update()
        .filter(job=job, status__in=QUEUED_CANCELLABLE_STATUSES)
        .order_by("id")
    )
    for execution in executions:
        execution.status = ExecutionStatus.CANCELLED
        execution.output = reason
        execution.lease_expires_at = None
        execution.claimed_by = ""
        execution.claimed_at = None
        execution.worker_id = ""
        execution.save(
            update_fields=[
                "status",
                "output",
                "lease_expires_at",
                "claimed_by",
                "claimed_at",
                "worker_id",
                "updated_at",
            ]
        )
        emit_event(
            EventType.CANCELLED,
            job=job,
            execution=execution,
            message=reason,
        )
    if executions:
        invalidate_scheduler_cache("queued executions cancelled", job=job)
    return len(executions)
