""" Cache operations for the scheduler app. """

from __future__ import annotations

from datetime import datetime

from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Q

from scheduler_app.models import (
    ExecutionStatus,
    Job,
    JobExecution,
    SchedulerHeartbeat,
    WorkerHeartbeat,
)
from scheduler_app.tasks.registry import catalog_metadata

from .events import emit_cache_invalidation
from .schedules import upcoming_runs_across_jobs, upcoming_runs_for_job

DASHBOARD_SUMMARY_KEY = "dashboard_summary:v1"
QUEUE_DEPTH_KEY = "queue_depth:v1"
TASK_CATALOG_KEY = "task_catalog:v1"
ALL_UPCOMING_KEY = "upcoming:all:v1"

# Previews are cached at a fixed depth and sliced per request, so the cache key
# does not depend on the caller's ``count`` (one deletable key per scope).
PREVIEW_CACHE_SIZE = 25


def job_stats_key(job_id: int) -> str:
    return f"job_stats:{job_id}:v1"


def upcoming_job_key(job_id: int) -> str:
    return f"upcoming:job:{job_id}:v1"


def queue_depth() -> dict[str, int]:
    cached = cache.get(QUEUE_DEPTH_KEY)
    if cached is not None:
        return cached
    depth = {choice.value: 0 for choice in ExecutionStatus}
    for row in JobExecution.objects.values("status").annotate(count=Count("id")):
        depth[row["status"]] = row["count"]
    cache.set(QUEUE_DEPTH_KEY, depth, timeout=10)
    return depth


def dashboard_summary() -> dict[str, object]:
    cached = cache.get(DASHBOARD_SUMMARY_KEY)
    if cached is not None:
        return cached
    summary = JobExecution.objects.aggregate(
        total_runs=Count("id"),
        success_count=Count("id", filter=Q(status=ExecutionStatus.SUCCEEDED)),
        failure_count=Count("id", filter=Q(status__in=[ExecutionStatus.FAILED, ExecutionStatus.DEAD_LETTERED])),
        timeout_count=Count("id", filter=Q(status=ExecutionStatus.TIMED_OUT)),
        misfire_count=Count("id", filter=Q(status=ExecutionStatus.MISSED)),
        average_duration=Avg("duration_ms"),
    )
    last_execution = JobExecution.objects.order_by("-created_at").first()
    summary["last_status"] = last_execution.status if last_execution else "none"
    summary["job_count"] = Job.objects.count()
    summary["enabled_job_count"] = Job.objects.filter(enabled=True).count()
    summary["scheduler_health"] = list(
        SchedulerHeartbeat.objects.values("scheduler_id", "health_state", "last_tick_at")[:10]
    )
    summary["worker_health"] = list(
        WorkerHeartbeat.objects.values("worker_id", "health_state", "last_heartbeat_at")[:10]
    )
    cache.set(DASHBOARD_SUMMARY_KEY, summary, timeout=15)
    return summary


def job_stats(job: Job) -> dict[str, object]:
    key = job_stats_key(job.id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    stats = job.executions.aggregate(
        total_runs=Count("id"),
        success_count=Count("id", filter=Q(status=ExecutionStatus.SUCCEEDED)),
        failure_count=Count("id", filter=Q(status__in=[ExecutionStatus.FAILED, ExecutionStatus.DEAD_LETTERED])),
        timeout_count=Count("id", filter=Q(status=ExecutionStatus.TIMED_OUT)),
        misfire_count=Count("id", filter=Q(status=ExecutionStatus.MISSED)),
        average_duration=Avg("duration_ms"),
    )
    last_execution = job.executions.order_by("-created_at").first()
    stats["last_status"] = last_execution.status if last_execution else "none"
    stats["last_error"] = last_execution.error if last_execution else ""
    cache.set(key, stats, timeout=15)
    return stats


def task_catalog_cached() -> list[dict[str, object]]:
    cached = cache.get(TASK_CATALOG_KEY)
    if cached is not None:
        return cached
    metadata = catalog_metadata()
    cache.set(TASK_CATALOG_KEY, metadata, timeout=300)
    return metadata


def upcoming_for_job_cached(job: Job, *, count: int, now: datetime) -> list[datetime]:
    key = upcoming_job_key(job.id)
    cached = cache.get(key)
    if cached is not None:
        return cached[:count]
    depth = max(count, PREVIEW_CACHE_SIZE)
    preview = upcoming_runs_for_job(job, count=depth, now=now)
    cache.set(key, preview, timeout=20)
    return preview[:count]


def upcoming_all_cached(*, count: int, now: datetime) -> list[tuple[Job, datetime]]:
    cached = cache.get(ALL_UPCOMING_KEY)
    if cached is not None:
        return cached[:count]
    jobs = list(Job.objects.filter(enabled=True).order_by("next_run_at"))
    depth = max(count, PREVIEW_CACHE_SIZE)
    preview = upcoming_runs_across_jobs(jobs, count=depth, now=now)
    cache.set(ALL_UPCOMING_KEY, preview, timeout=20)
    return preview[:count]


def scheduler_cache_keys(*, job_ids: set[int] | None = None) -> list[str]:
    """Keys that derive from job/execution state (the task catalog is excluded:
    it only changes with code, not runtime events)."""
    keys = [DASHBOARD_SUMMARY_KEY, QUEUE_DEPTH_KEY, ALL_UPCOMING_KEY]
    for job_id in sorted(job_ids or ()):
        keys.extend([job_stats_key(job_id), upcoming_job_key(job_id)])
    return keys


def invalidate_scheduler_cache(
    reason: str,
    *,
    job: Job | None = None,
    execution: JobExecution | None = None,
    job_ids: set[int] | None = None,
) -> None:
    target_job_ids: set[int] = set(job_ids or ())
    if job is not None:
        target_job_ids.add(job.id)
    if execution is not None:
        target_job_ids.add(execution.job_id)
    keys = scheduler_cache_keys(job_ids=target_job_ids)
    # Delete after the surrounding transaction commits so a concurrent reader
    # cannot repopulate the cache with pre-commit (stale) data. Outside an atomic
    # block this runs immediately.
    transaction.on_commit(lambda: cache.delete_many(keys))
    emit_cache_invalidation(reason, job=job, execution=execution)

