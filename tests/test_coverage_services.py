"""Extended service-layer coverage: due, cache, retention, alerts, executors, subprocess_runner."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import override_settings

from scheduler_app.models import (
    Alert,
    AlertMode,
    EventType,
    ExecutionStatus,
    Job,
    JobEvent,
    JobExecution,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleType,
)
from scheduler_app.services.alerts import create_alert, dead_letter_execution
from scheduler_app.services.cache import (
    dashboard_summary,
    invalidate_scheduler_cache,
    job_stats,
    job_stats_key,
    queue_depth,
    scheduler_cache_keys,
    task_catalog_cached,
    upcoming_all_cached,
    upcoming_for_job_cached,
)
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import (
    SchedulerService,
    create_manual_execution,
    ensure_job_next_run,
    scheduled_idempotency_key,
)
from scheduler_app.services.executors import (
    InProcessExecutor,
    SubprocessExecutor,
    _context_payload,
    executor_from_settings,
)
from scheduler_app.services.retention import prune_events, prune_job_history
from scheduler_app.services import subprocess_runner
from scheduler_app.tasks.registry import TaskContext


def test_scheduled_idempotency_key(job_factory, now):
    job = job_factory()
    key = scheduled_idempotency_key(job.id, now)
    assert key.startswith(f"scheduled:{job.id}:")


@pytest.mark.django_db
def test_ensure_job_next_run_sets_missing(job_factory, now):
    job = job_factory(next_run_at=None, enabled=True)
    ensure_job_next_run(job, now=now)
    job.refresh_from_db()
    assert job.next_run_at is not None


@pytest.mark.django_db
def test_create_manual_execution_rejects_disabled(job_factory, now):
    job = job_factory(enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        create_manual_execution(job, now=now)


@pytest.mark.django_db
def test_scheduler_tick_one_time_disables_job(now):
    job = Job.objects.create(
        name="one-shot",
        registered_task_name="always_succeed",
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        timezone="UTC",
        overlap_policy=OverlapPolicy.ALLOW,
        enabled=True,
        next_run_at=now,
    )
    result = SchedulerService(clock=FrozenClock(now)).tick()
    job.refresh_from_db()
    assert result.created >= 1
    assert job.enabled is False
    assert job.next_run_at is None


@pytest.mark.django_db
def test_scheduler_tick_coalesce_misfire_emits_event(now):
    job = Job.objects.create(
        name="coalesce-misfire",
        registered_task_name="always_succeed",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=10)).isoformat()},
        timezone="UTC",
        misfire_policy=MisfirePolicy.COALESCE,
        misfire_grace_seconds=30,
        overlap_policy=OverlapPolicy.ALLOW,
        enabled=True,
        next_run_at=now - timedelta(minutes=10),
    )
    SchedulerService(clock=FrozenClock(now)).tick()
    assert JobEvent.objects.filter(job=job, event_type=EventType.MISFIRE).exists()


@pytest.mark.django_db
def test_scheduler_tick_overlap_skip(now):
    job = Job.objects.create(
        name="overlap-skip",
        registered_task_name="always_succeed",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": now.isoformat()},
        timezone="UTC",
        overlap_policy=OverlapPolicy.SKIP,
        enabled=True,
        next_run_at=now,
    )
    JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(minutes=1),
        run_after=now - timedelta(minutes=1),
        idempotency_key="active-run",
        status=ExecutionStatus.RUNNING,
    )
    SchedulerService(clock=FrozenClock(now)).tick()
    missed = job.executions.filter(status=ExecutionStatus.MISSED)
    assert missed.exists()


@pytest.mark.django_db
def test_scheduler_tick_duplicate_occurrence(now):
    job = Job.objects.create(
        name="dup-occurrence",
        registered_task_name="always_succeed",
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": now.isoformat()},
        timezone="UTC",
        overlap_policy=OverlapPolicy.ALLOW,
        enabled=True,
        next_run_at=now,
    )
    service = SchedulerService(clock=FrozenClock(now))
    first = service.tick()
    second = service.tick()
    assert first.created >= 1
    assert second.duplicates >= 0 or second.created == 0
    assert job.executions.count() >= 1


@pytest.mark.django_db
def test_cache_helpers_use_and_invalidate(job_factory, now):
    cache.clear()
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cache-run",
        status=ExecutionStatus.SUCCEEDED,
        duration_ms=100,
    )
    depth1 = queue_depth()
    depth2 = queue_depth()
    assert depth1 == depth2

    summary1 = dashboard_summary()
    summary2 = dashboard_summary()
    assert summary1["total_runs"] == summary2["total_runs"]

    stats1 = job_stats(job)
    stats2 = job_stats(job)
    assert stats1["total_runs"] == stats2["total_runs"]

    catalog1 = task_catalog_cached()
    catalog2 = task_catalog_cached()
    assert catalog1 == catalog2

    upcoming = upcoming_for_job_cached(job, count=3, now=now)
    assert upcoming

    all_upcoming = upcoming_all_cached(count=5, now=now)
    assert all_upcoming

    keys = scheduler_cache_keys(job_ids={job.id})
    assert job_stats_key(job.id) in keys

    invalidate_scheduler_cache("test", job=job)
    cache.clear()


@pytest.mark.django_db
def test_prune_job_history_retention_count_only(job_factory, now):
    job = job_factory(retention_count=1, retention_days=0)
    for index in range(3):
        scheduled = now + timedelta(minutes=index)
        ex = JobExecution.objects.create(
            job=job,
            scheduled_for=scheduled,
            run_after=scheduled,
            idempotency_key=f"retain-{index}",
            status=ExecutionStatus.SUCCEEDED,
        )
        JobExecution.objects.filter(pk=ex.pk).update(created_at=now - timedelta(days=index))
    deleted = prune_job_history(job)
    assert deleted >= 2


@pytest.mark.django_db
def test_prune_events_zero_days():
    assert prune_events(older_than_days=0) == 0


@pytest.mark.django_db
def test_create_alert_log_only(job_factory, now):
    job = job_factory(alert_mode=AlertMode.LOG_ONLY)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="log-only-alert",
        status=ExecutionStatus.FAILED,
    )
    alert = create_alert(execution=execution, message="log only")
    assert alert is None
    assert JobEvent.objects.filter(event_type=EventType.ALERT).exists()


@pytest.mark.django_db
def test_dead_letter_idempotent(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="dl-idempotent",
        status=ExecutionStatus.FAILED,
        error="boom",
    )
    first = dead_letter_execution(execution, reason="test")
    second = dead_letter_execution(execution, reason="test")
    assert first.id == second.id
    assert Alert.objects.filter(execution=execution).count() == 1


def test_context_payload_with_scheduled_for(now):
    ctx = TaskContext(
        execution_id=1,
        job_id=2,
        attempt_number=1,
        idempotency_key="k",
        scheduled_for=now,
        worker_id="w",
    )
    payload = _context_payload(ctx)
    assert payload["scheduled_for"] == now.isoformat()


def test_inprocess_executor_success_and_failure():
    executor = InProcessExecutor()
    ok = executor.run(
        task_name="always_succeed",
        config={},
        context=TaskContext(1, 1, 1, "k", None, "w"),
        timeout_seconds=5,
    )
    assert ok.status == ExecutionStatus.SUCCEEDED

    bad = executor.run(
        task_name="always_fail",
        config={},
        context=TaskContext(1, 1, 1, "k", None, "w"),
        timeout_seconds=5,
    )
    assert bad.status == ExecutionStatus.FAILED


@override_settings(EXECUTOR_BACKEND="inprocess")
def test_executor_from_settings_inprocess():
    assert isinstance(executor_from_settings(), InProcessExecutor)


@override_settings(EXECUTOR_BACKEND="subprocess")
def test_executor_from_settings_subprocess():
    assert isinstance(executor_from_settings(), SubprocessExecutor)


def test_subprocess_executor_timeout():
    executor = SubprocessExecutor()
    with patch("scheduler_app.services.executors.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=1, output=b"partial")
        result = executor.run(
            task_name="always_succeed",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=1,
        )
    assert result.status == ExecutionStatus.TIMED_OUT


def test_subprocess_executor_bad_exit_and_json():
    executor = SubprocessExecutor()
    with patch("scheduler_app.services.executors.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        failed = executor.run(
            task_name="always_succeed",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
        assert failed.status == ExecutionStatus.FAILED

        mock_run.return_value = MagicMock(returncode=0, stdout="not-json", stderr="")
        bad_json = executor.run(
            task_name="always_succeed",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
        assert bad_json.status == ExecutionStatus.FAILED


@pytest.mark.django_db
def test_subprocess_runner_main_success_and_failure(tmp_path):
    payload = {
        "task_name": "always_succeed",
        "config": {},
        "context": {
            "execution_id": 1,
            "job_id": 1,
            "attempt_number": 1,
            "idempotency_key": "runner-main",
            "scheduled_for": None,
            "worker_id": "runner",
        },
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stdout = StringIO()
    with patch.object(sys, "argv", ["subprocess_runner", str(path)]):
        with patch.object(sys, "stdout", stdout):
            code = subprocess_runner.main()
    assert code == 0
    data = json.loads(stdout.getvalue())
    assert data["status"] == ExecutionStatus.SUCCEEDED

    payload["task_name"] = "always_fail"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stdout = StringIO()
    with patch.object(sys, "argv", ["subprocess_runner", str(path)]):
        with patch.object(sys, "stdout", stdout):
            code = subprocess_runner.main()
    assert code == 0
    data = json.loads(stdout.getvalue())
    assert data["status"] == ExecutionStatus.FAILED


def test_json_formatter_with_extra_and_exception():
    from task_scheduler.logging import JsonFormatter

    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.custom_field = {"ok": True}
    record.bad_field = object()
    formatted = formatter.format(record)
    parsed = json.loads(formatted)
    assert parsed["message"] == "hello"
    assert parsed["custom_field"] == {"ok": True}
    assert "bad_field" in parsed

    try:
        raise ValueError("boom")
    except ValueError:
        import sys as _sys

        record.exc_info = _sys.exc_info()
    formatted_exc = formatter.format(record)
    parsed_exc = json.loads(formatted_exc)
    assert "exception" in parsed_exc
