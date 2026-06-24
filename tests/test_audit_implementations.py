""" Test audit implementations for the scheduler app. """

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError

from scheduler_app.admin import JobAdmin, JobExecutionAdmin
from scheduler_app.models import Job, JobExecution, ScheduleType
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService, heal_enabled_jobs_missing_next_run
from scheduler_app.services.job_schedule import apply_next_run_after_edit


@pytest.mark.django_db
def test_apply_next_run_rejects_completed_one_time_reenable(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="one-time-test").tick()
    job.refresh_from_db()
    previous = Job.objects.get(pk=job.pk)
    assert job.enabled is False
    assert job.next_run_at is None
    job.enabled = True
    with pytest.raises(ValueError, match="already run"):
        apply_next_run_after_edit(
            job,
            now=now,
            schedule_changed=False,
            re_enabled=True,
            previous=previous,
        )


@pytest.mark.django_db
def test_completed_one_time_rejects_timezone_edit_and_enable(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="tz-one-time").tick()
    previous = Job.objects.get(pk=job.pk)
    job.refresh_from_db()
    job.enabled = True
    job.timezone = "America/New_York"
    with pytest.raises(ValueError, match="already run"):
        apply_next_run_after_edit(
            job,
            now=now,
            schedule_changed=True,
            re_enabled=True,
            previous=previous,
        )


@pytest.mark.django_db
def test_completed_one_time_allows_new_run_at(job_factory, now):
    from datetime import timedelta

    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="reschedule-one-time").tick()
    previous = Job.objects.get(pk=job.pk)
    job.refresh_from_db()
    future = now + timedelta(days=1)
    job.enabled = True
    job.schedule_value = {"run_at": future.isoformat()}
    apply_next_run_after_edit(
        job,
        now=now,
        schedule_changed=True,
        re_enabled=True,
        previous=previous,
    )
    assert job.next_run_at == future


@pytest.mark.django_db
def test_multi_job_claim_invalidates_all_job_stats(job_factory, now, django_capture_on_commit_callbacks):
    from django.core.cache import cache

    from scheduler_app.models import ExecutionStatus
    from scheduler_app.services.cache import job_stats_key
    from scheduler_app.services.claiming import claim_runnable_executions

    jobs = [job_factory(name=f"multi-{index}") for index in range(2)]
    for index, job in enumerate(jobs):
        job.executions.create(
            scheduled_for=now,
            run_after=now,
            idempotency_key=f"multi-claim-{index}",
            status=ExecutionStatus.PENDING,
        )
        cache.set(job_stats_key(job.id), {"stale": True})
    with django_capture_on_commit_callbacks(execute=True):
        claim_runnable_executions(worker_id="multi-cache", now=now, limit=2)
    for job in jobs:
        assert cache.get(job_stats_key(job.id)) is None


@pytest.mark.django_db
def test_heal_enabled_job_missing_next_run(job_factory, now):
    job = job_factory(enabled=True, next_run_at=None, schedule_type=ScheduleType.INTERVAL)
    assert heal_enabled_jobs_missing_next_run(now=now) == 1
    job.refresh_from_db()
    assert job.next_run_at is not None


@pytest.mark.django_db
def test_heal_invalidates_job_cache(job_factory, now, django_capture_on_commit_callbacks):
    from django.core.cache import cache

    from scheduler_app.services.cache import job_stats_key

    job = job_factory(enabled=True, next_run_at=None, schedule_type=ScheduleType.INTERVAL)
    cache.set(job_stats_key(job.id), {"stale": True})
    with django_capture_on_commit_callbacks(execute=True):
        assert heal_enabled_jobs_missing_next_run(now=now) == 1
    assert cache.get(job_stats_key(job.id)) is None


@pytest.mark.django_db
def test_completed_one_time_rejects_equivalent_run_at_format(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="fmt-one-time").tick()
    previous = Job.objects.get(pk=job.pk)
    job.refresh_from_db()
    z_format = now.isoformat().replace("+00:00", "Z")
    job.enabled = True
    job.schedule_value = {"run_at": z_format}
    with pytest.raises(ValueError, match="already run"):
        apply_next_run_after_edit(
            job,
            now=now,
            schedule_changed=True,
            re_enabled=True,
            previous=previous,
        )


def test_one_time_run_at_changed_normalizes_equivalent_formats(now):
    from scheduler_app.services.job_schedule import one_time_run_at_changed

    iso = now.isoformat()
    z_format = iso.replace("+00:00", "Z")
    assert one_time_run_at_changed(
        {"run_at": iso},
        {"run_at": z_format},
        timezone_name="UTC",
        previous_timezone="UTC",
    ) is False


@pytest.mark.django_db
def test_job_execution_admin_cannot_delete():
    site = AdminSite()
    admin = JobExecutionAdmin(JobExecution, site)
    assert admin.has_delete_permission(None) is False
    assert admin.has_change_permission(None) is False


@pytest.mark.django_db
def test_cli_add_rejects_duplicate_job_name(job_factory):
    job_factory(name="cli-dup")
    with pytest.raises(CommandError, match="already exists"):
        call_command(
            "job",
            "add",
            "cli-dup",
            "always_succeed",
            "interval",
            "--every",
            "60s",
        )


@pytest.mark.django_db
def test_cli_enable_rejects_completed_one_time(job_factory, now):
    job = job_factory(
        name="cli-one-time",
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="cli-one-time").tick()
    with pytest.raises(CommandError, match="already run"):
        call_command("job", "enable", str(job.id))


@pytest.mark.django_db
def test_cli_edit_rejects_incompatible_schedule_type(job_factory):
    job = job_factory(schedule_value={"every": "60s"})
    with pytest.raises(CommandError, match="invalid schedule"):
        call_command("job", "edit", str(job.id), "--schedule-type", "cron")


@pytest.mark.django_db
def test_job_admin_cannot_delete():
    site = AdminSite()
    admin = JobAdmin(Job, site)
    assert admin.has_delete_permission(None) is False
    assert admin.has_change_permission(None) is False


@pytest.mark.django_db
def test_webui_auth_disabled_emits_deploy_warning():
    from django.core.checks import run_checks

    from scheduler_app.checks import webui_auth_disabled_check

    warnings = webui_auth_disabled_check(None)
    assert not warnings

    from django.test import override_settings

    with override_settings(WEBUI_AUTH_ENABLED=False):
        warnings = webui_auth_disabled_check(None)
        assert len(warnings) == 1
        assert warnings[0].id == "scheduler_app.W001"
        assert any(item.id == "scheduler_app.W001" for item in run_checks())


def test_parse_duration_bare_integer_seconds():
    from scheduler_app.services.schedules import parse_duration_seconds

    assert parse_duration_seconds("120") == 120


def test_interval_seconds_requires_every_or_units():
    from scheduler_app.services.schedules import interval_seconds

    with pytest.raises(ValueError, match="requires seconds"):
        interval_seconds({})


def test_validate_task_config_rejects_bool_as_number():
    from scheduler_app.services.task_config import validate_task_config

    with pytest.raises(ValueError, match="must be a number"):
        validate_task_config("sleep_for_seconds", {"seconds": True})


@pytest.mark.django_db
def test_worker_idle_loop_sleeps_when_nothing_to_claim(monkeypatch):
    from scheduler_app.services import worker as worker_module
    from scheduler_app.services.worker import _worker_thread_loop

    sleep_calls: list[float] = []

    def fake_interruptible_sleep(seconds: float) -> bool:
        sleep_calls.append(seconds)
        raise StopIteration

    monkeypatch.setattr(worker_module, "interruptible_sleep", fake_interruptible_sleep)
    monkeypatch.setattr(worker_module, "claim_runnable_executions", lambda **kwargs: [])
    monkeypatch.setattr(worker_module, "update_worker_heartbeat", lambda **kwargs: None)

    clock = type("Clock", (), {"now": lambda self: None})()
    with pytest.raises(StopIteration):
        _worker_thread_loop(
            base_worker_id="idle-test",
            clock=clock,
            executor=object(),
            sleep_seconds=0.01,
            stop_after=None,
            completed_counter=[0],
            counter_lock=__import__("threading").Lock(),
        )
    assert sleep_calls == [0.01]
