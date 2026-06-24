""" Health operations for the scheduler app. """

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import F

from scheduler_app.models import SchedulerHeartbeat, WorkerHeartbeat


def update_scheduler_heartbeat(
    *,
    scheduler_id: str,
    now: datetime,
    recent_occurrences_created: int,
    recent_failure_count: int,
    health_state: str,
) -> SchedulerHeartbeat:
    heartbeat, _ = SchedulerHeartbeat.objects.update_or_create(
        scheduler_id=scheduler_id,
        defaults={
            "hostname": socket.gethostname(),
            "process_id": os.getpid(),
            "last_tick_at": now,
            "recent_occurrences_created": recent_occurrences_created,
            "recent_failure_count": recent_failure_count,
            "health_state": health_state,
        },
    )
    return heartbeat


def update_worker_heartbeat(
    *,
    worker_id: str,
    now: datetime,
    active_execution_count: int,
    health_state: str,
    completed_delta: int = 0,
    failed_delta: int = 0,
    current_execution_id: int | None = None,
) -> WorkerHeartbeat:
    # completed_count/failed_count are lifetime totals, so they are incremented
    # atomically rather than overwritten on each heartbeat.
    heartbeat, created = WorkerHeartbeat.objects.get_or_create(
        worker_id=worker_id,
        defaults={
            "hostname": socket.gethostname(),
            "process_id": os.getpid(),
            "last_heartbeat_at": now,
            "active_execution_count": active_execution_count,
            "completed_count": completed_delta,
            "failed_count": failed_delta,
            "health_state": health_state,
            "current_execution_id": current_execution_id,
        },
    )
    if not created:
        WorkerHeartbeat.objects.filter(pk=heartbeat.pk).update(
            hostname=socket.gethostname(),
            process_id=os.getpid(),
            last_heartbeat_at=now,
            active_execution_count=active_execution_count,
            completed_count=F("completed_count") + completed_delta,
            failed_count=F("failed_count") + failed_delta,
            health_state=health_state,
            current_execution_id=current_execution_id,
            updated_at=now,
        )
        heartbeat.refresh_from_db()
    return heartbeat


def health_snapshot() -> dict[str, object]:
    return {
        "schedulers": list(SchedulerHeartbeat.objects.order_by("scheduler_id")),
        "workers": list(WorkerHeartbeat.objects.order_by("worker_id")),
    }


def prune_stale_worker_heartbeats(*, now: datetime, max_age_seconds: int | None = None) -> int:
    age = max_age_seconds if max_age_seconds is not None else getattr(settings, "HEARTBEAT_PRUNE_SECONDS", 86_400)
    cutoff = now - timedelta(seconds=age)
    deleted, _ = WorkerHeartbeat.objects.filter(last_heartbeat_at__lt=cutoff).delete()
    return deleted


def prune_stale_scheduler_heartbeats(*, now: datetime, max_age_seconds: int | None = None) -> int:
    age = max_age_seconds if max_age_seconds is not None else getattr(settings, "HEARTBEAT_PRUNE_SECONDS", 86_400)
    cutoff = now - timedelta(seconds=age)
    deleted, _ = SchedulerHeartbeat.objects.filter(last_tick_at__lt=cutoff).delete()
    return deleted

