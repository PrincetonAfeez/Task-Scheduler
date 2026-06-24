"""Exhaustive tests for scheduler_app.services.schedules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scheduler_app.models import ScheduleType
from scheduler_app.services.schedules import (
    coalesced_due_fire_times,
    compute_next_run,
    cron_expression,
    due_fire_times,
    initial_next_run,
    interval_seconds,
    next_after_fire,
    parse_datetime,
    parse_duration_seconds,
    upcoming_runs_across_jobs,
    upcoming_runs_for_job,
    utc,
)


def test_utc_naive_and_aware():
    naive = datetime(2026, 1, 1, 12, 0)
    assert utc(naive).tzinfo == UTC
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert utc(aware) == aware


def test_parse_datetime_from_string_and_datetime(now):
    parsed = parse_datetime(now.isoformat())
    assert parsed.tzinfo == UTC
    naive_local = datetime(2026, 6, 1, 9, 0)
    parsed_tz = parse_datetime(naive_local, "America/Los_Angeles")
    assert parsed_tz.tzinfo == UTC


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (30, 30),
        ("45s", 45),
        ("2m", 120),
        ("1h", 3600),
        ("1d", 86400),
        ("90 seconds", 90),
        ({"seconds": 10, "minutes": 1}, 70),
        ({"every": "5m"}, 300),
    ],
)
def test_parse_duration_seconds_variants(value, expected):
    assert parse_duration_seconds(value) == expected


def test_parse_duration_seconds_rejects_invalid():
    with pytest.raises(ValueError, match="greater than zero"):
        parse_duration_seconds(0)
    with pytest.raises(ValueError, match="unsupported"):
        parse_duration_seconds([])


def test_interval_seconds_requires_shape():
    with pytest.raises(ValueError, match="requires"):
        interval_seconds({})


def test_cron_expression_validates():
    assert cron_expression({"expression": "*/5 * * * *"}) == "*/5 * * * *"
    assert cron_expression({"cron": "0 * * * *"}) == "0 * * * *"
    with pytest.raises(ValueError, match="requires expression"):
        cron_expression({})
    with pytest.raises(ValueError, match="invalid cron"):
        cron_expression({"expression": "not a cron"})


def test_compute_next_run_one_time_and_interval(now):
    run_at = now + timedelta(hours=1)
    first = compute_next_run(
        ScheduleType.ONE_TIME,
        {"run_at": run_at.isoformat()},
        previous_fire_time=None,
        now=now,
        timezone_name="UTC",
    )
    assert first == utc(run_at)
    assert (
        compute_next_run(
            ScheduleType.ONE_TIME,
            {"run_at": run_at.isoformat()},
            previous_fire_time=first,
            now=now,
            timezone_name="UTC",
        )
        is None
    )

    nxt = compute_next_run(
        ScheduleType.INTERVAL,
        {"every": "60s", "start_at": now.isoformat()},
        previous_fire_time=now,
        now=now,
        timezone_name="UTC",
    )
    assert nxt == now + timedelta(seconds=60)


def test_compute_next_run_cron(now):
    nxt = compute_next_run(
        ScheduleType.CRON,
        {"expression": "0 * * * *"},
        previous_fire_time=None,
        now=now,
        timezone_name="UTC",
    )
    assert nxt is not None
    assert nxt > now


def test_compute_next_run_unsupported_type(now):
    with pytest.raises(ValueError, match="unsupported schedule_type"):
        compute_next_run("bogus", {}, previous_fire_time=None, now=now, timezone_name="UTC")


def test_initial_next_run(now):
    result = initial_next_run(
        ScheduleType.INTERVAL,
        {"every": "30s"},
        now=now,
        timezone_name="UTC",
    )
    assert result is not None


@pytest.mark.django_db
def test_due_fire_times_and_coalesce(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
        next_run_at=now - timedelta(minutes=5),
    )
    fires, nxt = due_fire_times(job, now, cap=3)
    assert len(fires) == 3
    assert nxt is not None

    coalesce_job = job_factory(
        name="coalesce-job",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
        next_run_at=now - timedelta(minutes=5),
    )
    coalesced, coalesce_next = coalesced_due_fire_times(coalesce_job, now)
    assert len(coalesced) == 1
    assert coalesce_next == coalesced[0] + timedelta(seconds=60)


@pytest.mark.django_db
def test_due_fire_times_empty_when_not_due(job_factory, now):
    job = job_factory(next_run_at=now + timedelta(hours=1))
    fires, nxt = due_fire_times(job, now, cap=10)
    assert fires == []
    assert nxt == job.next_run_at


@pytest.mark.django_db
def test_upcoming_runs_for_job_overdue_interval(job_factory, now):
    job = job_factory(
        next_run_at=now - timedelta(minutes=10),
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(hours=1)).isoformat()},
    )
    upcoming = upcoming_runs_for_job(job, count=3, now=now)
    assert len(upcoming) == 3
    assert all(utc(t) > utc(now) for t in upcoming)


@pytest.mark.django_db
def test_upcoming_runs_for_job_cron_overdue(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.CRON,
        schedule_value={"expression": "*/15 * * * *"},
        next_run_at=now - timedelta(hours=2),
    )
    upcoming = upcoming_runs_for_job(job, count=2, now=now)
    assert len(upcoming) == 2


@pytest.mark.django_db
def test_upcoming_runs_for_job_without_next_run_requires_now(job_factory, now):
    job = job_factory(next_run_at=None)
    with pytest.raises(ValueError, match="now is required"):
        upcoming_runs_for_job(job, count=1)


@pytest.mark.django_db
def test_upcoming_runs_for_job_zero_count(job_factory, now):
    job = job_factory()
    assert upcoming_runs_for_job(job, count=0, now=now) == []


@pytest.mark.django_db
def test_upcoming_runs_across_jobs(job_factory, now):
    jobs = [
        job_factory(name="a", next_run_at=now + timedelta(minutes=5)),
        job_factory(name="b", next_run_at=now + timedelta(minutes=1)),
    ]
    combined = upcoming_runs_across_jobs(jobs, count=2, now=now)
    assert len(combined) == 2
    assert combined[0][0].name == "b"


@pytest.mark.django_db
def test_next_after_fire(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "120s", "start_at": now.isoformat()},
        next_run_at=now,
    )
    nxt = next_after_fire(job, now, now)
    assert nxt == now + timedelta(seconds=120)


def test_parse_duration_dict_zero_seconds():
    with pytest.raises(ValueError, match="greater than zero"):
        parse_duration_seconds({"seconds": 0})


def test_parse_duration_bare_string_int():
    assert parse_duration_seconds("120") == 120


@pytest.mark.django_db
def test_coalesced_due_when_next_run_in_future(job_factory, now):
    job = job_factory(next_run_at=now + timedelta(hours=1))
    fires, nxt = coalesced_due_fire_times(job, now)
    assert fires == []
    assert nxt == job.next_run_at


@pytest.mark.django_db
def test_upcoming_runs_requires_now_when_next_run_unset(job_factory):
    job = job_factory(next_run_at=None)
    with pytest.raises(ValueError, match="now is required"):
        upcoming_runs_for_job(job, count=1)
