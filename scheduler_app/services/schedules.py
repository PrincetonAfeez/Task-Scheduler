""" Schedule operations for the scheduler app. """

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from scheduler_app.models import Job, ScheduleType


@dataclass(frozen=True)
class SchedulePreview:
    fire_times: list[datetime]


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: str | datetime, timezone_name: str = "UTC") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return utc(parsed)


def parse_duration_seconds(value: Any) -> int:
    if isinstance(value, dict):
        if "every" in value:
            return parse_duration_seconds(value["every"])
        seconds = 0
        seconds += int(value.get("seconds", 0))
        seconds += int(value.get("minutes", 0)) * 60
        seconds += int(value.get("hours", 0)) * 3600
        seconds += int(value.get("days", 0)) * 86400
        if seconds <= 0:
            raise ValueError("interval schedule must be greater than zero seconds")
        return seconds
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("interval schedule must be greater than zero seconds")
        return value
    if isinstance(value, str):
        stripped = value.strip().lower()
        units = {
            "s": 1,
            "sec": 1,
            "second": 1,
            "seconds": 1,
            "m": 60,
            "min": 60,
            "minute": 60,
            "minutes": 60,
            "h": 3600,
            "hour": 3600,
            "hours": 3600,
            "d": 86400,
            "day": 86400,
            "days": 86400,
        }
        for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
            if stripped.endswith(suffix):
                number = stripped[: -len(suffix)].strip()
                seconds = int(number) * multiplier
                if seconds <= 0:
                    raise ValueError("interval schedule must be greater than zero seconds")
                return seconds
        seconds = int(stripped)
        if seconds <= 0:
            raise ValueError("interval schedule must be greater than zero seconds")
        return seconds
    raise ValueError("unsupported interval value")


def interval_seconds(schedule_value: dict[str, Any]) -> int:
    if any(unit in schedule_value for unit in ("seconds", "minutes", "hours", "days")):
        return parse_duration_seconds(schedule_value)
    if "every" in schedule_value:
        return parse_duration_seconds(schedule_value["every"])
    raise ValueError("interval schedule_value requires seconds/minutes/hours/days or every")


def cron_expression(schedule_value: dict[str, Any]) -> str:
    expression = schedule_value.get("expression") or schedule_value.get("cron")
    if not expression:
        raise ValueError("cron schedule_value requires expression")
    if not croniter.is_valid(expression):
        raise ValueError(f"invalid cron expression: {expression}")
    return str(expression)


def compute_next_run(
    schedule_type: str,
    schedule_value: dict[str, Any],
    *,
    previous_fire_time: datetime | None,
    now: datetime,
    timezone_name: str,
) -> datetime | None:
    now = utc(now)
    if schedule_type == ScheduleType.ONE_TIME:
        run_at_raw = schedule_value.get("run_at") or schedule_value.get("at")
        if not run_at_raw:
            raise ValueError("one_time schedule_value requires run_at")
        run_at = parse_datetime(run_at_raw, timezone_name)
        if previous_fire_time is not None:
            return None
        return run_at

    if schedule_type == ScheduleType.INTERVAL:
        seconds = interval_seconds(schedule_value)
        if previous_fire_time is not None:
            return utc(previous_fire_time) + timedelta(seconds=seconds)
        start_at_raw = schedule_value.get("start_at")
        if start_at_raw:
            return parse_datetime(start_at_raw, timezone_name)
        return now + timedelta(seconds=seconds)

    if schedule_type == ScheduleType.CRON:
        expression = cron_expression(schedule_value)
        local_tz = ZoneInfo(timezone_name)
        base = utc(previous_fire_time or now).astimezone(local_tz)
        next_local = croniter(expression, base).get_next(datetime)
        if next_local.tzinfo is None:
            next_local = next_local.replace(tzinfo=local_tz)
        return utc(next_local)

    raise ValueError(f"unsupported schedule_type: {schedule_type}")


def initial_next_run(
    schedule_type: str,
    schedule_value: dict[str, Any],
    *,
    now: datetime,
    timezone_name: str,
) -> datetime | None:
    return compute_next_run(
        schedule_type,
        schedule_value,
        previous_fire_time=None,
        now=now,
        timezone_name=timezone_name,
    )


def next_after_fire(job: Job, fire_time: datetime, now: datetime) -> datetime | None:
    return compute_next_run(
        job.schedule_type,
        job.schedule_value,
        previous_fire_time=fire_time,
        now=now,
        timezone_name=job.timezone,
    )


def due_fire_times(job: Job, now: datetime, *, cap: int) -> tuple[list[datetime], datetime | None]:
    if job.next_run_at is None or job.next_run_at > now or cap <= 0:
        return [], job.next_run_at

    fire_times: list[datetime] = []
    cursor: datetime | None = utc(job.next_run_at)
    while cursor is not None and cursor <= now and len(fire_times) < cap:
        fire_times.append(cursor)
        cursor = next_after_fire(job, cursor, now)

    return fire_times, cursor


# Safety bound for walking a cron/one-time backlog when coalescing. Cron has at
# most minute granularity, so this covers multi-year gaps without unbounded work.
_COALESCE_WALK_GUARD = 1_000_000


def coalesced_due_fire_times(job: Job, now: datetime) -> tuple[list[datetime], datetime | None]:
    """Collapse a missed backlog into the single latest due occurrence.

    Returns ``([latest_due], next_future_fire_time)`` (or ``([], next)`` when nothing
    is due). Unlike :func:`due_fire_times`, this never materialises the intermediate
    occurrences, so ``coalesce`` runs exactly once and advances past the whole backlog
    in one tick instead of one capped batch per tick.
    """
    if job.next_run_at is None or utc(job.next_run_at) > now:
        return [], job.next_run_at

    cursor = utc(job.next_run_at)
    latest: datetime | None = None
    if job.schedule_type == ScheduleType.INTERVAL:
        seconds = interval_seconds(job.schedule_value)
        steps = int((now - cursor).total_seconds() // seconds)
        latest = cursor + timedelta(seconds=steps * seconds)
        return [latest], latest + timedelta(seconds=seconds)

    nxt: datetime | None = cursor
    guard = 0
    while nxt is not None and nxt <= now and guard < _COALESCE_WALK_GUARD:
        latest = nxt
        nxt = next_after_fire(job, nxt, now)
        guard += 1
    return ([latest] if latest is not None else []), nxt


def upcoming_runs_for_job(job: Job, *, count: int, now: datetime | None = None) -> list[datetime]:
    if count <= 0:
        return []

    results: list[datetime] = []
    cursor = job.next_run_at
    if cursor is None:
        if now is None:
            raise ValueError("now is required when job.next_run_at is not set")
        cursor = initial_next_run(
            job.schedule_type,
            job.schedule_value,
            now=now,
            timezone_name=job.timezone,
        )

    # "Upcoming" means strictly future when a reference time is supplied, so an
    # overdue job's preview skips the times the scheduler has already passed.
    if now is not None and cursor is not None and utc(cursor) <= utc(now):
        if job.schedule_type == ScheduleType.INTERVAL:
            seconds = interval_seconds(job.schedule_value)
            elapsed = (utc(now) - utc(cursor)).total_seconds()
            cursor = utc(cursor) + timedelta(seconds=(int(elapsed // seconds) + 1) * seconds)
        else:
            guard = 0
            while cursor is not None and utc(cursor) <= utc(now) and guard < _COALESCE_WALK_GUARD:
                cursor = next_after_fire(job, cursor, now)
                guard += 1

    while cursor is not None and len(results) < count:
        results.append(utc(cursor))
        cursor = next_after_fire(job, cursor, now or cursor)
    return results


def upcoming_runs_across_jobs(jobs: list[Job], *, count: int, now: datetime) -> list[tuple[Job, datetime]]:
    upcoming: list[tuple[Job, datetime]] = []
    for job in jobs:
        for fire_time in upcoming_runs_for_job(job, count=count, now=now):
            upcoming.append((job, fire_time))
    upcoming.sort(key=lambda item: item[1])
    return upcoming[:count]

