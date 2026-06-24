""" Due operations for the scheduler app. """

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction

from scheduler_app.models import (
    EventType,
    ExecutionStatus,
    Job,
    JobExecution,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleType,
)

from .cache import invalidate_scheduler_cache
from .clock import Clock, SystemClock
from .events import emit_event
from .health import update_scheduler_heartbeat
from .overlap import has_active_execution
from .schedules import coalesced_due_fire_times, due_fire_times, initial_next_run, utc


@dataclass
class SchedulerTickResult:
    due_jobs: int = 0
    created: int = 0
    missed: int = 0
    duplicates: int = 0
    advanced: int = 0


def scheduled_idempotency_key(job_id: int, scheduled_for: datetime) -> str:
    return f"scheduled:{job_id}:{utc(scheduled_for).isoformat()}"


def ensure_job_next_run(job: Job, *, now: datetime) -> Job:
    if job.next_run_at is None and job.enabled:
        job.next_run_at = initial_next_run(
            job.schedule_type,
            job.schedule_value,
            now=now,
            timezone_name=job.timezone,
        )
        job.save(update_fields=["next_run_at", "updated_at"])
    return job


def create_execution_for_occurrence(
    job: Job,
    *,
    scheduled_for: datetime,
    now: datetime,
    status: str = ExecutionStatus.PENDING,
    is_manual: bool = False,
    idempotency_key: str | None = None,
    output: str = "",
) -> tuple[JobExecution, bool]:
    key = idempotency_key or scheduled_idempotency_key(job.id, scheduled_for)
    try:
        execution, created = JobExecution.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "job": job,
                "scheduled_for": utc(scheduled_for),
                "run_after": now,
                "status": status,
                "attempt_number": 1,
                "is_manual": is_manual,
                "output": output,
            },
        )
    except IntegrityError:
        existing = JobExecution.objects.filter(idempotency_key=key).first()
        if existing is None and not is_manual:
            existing = JobExecution.objects.filter(
                job=job,
                scheduled_for=utc(scheduled_for),
                is_manual=False,
            ).first()
        if existing is None:
            raise
        execution = existing
        created = False

    emit_event(
        EventType.OCCURRENCE_CREATED if created else EventType.OCCURRENCE_EXISTS,
        job=job,
        execution=execution,
        message=(
            f"Created occurrence for {scheduled_for.isoformat()}"
            if created
            else f"Occurrence already existed for {scheduled_for.isoformat()}"
        ),
        data={"status": execution.status, "scheduled_for": scheduled_for.isoformat()},
    )
    return execution, created


@transaction.atomic
def create_manual_execution(job: Job, *, now: datetime, requested_by: str = "operator") -> JobExecution:
    import uuid

    if not job.enabled:
        raise ValueError(f"job {job.id} is disabled; enable it before triggering a manual run")

    execution, _ = create_execution_for_occurrence(
        job,
        scheduled_for=now,
        now=now,
        status=ExecutionStatus.PENDING,
        is_manual=True,
        idempotency_key=f"manual:{job.id}:{uuid.uuid4()}",
        output=f"Manual run requested by {requested_by}",
    )
    invalidate_scheduler_cache("job manually triggered", job=job, execution=execution)
    return execution


def heal_enabled_jobs_missing_next_run(*, now: datetime) -> int:
    """Self-heal enabled interval/cron jobs that lost next_run_at (not one-time jobs)."""
    healed_ids: list[int] = []
    queryset = Job.objects.filter(enabled=True, next_run_at__isnull=True).exclude(
        schedule_type=ScheduleType.ONE_TIME,
    )
    for job in queryset:
        with transaction.atomic():
            locked = Job.objects.select_for_update().filter(pk=job.pk).first()
            if locked is None:
                continue
            if not locked.enabled or locked.next_run_at is not None:
                continue
            if locked.schedule_type == ScheduleType.ONE_TIME:
                continue
            ensure_job_next_run(locked, now=now)
            healed_ids.append(locked.id)
    if healed_ids:
        invalidate_scheduler_cache("job next_run healed", job_ids=set(healed_ids))
    return len(healed_ids)


class SchedulerService:
    def __init__(self, *, clock: Clock | None = None, scheduler_id: str | None = None):
        self.clock = clock or SystemClock()
        self.scheduler_id = scheduler_id or f"scheduler-{socket.gethostname()}-{os.getpid()}"

    def tick(self) -> SchedulerTickResult:
        now = self.clock.now()
        result = SchedulerTickResult()
        cap = getattr(settings, "MISFIRE_CATCH_UP_CAP", 50)
        heal_enabled_jobs_missing_next_run(now=now)

        with transaction.atomic():
            due_jobs = list(
                Job.objects.select_for_update(skip_locked=True)
                .filter(enabled=True, next_run_at__isnull=False, next_run_at__lte=now)
                .order_by("next_run_at", "id")
            )
            result.due_jobs = len(due_jobs)
            affected_job_ids: set[int] = set()
            for job in due_jobs:
                if job.misfire_policy == MisfirePolicy.COALESCE:
                    # Coalesce collapses the whole missed backlog into one run and
                    # advances past it in a single tick (no capped batch-per-tick).
                    fire_times, next_run_at = coalesced_due_fire_times(job, now)
                    if fire_times and job.next_run_at is not None and utc(job.next_run_at) < fire_times[-1]:
                        emit_event(
                            EventType.MISFIRE,
                            job=job,
                            message="coalesced missed occurrences into latest due run",
                            data={
                                "from": utc(job.next_run_at).isoformat(),
                                "latest": fire_times[-1].isoformat(),
                            },
                        )
                else:
                    fire_times, next_run_at = due_fire_times(job, now, cap=cap)
                if not fire_times:
                    continue
                affected_job_ids.add(job.id)
                emit_event(
                    EventType.DUE_DETECTED,
                    job=job,
                    message=f"{len(fire_times)} due occurrence(s) detected",
                    data={"now": now.isoformat()},
                )
                created, missed, duplicates = self._create_occurrences(job, fire_times, now)
                result.created += created
                result.missed += missed
                result.duplicates += duplicates

                job.next_run_at = next_run_at
                if job.schedule_type == ScheduleType.ONE_TIME and next_run_at is None:
                    job.enabled = False
                    job.save(update_fields=["next_run_at", "enabled", "updated_at"])
                else:
                    job.save(update_fields=["next_run_at", "updated_at"])
                result.advanced += 1

        update_scheduler_heartbeat(
            scheduler_id=self.scheduler_id,
            now=now,
            recent_occurrences_created=result.created,
            # Misfired/overlap-skipped occurrences are the scheduler's "failed to
            # run on time" signal for this tick.
            recent_failure_count=result.missed,
            health_state="healthy",
        )
        if result.created or result.missed or result.duplicates:
            invalidate_scheduler_cache("scheduler tick", job_ids=affected_job_ids)
        return result

    def _create_occurrences(
        self,
        job: Job,
        fire_times: list[datetime],
        now: datetime,
    ) -> tuple[int, int, int]:
        grace_cutoff = now - timedelta(seconds=job.misfire_grace_seconds)
        statuses: list[tuple[datetime, ExecutionStatus, str]] = []

        late_times = [fire_time for fire_time in fire_times if fire_time < grace_cutoff]
        if late_times and job.misfire_policy == MisfirePolicy.COALESCE:
            for skipped in fire_times[:-1]:
                statuses.append((skipped, ExecutionStatus.MISSED, "coalesced into later run"))
            statuses.append((fire_times[-1], ExecutionStatus.PENDING, "coalesced misfire"))
        elif late_times and job.misfire_policy == MisfirePolicy.SKIP:
            for fire_time in fire_times:
                status = ExecutionStatus.MISSED if fire_time < grace_cutoff else ExecutionStatus.PENDING
                statuses.append((fire_time, status, "misfire skipped" if status == ExecutionStatus.MISSED else "due"))
        else:
            for fire_time in fire_times:
                statuses.append((fire_time, ExecutionStatus.PENDING, "due"))

        created = missed = duplicates = 0
        for fire_time, status, reason in statuses:
            active = has_active_execution(job.id, now=now)
            final_status = status
            if (
                status == ExecutionStatus.PENDING
                and job.overlap_policy == OverlapPolicy.SKIP
                and active
            ):
                final_status = ExecutionStatus.MISSED
                reason = "overlap skipped"

            execution, was_created = create_execution_for_occurrence(
                job,
                scheduled_for=fire_time,
                now=now,
                status=final_status,
                output=reason,
            )
            if was_created and final_status == ExecutionStatus.MISSED:
                missed += 1
                emit_event(
                    EventType.MISFIRE,
                    job=job,
                    execution=execution,
                    message=reason,
                    data={"scheduled_for": fire_time.isoformat()},
                )
            elif was_created:
                created += 1
            else:
                duplicates += 1
        return created, missed, duplicates

