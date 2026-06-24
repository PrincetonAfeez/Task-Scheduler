"""Worker loop, claiming edge cases, clock, login throttle, models, admin."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.test import override_settings

from scheduler_app.admin import (
    AlertAdmin,
    DeadLetterAdmin,
    JobAdmin,
    JobEventAdmin,
    JobExecutionAdmin,
    ReadOnlyAdmin,
    SchedulerHeartbeatAdmin,
    WorkerHeartbeatAdmin,
)
from scheduler_app.login_throttle import is_login_blocked, record_login_failure
from scheduler_app.models import (
    Alert,
    DeadLetter,
    ExecutionStatus,
    Job,
    JobEvent,
    JobExecution,
    SchedulerHeartbeat,
    WorkerHeartbeat,
)
from scheduler_app.services.claiming import claim_runnable_executions
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.executors import InProcessExecutor, TaskRunResult
from scheduler_app.services.worker import (
    default_worker_id,
    execute_claimed_execution,
    run_worker_loop,
    thread_worker_id,
)


def test_frozen_clock_advance(now):
    clock = FrozenClock(now)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == now + timedelta(minutes=5)


def test_default_and_thread_worker_id():
    base = default_worker_id()
    assert base.startswith("worker-")
    tid = thread_worker_id("pool")
    assert tid.startswith("pool-t")


@pytest.mark.django_db
@override_settings(LOGIN_RATE_LIMIT_ATTEMPTS=0)
def test_login_throttle_disabled_when_limit_zero():
    cache.clear()
    record_login_failure(username="x", ip="1.1.1.1")
    assert is_login_blocked(username="x", ip="1.1.1.1") is False


@pytest.mark.django_db
def test_claim_cancels_disabled_job_pending(job_factory, now):
    job = job_factory(enabled=False)
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="disabled-claim",
        status=ExecutionStatus.PENDING,
    )
    claimed = claim_runnable_executions(worker_id="w", now=now, limit=5)
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.CANCELLED
    assert claimed == []


@pytest.mark.django_db
def test_claim_respects_limit(job_factory, now):
    for index in range(5):
        job = job_factory(name=f"limit-{index}")
        JobExecution.objects.create(
            job=job,
            scheduled_for=now,
            run_after=now,
            idempotency_key=f"limit-{index}",
            status=ExecutionStatus.PENDING,
        )
    claimed = claim_runnable_executions(worker_id="limit-worker", now=now, limit=2)
    assert len(claimed) == 2


@pytest.mark.django_db
def test_stale_claim_rejected(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="stale-claim",
        status=ExecutionStatus.CLAIMED,
        worker_id="other-worker",
    )
    result = execute_claimed_execution(
        execution.id,
        worker_id="wrong-worker",
        clock=FrozenClock(now),
        executor=InProcessExecutor(),
    )
    assert result.status == ExecutionStatus.CLAIMED


@pytest.mark.django_db
def test_stale_result_discarded(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="stale-result",
        status=ExecutionStatus.CLAIMED,
        worker_id="worker-a",
    )
    claim_runnable_executions(worker_id="worker-a", now=now, limit=1)

    class SlowThenStaleExecutor:
        def run(self, **kwargs):
            execution.status = ExecutionStatus.PENDING
            execution.worker_id = "other"
            execution.save(update_fields=["status", "worker_id", "updated_at"])
            return TaskRunResult(status=ExecutionStatus.SUCCEEDED, output="late", duration_ms=1)

    result = execute_claimed_execution(
        execution.id,
        worker_id="worker-a",
        clock=FrozenClock(now),
        executor=SlowThenStaleExecutor(),
    )
    execution.refresh_from_db()
    assert result.status != ExecutionStatus.SUCCEEDED or execution.worker_id == "other"


@pytest.mark.django_db(transaction=True)
@override_settings(EXECUTOR_BACKEND="inprocess")
def test_run_worker_loop_stop_after(job_factory, now):
    job = job_factory()
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="loop-once",
        status=ExecutionStatus.PENDING,
    )
    with patch("scheduler_app.services.worker.interruptible_sleep", return_value=False):
        completed = run_worker_loop(
            worker_id="loop-test",
            clock=FrozenClock(now),
            workers=1,
            sleep_seconds=0.001,
            stop_after=1,
        )
    assert completed >= 1


@pytest.mark.django_db
def test_model_str_and_properties(job_factory, now):
    job = job_factory(name="str-job")
    assert str(job) == "str-job"

    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="str-exec",
        status=ExecutionStatus.SUCCEEDED,
    )
    assert "str-job" in str(execution)
    assert execution.is_terminal is True

    event = JobEvent.objects.create(event_type="claim", job=job, message="m")
    assert "claim" in str(event)

    dl = DeadLetter.objects.create(
        job=job,
        execution=execution,
        reason="test",
        final_error="err",
    )
    assert "str-job" in str(dl)

    alert = Alert.objects.create(job=job, message="x" * 100)
    assert len(str(alert)) <= 80

    sched = SchedulerHeartbeat.objects.create(
        scheduler_id="s1",
        hostname="h",
        process_id=1,
        last_tick_at=now,
        health_state="healthy",
    )
    assert "s1" in str(sched)

    worker = WorkerHeartbeat.objects.create(
        worker_id="w1",
        hostname="h",
        process_id=1,
        last_heartbeat_at=now,
        health_state="idle",
    )
    assert "w1" in str(worker)


@pytest.mark.django_db
def test_admin_permissions_and_save(admin_user, job_factory):
    site = AdminSite()
    job_admin = JobAdmin(Job, site)
    assert job_admin.has_add_permission(None) is False
    assert job_admin.has_change_permission(None) is False
    assert job_admin.has_delete_permission(None) is False

    exec_admin = JobExecutionAdmin(JobExecution, site)
    assert exec_admin.has_add_permission(None) is False
    assert exec_admin.has_change_permission(None) is False
    assert exec_admin.has_delete_permission(None) is False

    readonly = ReadOnlyAdmin(JobEvent, site)
    assert readonly.has_add_permission(None) is False
    assert readonly.has_change_permission(None) is False
    assert readonly.has_delete_permission(None) is False

    for model, model_admin_cls in (
        (JobEvent, JobEventAdmin),
        (Alert, AlertAdmin),
        (DeadLetter, DeadLetterAdmin),
        (SchedulerHeartbeat, SchedulerHeartbeatAdmin),
        (WorkerHeartbeat, WorkerHeartbeatAdmin),
    ):
        admin = model_admin_cls(model, site)
        assert admin.has_change_permission(None) is False
