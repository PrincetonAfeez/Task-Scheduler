""" Retention operations for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone

from scheduler_app.models import EventType, Job, JobEvent, JobExecution, TERMINAL_EXECUTION_STATUSES

from .cache import invalidate_scheduler_cache
from .events import emit_event


def _terminal_history(job: Job) -> QuerySet[JobExecution]:
    # Executions with a dead-letter record are operator-attention evidence and are
    # never pruned, so the dead-letter audit trail survives retention.
    return job.executions.filter(status__in=TERMINAL_EXECUTION_STATUSES, dead_letter__isnull=True)


def prune_job_history(job: Job) -> int:
    deleted = 0
    now = timezone.now()
    if job.retention_days:
        cutoff = now - timedelta(days=job.retention_days)
        count, _ = _terminal_history(job).filter(created_at__lt=cutoff).delete()
        deleted += count

    if job.retention_count:
        keep_ids = list(
            _terminal_history(job).order_by("-created_at").values_list("id", flat=True)[: job.retention_count]
        )
        if keep_ids:
            count, _ = _terminal_history(job).exclude(id__in=keep_ids).delete()
        else:
            count, _ = _terminal_history(job).delete()
        deleted += count

    if deleted:
        emit_event(
            EventType.CACHE_INVALIDATION,
            job=job,
            message=f"Pruned {deleted} old execution rows",
            data={"deleted": deleted},
        )
        invalidate_scheduler_cache("retention pruning", job=job)
    return deleted


def prune_events(*, older_than_days: int | None = None) -> int:
    """Age-cap the JobEvent audit log so it does not grow without bound."""
    days = older_than_days if older_than_days is not None else getattr(settings, "EVENT_RETENTION_DAYS", 30)
    if not days:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = JobEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted


def prune_all_jobs() -> int:
    total = 0
    for job in Job.objects.all():
        total += prune_job_history(job)
    prune_events()
    return total

