""" Job schedule operations for the scheduler app. """

from __future__ import annotations

from datetime import datetime
from typing import Any

from scheduler_app.models import Job, ScheduleType

from .schedules import initial_next_run, parse_datetime, utc


SCHEDULE_FIELDS = frozenset({"schedule_type", "schedule_value", "timezone"})

ONE_TIME_ALREADY_RAN = (
    "This one-time job has already run. Edit the schedule (run_at) before re-enabling, "
    "or create a new job."
)


def schedule_fields_changed(changed_fields: set[str] | list[str]) -> bool:
    return bool(SCHEDULE_FIELDS & set(changed_fields))


def one_time_run_at_instant(
    schedule_value: dict[str, Any],
    *,
    timezone_name: str = "UTC",
) -> datetime | None:
    raw = schedule_value.get("run_at") or schedule_value.get("at")
    if raw is None:
        return None
    return utc(parse_datetime(raw, timezone_name))


def is_completed_one_time(job: Job) -> bool:
    return (
        job.schedule_type == ScheduleType.ONE_TIME
        and not job.enabled
        and job.next_run_at is None
    )


def one_time_run_at_changed(
    current: dict[str, Any],
    previous: dict[str, Any],
    *,
    timezone_name: str,
    previous_timezone: str,
) -> bool:
    current_at = one_time_run_at_instant(current, timezone_name=timezone_name)
    previous_at = one_time_run_at_instant(previous, timezone_name=previous_timezone)
    if current_at is None and previous_at is None:
        return False
    if current_at is None or previous_at is None:
        return True
    return current_at != previous_at


def _reject_completed_one_time_resurrection(
    job: Job,
    *,
    previous: Job | None,
    schedule_changed: bool,
    re_enabled: bool,
) -> None:
    if previous is None or not is_completed_one_time(previous) or not job.enabled:
        return
    if one_time_run_at_changed(
        job.schedule_value,
        previous.schedule_value,
        timezone_name=job.timezone,
        previous_timezone=previous.timezone,
    ):
        return
    if re_enabled or schedule_changed:
        raise ValueError(ONE_TIME_ALREADY_RAN)


def validate_schedule_for_job(job: Job, *, now: datetime) -> None:
    """Ensure schedule_type and schedule_value are compatible and produce a next run."""
    initial_next_run(
        job.schedule_type,
        job.schedule_value,
        now=now,
        timezone_name=job.timezone,
    )


def apply_next_run_after_edit(
    job: Job,
    *,
    now: datetime,
    schedule_changed: bool,
    re_enabled: bool = False,
    previous: Job | None = None,
) -> None:
    """Re-anchor next_run_at when schedule inputs change, on create, or after a long disable."""
    if not job.enabled:
        return
    _reject_completed_one_time_resurrection(
        job,
        previous=previous,
        schedule_changed=schedule_changed,
        re_enabled=re_enabled,
    )
    if schedule_changed or job.next_run_at is None:
        job.next_run_at = initial_next_run(
            job.schedule_type,
            job.schedule_value,
            now=now,
            timezone_name=job.timezone,
        )
    elif re_enabled and utc(job.next_run_at) <= utc(now):
        job.next_run_at = initial_next_run(
            job.schedule_type,
            job.schedule_value,
            now=now,
            timezone_name=job.timezone,
        )
