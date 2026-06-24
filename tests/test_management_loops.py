"""Management command loop coverage and PostgreSQL-only demo tests."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from itertools import count
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings

from scheduler_app.management.commands.demo import Command as DemoCommand
from scheduler_app.management.commands.execution import Command as ExecutionCommand
from scheduler_app.management.commands.scheduler import Command as SchedulerCommand
from scheduler_app.management.commands.worker import Command as WorkerCommand
from scheduler_app.models import ExecutionStatus, JobExecution, MisfirePolicy
from scheduler_app.services.due import SchedulerService, create_execution_for_occurrence


class _StopSchedulerLoop(Exception):
    """Sentinel used to exit scheduler run loop tests after N sleeps."""


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_demo_single_fire_command_on_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("demo single-fire requires PostgreSQL")

    out = StringIO()
    call_command("demo", "single-fire", stdout=out)
    body = out.getvalue()
    assert "scheduled occurrence rows=1" in body
    assert "at-most-once" in body


@pytest.mark.django_db
def test_scheduler_run_loop_sleeps_between_ticks():
    monotonic = count(0).__next__

    def sleep_and_stop(_seconds: float) -> None:
        if sleep_and_stop.calls >= 2:  # type: ignore[attr-defined]
            raise _StopSchedulerLoop
        sleep_and_stop.calls += 1  # type: ignore[attr-defined]

    sleep_and_stop.calls = 0  # type: ignore[attr-defined]

    with (
        patch("scheduler_app.management.commands.scheduler.time.monotonic", side_effect=monotonic),
        patch(
            "scheduler_app.management.commands.scheduler.interruptible_sleep",
            side_effect=sleep_and_stop,
        ),
        pytest.raises(_StopSchedulerLoop),
    ):
        call_command("scheduler", "run", "--tick-seconds", "2")

    assert sleep_and_stop.calls == 2  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_scheduler_run_once_still_exits_without_sleep():
    with patch("scheduler_app.management.commands.scheduler.interruptible_sleep") as mock_sleep:
        call_command("scheduler", "run", "--once")
    mock_sleep.assert_not_called()


@pytest.mark.django_db
@override_settings(EXECUTOR_BACKEND="inprocess")
def test_worker_run_continuous_loop():
    with patch(
        "scheduler_app.management.commands.worker.run_worker_loop",
        return_value=4,
    ) as mock_loop:
        out = StringIO()
        call_command("worker", "run", "--workers", "2", stdout=out)
    mock_loop.assert_called_once()
    assert "completed=4" in out.getvalue()


def test_scheduler_command_unknown_action_direct():
    command = SchedulerCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="unknown scheduler action"):
        command.handle(action="bogus", once=False, tick_seconds=1.0, scheduler_id=None)


def test_worker_command_unknown_action_direct():
    command = WorkerCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="unknown worker action"):
        command.handle(action="bogus", once=False, workers=1, worker_id=None)


def test_demo_command_unknown_action_direct():
    command = DemoCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="unknown demo action"):
        command.handle(action="not-a-demo")


def test_execution_command_unknown_action_direct():
    command = ExecutionCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="unknown execution action"):
        command.handle(action="bogus", status=None, limit=50, execution_id=1, cli_secret=None)


@pytest.mark.django_db
def test_create_execution_recovers_from_scheduled_occurrence_conflict(job_factory, now):
    job = job_factory()
    existing = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="preexisting-key",
        status=ExecutionStatus.PENDING,
        is_manual=False,
    )
    execution, created = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    assert created is False
    assert execution.pk == existing.pk


@pytest.mark.django_db
def test_create_occurrences_coalesce_late_fire_times(job_factory, now):
    job = job_factory(
        misfire_policy=MisfirePolicy.COALESCE,
        misfire_grace_seconds=60,
    )
    service = SchedulerService()
    fire_times = [
        now - timedelta(minutes=5),
        now - timedelta(minutes=4),
        now - timedelta(minutes=3),
    ]
    created, missed, _duplicates = service._create_occurrences(job, fire_times, now)
    assert created == 1
    assert missed == 2
    assert job.executions.filter(status=ExecutionStatus.MISSED).count() == 2


@pytest.mark.django_db
def test_scheduler_tick_coalesce_emits_backlog_misfire_event(job_factory, now):
    job = job_factory(
        misfire_policy=MisfirePolicy.COALESCE,
        misfire_grace_seconds=30,
        next_run_at=now - timedelta(minutes=10),
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=10)).isoformat()},
    )
    from scheduler_app.models import EventType, JobEvent
    from scheduler_app.services.clock import FrozenClock

    SchedulerService(clock=FrozenClock(now)).tick()
    assert JobEvent.objects.filter(job=job, event_type=EventType.MISFIRE).exists()
