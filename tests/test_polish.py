""" Test polish for the scheduler app. """

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from scheduler_app.login_throttle import clear_login_failures, is_login_blocked, record_login_failure
from scheduler_app.models import ExecutionStatus, JobExecution, SchedulerHeartbeat, WorkerHeartbeat
from scheduler_app.services.retention import prune_all_jobs


def test_login_redirects_to_dashboard(client, django_user_model):
    django_user_model.objects.create_user(username="operator", password="secret")
    response = client.post(reverse("login"), {"username": "operator", "password": "secret"})
    assert response.status_code == 302
    assert response.url.endswith("/dashboard/")


@pytest.mark.django_db
def test_login_throttle_blocks_after_limit(settings):
    settings.LOGIN_RATE_LIMIT_ATTEMPTS = 2
    settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
    cache.clear()
    record_login_failure(username="operator", ip="1.2.3.4")
    record_login_failure(username="operator", ip="1.2.3.4")
    assert is_login_blocked(username="operator", ip="1.2.3.4") is True


def test_clear_login_failures_resets_counter(settings):
    settings.LOGIN_RATE_LIMIT_ATTEMPTS = 2
    cache.clear()
    record_login_failure(username="operator", ip="1.2.3.4")
    clear_login_failures(username="operator", ip="1.2.3.4")
    assert is_login_blocked(username="operator", ip="1.2.3.4") is False


@pytest.mark.django_db
def test_login_form_shows_throttle_message(client, django_user_model, settings):
    settings.LOGIN_RATE_LIMIT_ATTEMPTS = 1
    settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS = 300
    cache.clear()
    django_user_model.objects.create_user(username="operator", password="secret")
    client.post(reverse("login"), {"username": "operator", "password": "wrong"})
    response = client.post(reverse("login"), {"username": "operator", "password": "wrong"})
    assert response.status_code == 200
    body = response.content.decode()
    assert "Too many failed" in body
    assert 'class="message error"' in body


@pytest.mark.django_db
@override_settings(READYZ_REQUIRE_WORKER_HEARTBEAT=True, READYZ_HEARTBEAT_MAX_AGE_SECONDS=300)
def test_readyz_requires_worker_heartbeat_when_configured(client):
    now = timezone.now()
    WorkerHeartbeat.objects.create(
        worker_id="stale-worker",
        hostname="test",
        process_id=1,
        last_heartbeat_at=now - timedelta(hours=1),
        health_state="healthy",
    )
    assert client.get("/readyz").status_code == 503
    WorkerHeartbeat.objects.filter(worker_id="stale-worker").delete()
    WorkerHeartbeat.objects.create(
        worker_id="fresh-worker",
        hostname="test",
        process_id=2,
        last_heartbeat_at=now,
        health_state="healthy",
    )
    assert client.get("/readyz").status_code == 200


@pytest.mark.django_db
@override_settings(WEBUI_AUTH_ENABLED=True, WEBUI_PUBLIC_READ=False)
def test_public_read_disabled_gates_job_detail(client, job_factory):
    job = job_factory()
    response = client.get(reverse("scheduler_app:job_detail", args=[job.id]))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@override_settings(READYZ_REQUIRE_HEARTBEATS=True, READYZ_HEARTBEAT_MAX_AGE_SECONDS=300)
def test_readyz_requires_scheduler_heartbeat_when_configured(client):
    now = timezone.now()
    SchedulerHeartbeat.objects.create(
        scheduler_id="stale",
        hostname="test",
        process_id=1,
        last_tick_at=now - timedelta(hours=1),
        health_state="healthy",
    )
    assert client.get("/readyz").status_code == 503
    SchedulerHeartbeat.objects.filter(scheduler_id="stale").delete()
    SchedulerHeartbeat.objects.create(
        scheduler_id="fresh",
        hostname="test",
        process_id=2,
        last_tick_at=now,
        health_state="healthy",
    )
    assert client.get("/readyz").status_code == 200


@pytest.mark.django_db
def test_subprocess_runner_executes_task():
    payload = {
        "task_name": "always_succeed",
        "config": {},
        "context": {
            "execution_id": 1,
            "job_id": 1,
            "attempt_number": 1,
            "idempotency_key": "subprocess-smoke",
            "scheduled_for": timezone.now().isoformat(),
            "worker_id": "test-worker",
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        path = handle.name
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "task_scheduler.test_settings"
    completed = subprocess.run(
        [sys.executable, "-m", "scheduler_app.services.subprocess_runner", path],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == ExecutionStatus.SUCCEEDED
    assert "succeeded" in result["output"].lower()


@pytest.mark.django_db
def test_subprocess_runner_reports_task_failure():
    payload = {
        "task_name": "always_fail",
        "config": {},
        "context": {
            "execution_id": 2,
            "job_id": 1,
            "attempt_number": 1,
            "idempotency_key": "subprocess-fail",
            "scheduled_for": timezone.now().isoformat(),
            "worker_id": "test-worker",
        },
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        path = handle.name
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "task_scheduler.test_settings"
    completed = subprocess.run(
        [sys.executable, "-m", "scheduler_app.services.subprocess_runner", path],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == ExecutionStatus.FAILED
    assert "Intentional" in result["error"] or "RuntimeError" in result["error"]


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_ensure_dev_user_creates_and_is_idempotent():
    out = StringIO()
    call_command("ensure_dev_user", stdout=out)
    assert "created dev superuser" in out.getvalue() or "already exists" in out.getvalue()
    out = StringIO()
    call_command("ensure_dev_user", stdout=out)
    assert "already exists" in out.getvalue()


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_ensure_dev_user_refuses_default_password_when_debug_off():
    with pytest.raises(CommandError, match="DEV_ADMIN_PASSWORD"):
        call_command("ensure_dev_user")


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_ensure_dev_user_warns_on_default_credentials():
    err = StringIO()
    call_command("ensure_dev_user", stderr=err)
    assert "default credentials" in err.getvalue().lower() or "already exists" in err.getvalue().lower()


@pytest.mark.django_db
def test_health_command_smoke():
    call_command("health")


@pytest.mark.django_db
def test_prune_events_removes_old_job_events(job_factory, now):
    from scheduler_app.models import EventType, JobEvent
    from scheduler_app.services.retention import prune_events

    job = job_factory()
    JobEvent.objects.create(event_type=EventType.CLAIM, job=job, message="old")
    JobEvent.objects.filter(job=job).update(created_at=now - timedelta(days=60))
    deleted = prune_events(older_than_days=30)
    assert deleted >= 1


@pytest.mark.django_db
def test_prune_history_removes_old_terminal_runs(job_factory, now):
    job = job_factory(retention_count=1, retention_days=0)
    old = JobExecution.objects.create(
        job=job,
        scheduled_for=now - timedelta(days=10),
        run_after=now - timedelta(days=10),
        idempotency_key="old-run",
        status=ExecutionStatus.SUCCEEDED,
    )
    JobExecution.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=10))
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="keep-run",
        status=ExecutionStatus.SUCCEEDED,
    )
    deleted = prune_all_jobs()
    assert deleted >= 1
    assert not JobExecution.objects.filter(pk=old.pk).exists()


@pytest.mark.django_db(transaction=True)
@override_settings(EXECUTOR_BACKEND="inprocess")
def test_worker_thread_pool_completes_multiple(job_factory, now):
    from scheduler_app.services.dispatcher import dispatch_once

    job = job_factory(enabled=True)
    JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="pool-single",
        status=ExecutionStatus.PENDING,
    )
    result = dispatch_once(worker_id="pool-test", limit=1)
    assert result.completed == 1
    assert JobExecution.objects.filter(job=job, status=ExecutionStatus.SUCCEEDED).exists()


@pytest.mark.django_db
def test_authenticated_job_detail_shows_task_config(auth_client, job_factory):
    job = job_factory(
        registered_task_name="sleep_for_seconds",
        task_config={"seconds": 1},
    )
    response = auth_client.get(reverse("scheduler_app:job_detail", args=[job.id]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Task config" in body
    assert "seconds" in body
