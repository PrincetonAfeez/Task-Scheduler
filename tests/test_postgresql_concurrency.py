""" Test PostgreSQL concurrency for the scheduler app. """

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from scheduler_app.models import JobExecution
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_two_schedulers_create_one_scheduled_occurrence_on_postgresql(job_factory, now):
    if connection.vendor != "postgresql":
        pytest.skip("single-fire row-locking test requires PostgreSQL")

    job = job_factory(name="postgres-single-fire", next_run_at=now)
    clock = FrozenClock(now)

    def run_tick(scheduler_id: str):
        connections.close_all()
        return SchedulerService(clock=clock, scheduler_id=scheduler_id).tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run_tick, ["postgres-a", "postgres-b"]))

    assert JobExecution.objects.filter(job=job, scheduled_for=now, is_manual=False).count() == 1

