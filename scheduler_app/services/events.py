""" Event operations for the scheduler app. """ 

from __future__ import annotations

import logging
from typing import Any

from scheduler_app.models import EventType, Job, JobEvent, JobExecution

logger = logging.getLogger("scheduler_app")


def emit_event(
    event_type: str,
    *,
    job: Job | None = None,
    execution: JobExecution | None = None,
    message: str = "",
    data: dict[str, Any] | None = None,
) -> JobEvent:
    data = data or {}
    logger.info(
        message or event_type,
        extra={
            "event_type": event_type,
            "job_id": job.id if job else None,
            "execution_id": execution.id if execution else None,
            **data,
        },
    )
    return JobEvent.objects.create(
        event_type=event_type,
        job=job,
        execution=execution,
        message=message,
        data=data,
    )


def emit_cache_invalidation(reason: str, *, job: Job | None = None, execution: JobExecution | None = None) -> None:
    emit_event(
        EventType.CACHE_INVALIDATION,
        job=job,
        execution=execution,
        message=f"Cache invalidated: {reason}",
        data={"reason": reason},
    )

