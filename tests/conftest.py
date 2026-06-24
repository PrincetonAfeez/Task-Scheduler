""" Fixtures for the scheduler app. """

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scheduler_app.models import Job, OverlapPolicy, ScheduleType


@pytest.fixture
def auth_client(client):
    from django.contrib.auth.models import User

    user = User.objects.create_user(username="operator", password="secret")
    client.force_login(user)
    return client


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def job_factory(db, now):
    def make_job(**overrides):
        defaults = {
            "name": f"job-{Job.objects.count() + 1}",
            "registered_task_name": "always_succeed",
            "schedule_type": ScheduleType.INTERVAL,
            "schedule_value": {"every": "60s", "start_at": now.isoformat()},
            "timezone": "UTC",
            "next_run_at": now,
            "overlap_policy": OverlapPolicy.ALLOW,
            "max_attempts": 3,
            "retry_backoff_seconds": 10,
            "timeout_seconds": 5,
        }
        defaults.update(overrides)
        return Job.objects.create(**defaults)

    return make_job

