""" Test misfire policies for the scheduler app. """

from __future__ import annotations

from datetime import timedelta

import pytest

from scheduler_app.models import EventType, ExecutionStatus, MisfirePolicy, OverlapPolicy
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService


def _backlog_job(job_factory, now, *, policy):
    return job_factory(
        misfire_policy=policy,
        misfire_grace_seconds=30,
        overlap_policy=OverlapPolicy.ALLOW,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
        next_run_at=now - timedelta(minutes=5),
    )


@pytest.mark.django_db
def test_coalesce_collapses_backlog_into_single_run(job_factory, now):
    job = _backlog_job(job_factory, now, policy=MisfirePolicy.COALESCE)
    SchedulerService(clock=FrozenClock(now), scheduler_id="test").tick()

    executions = list(job.executions.all())
    assert len(executions) == 1
    assert executions[0].status == ExecutionStatus.PENDING

    job.refresh_from_db()
    # The whole backlog is drained in one tick; next_run_at moves into the future.
    assert job.next_run_at > now
    assert job.events.filter(event_type=EventType.MISFIRE).exists()


@pytest.mark.django_db
def test_catch_up_creates_each_missed_occurrence(job_factory, now):
    job = _backlog_job(job_factory, now, policy=MisfirePolicy.CATCH_UP)
    SchedulerService(clock=FrozenClock(now), scheduler_id="test").tick()

    statuses = list(job.executions.values_list("status", flat=True))
    # next_run_at .. now inclusive at 60s steps => 6 occurrences, all runnable.
    assert len(statuses) == 6
    assert set(statuses) == {ExecutionStatus.PENDING}


@pytest.mark.django_db
def test_catch_up_respects_safety_cap(job_factory, now, settings):
    settings.MISFIRE_CATCH_UP_CAP = 3
    job = _backlog_job(job_factory, now, policy=MisfirePolicy.CATCH_UP)
    SchedulerService(clock=FrozenClock(now), scheduler_id="test").tick()
    assert job.executions.count() == 3
