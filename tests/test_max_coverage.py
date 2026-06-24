"""Exhaustive coverage tests for remaining modules, branches, and edge cases."""

from __future__ import annotations

import json
import signal
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import override_settings
from django.urls import reverse

from scheduler_app.checks import scheduler_cli_secret_missing_check
from scheduler_app.forms import JobForm
from scheduler_app.login_throttle import clear_login_failures, is_login_blocked, record_login_failure
from scheduler_app.management.commands.demo import Command as DemoCommand
from scheduler_app.management.commands.job import Command as JobCommand
from scheduler_app.models import (
    Alert,
    DeadLetter,
    EventType,
    ExecutionStatus,
    Job,
    JobExecution,
    MisfirePolicy,
    ScheduleType,
    SchedulerHeartbeat,
    WorkerHeartbeat,
)
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import (
    SchedulerService,
    create_execution_for_occurrence,
    heal_enabled_jobs_missing_next_run,
    scheduled_idempotency_key,
)
from scheduler_app.services.executors import InProcessExecutor, SubprocessExecutor
from scheduler_app.services.job_schedule import (
    one_time_run_at_changed,
    one_time_run_at_instant,
    schedule_fields_changed,
    validate_schedule_for_job,
)
from scheduler_app.services.operator_audit import emit_cli_operator_action, emit_operator_action
from scheduler_app.services.overlap import has_active_execution
from scheduler_app.services.schedules import (
    interval_seconds,
    parse_duration_seconds,
    upcoming_runs_for_job,
)
from scheduler_app.services import shutdown as shutdown_module
from scheduler_app.services.shutdown import (
    install_shutdown_handlers,
    interruptible_sleep,
    reset_shutdown_flag,
)
from scheduler_app.services.worker import _worker_thread_loop
from scheduler_app.tasks.registry import TaskContext, TaskSpec


# --- checks ---


def test_cli_secret_check_silent_when_secret_configured():
    with override_settings(DEBUG=False, SCHEDULER_CLI_SECRET="configured-secret"):
        assert scheduler_cli_secret_missing_check(None) == []


# --- login throttle ---


@pytest.mark.django_db
def test_login_throttle_incr_and_touch_fallbacks():
    cache.clear()

    class BrokenCache:
        def add(self, *args, **kwargs):
            return False

        def incr(self, key):
            raise ValueError("missing")

        def set(self, *args, **kwargs):
            return True

        def get(self, key, default=0):
            return 0

        def touch(self, *args, **kwargs):
            raise RuntimeError("touch failed")

        def delete(self, *args, **kwargs):
            return True

    with patch("scheduler_app.login_throttle.cache", BrokenCache()):
        record_login_failure(username="operator", ip="1.2.3.4")
        assert is_login_blocked(username="operator", ip="1.2.3.4") is False

    cache.clear()
    record_login_failure(username="op", ip="9.9.9.9")
    record_login_failure(username="op", ip="9.9.9.9")
    clear_login_failures(username="op", ip="9.9.9.9")
    assert is_login_blocked(username="op", ip="9.9.9.9") is False


# --- shutdown ---


def test_shutdown_signal_handlers_and_sleep():
    reset_shutdown_flag()
    install_shutdown_handlers()
    shutdown_module._handle_shutdown_signal(signal.SIGTERM, None)
    assert shutdown_module.shutdown_requested() is True
    reset_shutdown_flag()
    assert interruptible_sleep(0.01) is False


def test_worker_thread_loop_exits_on_shutdown(monkeypatch):
    monkeypatch.setattr("scheduler_app.services.worker.shutdown_requested", lambda: True)
    monkeypatch.setattr(
        "scheduler_app.services.worker.claim_runnable_executions",
        lambda **kwargs: [],
    )
    monkeypatch.setattr("scheduler_app.services.worker.update_worker_heartbeat", lambda **kwargs: None)
    monkeypatch.setattr("scheduler_app.services.worker.interruptible_sleep", lambda _s: False)
    clock = type("Clock", (), {"now": lambda self: None})()
    _worker_thread_loop(
        base_worker_id="shutdown-worker",
        clock=clock,
        executor=object(),
        sleep_seconds=0.01,
        stop_after=None,
        completed_counter=[0],
        counter_lock=__import__("threading").Lock(),
    )


def test_scheduler_loop_exits_on_shutdown():
    from scheduler_app.management.commands.scheduler import Command as SchedulerCommand

    command = SchedulerCommand()
    command.stdout = StringIO()
    with (
        patch("scheduler_app.management.commands.scheduler.install_shutdown_handlers"),
        patch("scheduler_app.management.commands.scheduler.recover_expired_leases", return_value=0),
        patch("scheduler_app.management.commands.scheduler.prune_stale_worker_heartbeats", return_value=0),
        patch("scheduler_app.management.commands.scheduler.prune_stale_scheduler_heartbeats", return_value=0),
        patch("scheduler_app.management.commands.scheduler.SchedulerService") as mock_service,
        patch("scheduler_app.management.commands.scheduler.shutdown_requested", side_effect=[False, True]),
        patch("scheduler_app.management.commands.scheduler.interruptible_sleep", return_value=False),
    ):
        mock_service.return_value.tick.return_value = MagicMock(
            due_jobs=0, created=0, missed=0, duplicates=0
        )
        command.handle(action="run", once=False, tick_seconds=1.0, scheduler_id="shutdown-test")
    assert "shutdown requested" in command.stdout.getvalue()


# --- job_schedule ---


def test_one_time_run_at_instant_missing(now):
    assert one_time_run_at_instant({}) is None
    assert one_time_run_at_changed({}, {}, timezone_name="UTC", previous_timezone="UTC") is False
    assert one_time_run_at_changed(
        {"run_at": now.isoformat()},
        {},
        timezone_name="UTC",
        previous_timezone="UTC",
    ) is True
    assert schedule_fields_changed(["name"]) is False


@pytest.mark.django_db
def test_validate_schedule_for_job(job_factory, now):
    job = job_factory()
    validate_schedule_for_job(job, now=now)


# --- schedules ---


def test_parse_duration_zero_integer_rejected():
    with pytest.raises(ValueError, match="greater than zero"):
        parse_duration_seconds("0")


def test_interval_seconds_requires_shape():
    with pytest.raises(ValueError, match="requires"):
        interval_seconds({})


@pytest.mark.django_db
def test_upcoming_runs_without_next_run_requires_now(job_factory):
    job = job_factory(next_run_at=None)
    with pytest.raises(ValueError, match="now is required"):
        upcoming_runs_for_job(job, count=3)


# --- overlap ---


@pytest.mark.django_db
def test_has_active_execution_excludes_due_retry(job_factory, now):
    job = job_factory()
    retry = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="exclude-retry",
        status=ExecutionStatus.RETRY_SCHEDULED,
    )
    assert has_active_execution(job.id, now=now, exclude_execution_id=retry.id) is False


# --- due ---


def test_scheduled_idempotency_key_format(now):
    key = scheduled_idempotency_key(42, now)
    assert key.startswith("scheduled:42:")


@pytest.mark.django_db
def test_create_execution_integrity_error_returns_existing(job_factory, now):
    job = job_factory()
    existing = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key=scheduled_idempotency_key(job.id, now),
        status=ExecutionStatus.PENDING,
    )
    with patch(
        "scheduler_app.services.due.JobExecution.objects.get_or_create",
        side_effect=IntegrityError,
    ):
        execution, created = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    assert created is False
    assert execution.pk == existing.pk


@pytest.mark.django_db
def test_heal_skips_jobs_that_no_longer_qualify(job_factory, now):
    job = job_factory(enabled=True, next_run_at=None, schedule_type=ScheduleType.INTERVAL)
    locked = Job.objects.get(pk=job.pk)
    locked.enabled = False
    locked.save(update_fields=["enabled"])
    assert heal_enabled_jobs_missing_next_run(now=now) == 0


@pytest.mark.django_db
def test_scheduler_tick_counts_duplicate_occurrences(job_factory, now):
    job = job_factory(enabled=True, next_run_at=now)
    create_execution_for_occurrence(job, scheduled_for=now, now=now)
    service = SchedulerService(clock=FrozenClock(now), scheduler_id="dup-tick")
    with patch("scheduler_app.services.due.due_fire_times", return_value=([now], now + timedelta(seconds=60))):
        result = service.tick()
    assert result.duplicates >= 1


@pytest.mark.django_db
def test_scheduler_tick_skips_job_with_no_fire_times(job_factory, now):
    job_factory(enabled=True, next_run_at=now, misfire_policy=MisfirePolicy.CATCH_UP)
    service = SchedulerService(clock=FrozenClock(now), scheduler_id="no-fire")
    with patch("scheduler_app.services.due.due_fire_times", return_value=([], None)):
        result = service.tick()
    assert result.advanced == 0


# --- executors ---


def test_subprocess_executor_invalid_json_stdout():
    executor = SubprocessExecutor()
    completed = MagicMock(returncode=0, stdout="not-json", stderr="")
    with patch("scheduler_app.services.executors.subprocess.run", return_value=completed):
        result = executor.run(
            task_name="always_succeed",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
    assert result.status == ExecutionStatus.FAILED
    assert "invalid JSON" in result.error


def test_inprocess_executor_missing_callable():
    executor = InProcessExecutor()
    with patch("scheduler_app.services.executors.get_task") as mock_get:
        mock_get.return_value = TaskSpec(
            name="broken",
            description="",
            func=None,
            idempotent=True,
            safe_for_demo=False,
        )
        result = executor.run(
            task_name="broken",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
    assert result.status == ExecutionStatus.FAILED


def test_inprocess_executor_user_exception():
    executor = InProcessExecutor()

    def boom(_config, _context):
        raise RuntimeError("demo failure")

    with patch("scheduler_app.services.executors.get_task") as mock_get:
        mock_get.return_value = TaskSpec(
            name="boom",
            description="",
            func=boom,
            idempotent=False,
            safe_for_demo=True,
        )
        result = executor.run(
            task_name="boom",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
    assert result.status == ExecutionStatus.FAILED
    assert "RuntimeError" in result.error


# --- subprocess_runner ---


def test_subprocess_runner_missing_callable(tmp_path):
    from scheduler_app.services import subprocess_runner

    payload = {
        "task_name": "missing_callable",
        "config": {},
        "context": {
            "job_id": 1,
            "execution_id": 1,
            "attempt_number": 1,
            "idempotency_key": "k",
            "scheduled_for": None,
            "worker_id": "w",
        },
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with (
        patch("scheduler_app.tasks.registry.get_task") as mock_get,
        patch.object(subprocess_runner.sys, "argv", ["runner", str(path)]),
    ):
        mock_get.return_value = TaskSpec(
            name="missing_callable",
            description="",
            func=None,
            idempotent=True,
            safe_for_demo=False,
        )
        assert subprocess_runner.main() == 0


# --- operator audit ---


@pytest.mark.django_db
def test_emit_operator_action_with_actor(job_factory):
    job = job_factory()
    emit_operator_action(action="test", message="actor test", job=job, actor="script")
    event = job.events.latest("created_at")
    assert event.data["username"] == "script"
    emit_cli_operator_action(action="cli_test", message="cli", job=job)
    assert job.events.filter(event_type=EventType.OPERATOR_ACTION, data__source="cli").exists()


# --- forms ---


@pytest.mark.django_db
def test_job_form_task_config_validation_error(job_factory):
    form = JobForm(
        data={
            "name": "bad-config",
            "description": "",
            "registered_task_name": "sleep_for_seconds",
            "task_config": '{"seconds": "not-a-number"}',
            "schedule_type": ScheduleType.INTERVAL,
            "schedule_value": '{"every": "30s"}',
            "timezone": "UTC",
            "enabled": "on",
            "overlap_policy": "skip",
            "misfire_policy": "coalesce",
            "misfire_grace_seconds": "60",
            "max_attempts": "3",
            "retry_backoff_seconds": "10",
            "timeout_seconds": "30",
            "retention_count": "500",
            "retention_days": "30",
            "alert_mode": "web",
        }
    )
    assert form.is_valid() is False
    assert "task_config" in form.errors or "registered_task_name" in form.errors


@pytest.mark.django_db
def test_job_form_one_time_reenable_validation(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="form-reenable-test").tick()
    job.refresh_from_db()
    assert job.enabled is False
    assert job.next_run_at is None

    form = JobForm(
        data={
            "name": job.name,
            "description": "",
            "registered_task_name": job.registered_task_name,
            "task_config": "{}",
            "schedule_type": ScheduleType.ONE_TIME,
            "schedule_value": json.dumps(job.schedule_value),
            "timezone": job.timezone,
            "enabled": "on",
            "overlap_policy": job.overlap_policy,
            "misfire_policy": job.misfire_policy,
            "misfire_grace_seconds": str(job.misfire_grace_seconds),
            "max_attempts": str(job.max_attempts),
            "retry_backoff_seconds": str(job.retry_backoff_seconds),
            "timeout_seconds": str(job.timeout_seconds),
            "retention_count": str(job.retention_count),
            "retention_days": str(job.retention_days),
            "alert_mode": job.alert_mode,
        },
        instance=job,
    )
    assert form.is_valid()
    with pytest.raises(ValidationError) as exc_info:
        form.save()
    assert "enabled" in exc_info.value.error_dict


# --- management: job CLI ---


@pytest.mark.django_db
def test_job_cli_validation_errors():
    with pytest.raises(CommandError, match="JSON value must be an object"):
        call_command(
            "job",
            "add",
            "bad-json",
            "always_succeed",
            "interval",
            "--every",
            "60s",
            "--config",
            "[]",
        )

    with pytest.raises(CommandError, match="invalid JSON"):
        call_command(
            "job",
            "add",
            "bad-json-obj",
            "always_succeed",
            "interval",
            "--every",
            "60s",
            "--config",
            "not-json",
        )

    with pytest.raises(CommandError, match="one_time"):
        call_command("job", "add", "missing-run-at", "always_succeed", "one_time")

    with pytest.raises(CommandError, match="interval"):
        call_command("job", "add", "missing-every", "always_succeed", "interval")

    with pytest.raises(CommandError, match="cron"):
        call_command("job", "add", "missing-cron", "always_succeed", "cron")

    with pytest.raises(CommandError, match="pass --yes"):
        call_command("job", "delete", "1")


@pytest.mark.django_db
def test_job_cli_edit_all_fields(job_factory):
    job = job_factory(name="edit-all")
    call_command(
        "job",
        "edit",
        str(job.id),
        "--name",
        "edit-all-renamed",
        "--task",
        "always_succeed",
        "--schedule-type",
        "interval",
        "--schedule-value",
        '{"every": "120s"}',
        "--timezone",
        "UTC",
        "--config",
        "{}",
        "--disabled",
    )
    job.refresh_from_db()
    assert job.name == "edit-all-renamed"
    assert job.enabled is False


@pytest.mark.django_db
def test_job_cli_add_with_schedule_value():
    call_command(
        "job",
        "add",
        "schedule-value-job",
        "always_succeed",
        "interval",
        "--schedule-value",
        '{"every": "90s"}',
    )
    assert Job.objects.filter(name="schedule-value-job").exists()


def test_job_command_unknown_action_direct():
    command = JobCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="unknown job action"):
        command.handle(action="nope")


# --- management: demo ---


@pytest.mark.django_db
def test_demo_misfire_command():
    out = StringIO()
    call_command("demo", "misfire", stdout=out)
    assert "coalesce" in out.getvalue()


@pytest.mark.django_db
def test_demo_timeout_command(job_factory, now):
    command = DemoCommand()
    command.stdout = StringIO()
    with patch.object(command, "_timeout") as mock_timeout:
        mock_timeout.return_value = None
        command.handle(action="timeout")
    mock_timeout.assert_called_once()


def test_demo_single_fire_rejects_sqlite():
    command = DemoCommand()
    command.stdout = StringIO()
    with pytest.raises(CommandError, match="PostgreSQL"):
        command.handle(action="single-fire")


# --- management: health, alerts, ensure_dev_user ---


@pytest.mark.django_db
def test_health_command_prints_snapshots(now):
    SchedulerHeartbeat.objects.create(
        scheduler_id="health-cli",
        hostname="test",
        process_id=1,
        last_tick_at=now,
        health_state="healthy",
    )
    WorkerHeartbeat.objects.create(
        worker_id="worker-cli",
        hostname="test",
        process_id=2,
        last_heartbeat_at=now,
        health_state="idle",
    )
    out = StringIO()
    call_command("health", stdout=out)
    body = out.getvalue()
    assert "health-cli" in body
    assert "worker-cli" in body


@pytest.mark.django_db
def test_alerts_list_includes_dead_letters(job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="dl-list",
        status=ExecutionStatus.DEAD_LETTERED,
    )
    DeadLetter.objects.create(job=job, execution=execution, reason="test", final_error="err")
    Alert.objects.create(job=job, message="alert row")
    out = StringIO()
    call_command("alerts", "list", stdout=out)
    body = out.getvalue()
    assert "Dead letters" in body
    assert "alert row" in body


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_ensure_dev_user_creates_and_skips_existing():
    out = StringIO()
    call_command("ensure_dev_user", stdout=out)
    assert User.objects.filter(username="admin").exists()
    call_command("ensure_dev_user", stdout=out)
    assert "already exists" in out.getvalue()


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_ensure_dev_user_refuses_default_password_in_production():
    with pytest.raises(CommandError, match="Refusing"):
        call_command("ensure_dev_user")


# --- views & auth ---


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_job_edit_integrity_error(auth_client, job_factory, monkeypatch):
    job = job_factory(name="edit-integrity")

    def raise_integrity(*args, **kwargs):
        raise IntegrityError

    monkeypatch.setattr(JobForm, "save", raise_integrity)
    response = auth_client.post(
        reverse("scheduler_app:job_edit", args=[job.id]),
        {
            "name": "edit-integrity",
            "description": "",
            "registered_task_name": "always_succeed",
            "task_config": "{}",
            "schedule_type": "interval",
            "schedule_value": '{"every": "30s"}',
            "timezone": "UTC",
            "enabled": "on",
            "overlap_policy": "skip",
            "misfire_policy": "coalesce",
            "misfire_grace_seconds": "60",
            "max_attempts": "3",
            "retry_backoff_seconds": "10",
            "timeout_seconds": "30",
            "retention_count": "500",
            "retention_days": "30",
            "alert_mode": "web",
        },
    )
    assert response.status_code == 200
    assert "already exists" in response.content.decode()


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_alert_resolve_bulk_empty_selection(auth_client):
    response = auth_client.post(reverse("scheduler_app:alert_resolve_bulk"), {})
    assert response.status_code == 302
    follow = auth_client.get(reverse("scheduler_app:alert_list"))
    assert "Select at least one" in follow.content.decode()


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_alert_resolve_bulk_ignores_invalid_ids(auth_client):
    alert = Alert.objects.create(message="bulk-valid", resolved=False)
    response = auth_client.post(
        reverse("scheduler_app:alert_resolve_bulk"),
        {"alert_ids": ["not-int", str(alert.id)]},
    )
    assert response.status_code == 302
    alert.refresh_from_db()
    assert alert.resolved is True


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True)
def test_alert_resolve_idempotent(auth_client):
    alert = Alert.objects.create(message="already", resolved=True)
    response = auth_client.post(reverse("scheduler_app:alert_resolve", args=[alert.id]))
    assert response.status_code == 302


@pytest.mark.django_db
def test_readyz_database_unavailable(client):
    with patch("scheduler_app.views.connection.ensure_connection", side_effect=Exception("db down")):
        response = client.get(reverse("scheduler_app:readyz"))
    assert response.status_code == 503
    assert "database unavailable" in response.content.decode()


@pytest.mark.django_db
def test_readyz_pending_migrations(client):
    with patch("django.db.migrations.executor.MigrationExecutor") as mock_executor:
        mock_executor.return_value.migration_plan.return_value = [("app", "0001")]
        response = client.get(reverse("scheduler_app:readyz"))
    assert response.status_code == 503
    assert "migrations pending" in response.content.decode()


@pytest.mark.django_db
def test_readyz_cache_probe_failure(client):
    with patch("scheduler_app.views.cache.set", side_effect=Exception("cache down")):
        response = client.get(reverse("scheduler_app:readyz"))
    assert response.status_code == 503
    assert "cache unavailable" in response.content.decode()


@pytest.mark.django_db
def test_readyz_redis_unavailable(client, settings):
    settings.REDIS_URL = "redis://localhost:6379/0"
    settings.CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": "redis://localhost:6379/0",
        }
    }
    with (
        patch("django.db.migrations.executor.MigrationExecutor") as mock_executor,
        patch("django_redis.get_redis_connection", side_effect=Exception("redis down")),
    ):
        mock_executor.return_value.migration_plan.return_value = []
        response = client.get(reverse("scheduler_app:readyz"))
    assert response.status_code == 503
    assert "redis unavailable" in response.content.decode()


@pytest.mark.django_db
@override_settings(
    WEBUI_AUTH_ENABLED=True,
    WEBUI_PUBLIC_READ=False,
)
def test_public_read_disabled_redirects_anonymous(client, job_factory):
    job = job_factory()
    response = client.get(reverse("scheduler_app:job_detail", args=[job.id]))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True, LOGIN_RATE_LIMIT_ATTEMPTS=2)
def test_throttled_login_view_blocks_and_records(client):
    User.objects.create_user(username="operator", password="secret")
    for _ in range(2):
        client.post(reverse("login"), {"username": "operator", "password": "wrong"})
    response = client.post(reverse("login"), {"username": "operator", "password": "wrong"})
    assert response.status_code == 200
    assert "Too many failed" in response.content.decode()


# --- models ---


@pytest.mark.django_db
@override_settings(ALERT_MODE="invalid-mode")
def test_default_alert_mode_falls_back(job_factory):
    job = job_factory()
    assert job.alert_mode in {"web", "log_only"}


@pytest.mark.django_db
def test_execution_is_terminal_property(job_factory, now):
    job = job_factory()
    pending = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="terminal-pending",
        status=ExecutionStatus.PENDING,
    )
    assert pending.is_terminal is False
    assert str(pending).startswith(job.name)


# --- package ---


def test_scheduler_app_version():
    import scheduler_app

    assert scheduler_app.__version__
