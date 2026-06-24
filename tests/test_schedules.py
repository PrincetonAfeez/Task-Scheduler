""" Test schedules for the scheduler app. """

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scheduler_app.models import Job, ScheduleType
from scheduler_app.services.schedules import compute_next_run, due_fire_times, upcoming_runs_for_job


def test_one_time_next_run_uses_utc(now):
    run_at = "2026-01-01T09:00:00-05:00"
    result = compute_next_run(
        ScheduleType.ONE_TIME,
        {"run_at": run_at},
        previous_fire_time=None,
        now=now,
        timezone_name="America/New_York",
    )
    assert result == datetime(2026, 1, 1, 14, 0, tzinfo=UTC)


def test_interval_next_run_advances_from_previous_fire(now):
    result = compute_next_run(
        ScheduleType.INTERVAL,
        {"every": "5m"},
        previous_fire_time=now,
        now=now,
        timezone_name="UTC",
    )
    assert result == now + timedelta(minutes=5)


def test_cron_next_run_interprets_job_timezone():
    now = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
    result = compute_next_run(
        ScheduleType.CRON,
        {"expression": "0 9 * * 1-5"},
        previous_fire_time=None,
        now=now,
        timezone_name="America/New_York",
    )
    assert result == datetime(2026, 1, 5, 14, 0, tzinfo=UTC)


def test_due_fire_times_is_capped(job_factory, now):
    job = job_factory(
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=10)).isoformat()},
        next_run_at=now - timedelta(minutes=10),
    )
    fire_times, next_run = due_fire_times(job, now, cap=3)
    assert len(fire_times) == 3
    assert next_run == now - timedelta(minutes=7)


def test_cron_dst_spring_forward_shifts_utc_offset():
    # 2026 US DST begins 2026-03-08 02:00 local. A 09:00 New York daily cron is
    # 14:00 UTC under EST (-5) and 13:00 UTC under EDT (-4): the engine must track
    # the offset change across the spring-forward boundary.
    previous = datetime(2026, 3, 7, 14, 0, tzinfo=UTC)  # 2026-03-07 09:00 EST
    result = compute_next_run(
        ScheduleType.CRON,
        {"expression": "0 9 * * *"},
        previous_fire_time=previous,
        now=previous,
        timezone_name="America/New_York",
    )
    assert result == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)  # 09:00 EDT


def test_cron_spring_forward_nonexistent_time_collapses_to_gap_edge():
    # 02:30 does not exist on 2026-03-08 (clocks jump 02:00 -> 03:00). The documented
    # policy: the nonexistent local time collapses to the first valid instant (03:00 EDT).
    base = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # 01:00 EST, just before the gap
    first = compute_next_run(
        ScheduleType.CRON,
        {"expression": "30 2 * * *"},
        previous_fire_time=base,
        now=base,
        timezone_name="America/New_York",
    )
    second = compute_next_run(
        ScheduleType.CRON,
        {"expression": "30 2 * * *"},
        previous_fire_time=first,
        now=first,
        timezone_name="America/New_York",
    )
    # 03:00 EDT (gap edge) on the transition day.
    assert first == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    # The next day 02:30 EDT exists again, and the sequence advances monotonically.
    assert second == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    assert second > first


def test_fall_back_cron_runs_once_for_ambiguous_local_time(db):
    job = Job.objects.create(
        name="fall-back",
        registered_task_name="always_succeed",
        schedule_type=ScheduleType.CRON,
        schedule_value={"expression": "30 1 1 11 *"},
        timezone="America/New_York",
        next_run_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
    )
    upcoming = upcoming_runs_for_job(job, count=2, now=datetime(2026, 11, 1, 4, 0, tzinfo=UTC))
    assert upcoming[0] == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert upcoming[1].year == 2027


@pytest.mark.parametrize(
    "schedule_type, schedule_value",
    [
        (ScheduleType.CRON, {"expression": "not a cron"}),
        (ScheduleType.CRON, {}),  # missing expression
        (ScheduleType.ONE_TIME, {}),  # missing run_at
        (ScheduleType.INTERVAL, {}),  # missing interval spec
        (ScheduleType.INTERVAL, {"every": "0s"}),  # non-positive interval
        (ScheduleType.INTERVAL, {"every": "-5m"}),  # negative interval
        ("weekly", {}),  # unsupported schedule type
    ],
)
def test_malformed_schedule_raises_value_error(schedule_type, schedule_value, now):
    with pytest.raises(ValueError):
        compute_next_run(
            schedule_type,
            schedule_value,
            previous_fire_time=None,
            now=now,
            timezone_name="UTC",
        )

