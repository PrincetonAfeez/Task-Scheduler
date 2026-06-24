"""Target tests for remaining uncovered branches."""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from django.urls import reverse

from scheduler_app.forms import JobForm
from scheduler_app.models import ExecutionStatus, JobExecution, MisfirePolicy, ScheduleType
from scheduler_app.services import subprocess_runner
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService, create_execution_for_occurrence
from scheduler_app.services.executors import InProcessExecutor
from scheduler_app.services.retention import prune_job_history
from scheduler_app.tasks.registry import TaskContext


@pytest.mark.django_db
def test_create_execution_integrity_error_recovery(job_factory, now):
    job = job_factory()
    first, created = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    assert created is True
    second, created_again = create_execution_for_occurrence(job, scheduled_for=now, now=now)
    assert created_again is False
    assert first.id == second.id


@pytest.mark.django_db
def test_prune_job_history_empty_keep_ids(job_factory):
    job = job_factory(retention_count=5, retention_days=0)
    assert prune_job_history(job) == 0


@pytest.mark.django_db
def test_scheduler_tick_skip_misfire_policy(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
        misfire_policy=MisfirePolicy.SKIP,
        misfire_grace_seconds=30,
        next_run_at=now - timedelta(minutes=5),
    )
    SchedulerService(clock=FrozenClock(now)).tick()
    assert job.executions.filter(status=ExecutionStatus.MISSED).exists()


@pytest.mark.django_db
def test_scheduler_tick_catch_up_policy(job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.INTERVAL,
        schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=3)).isoformat()},
        misfire_policy=MisfirePolicy.CATCH_UP,
        misfire_grace_seconds=3600,
        next_run_at=now - timedelta(minutes=3),
    )
    SchedulerService(clock=FrozenClock(now)).tick()
    assert job.executions.count() >= 1


@pytest.mark.django_db
def test_job_form_clean_registered_task_validator():
    form = JobForm()
    form.cleaned_data = {"registered_task_name": "always_succeed"}
    with patch("scheduler_app.forms.registered_tasks", return_value={}):
        with pytest.raises(ValidationError, match="registered task"):
            form.clean_registered_task_name()


def test_inprocess_executor_missing_callable():
    executor = InProcessExecutor()
    with patch("scheduler_app.services.executors.get_task") as mock_get:
        mock_get.return_value.func = None
        result = executor.run(
            task_name="broken",
            config={},
            context=TaskContext(1, 1, 1, "k", None, "w"),
            timeout_seconds=5,
        )
    assert result.status == ExecutionStatus.FAILED


@pytest.mark.django_db
def test_subprocess_runner_with_scheduled_for(tmp_path):
    payload = {
        "task_name": "always_succeed",
        "config": {},
        "context": {
            "execution_id": 1,
            "job_id": 1,
            "attempt_number": 1,
            "idempotency_key": "with-sched",
            "scheduled_for": "2026-01-01T12:00:00+00:00",
            "worker_id": "runner",
        },
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with patch.object(subprocess_runner.sys, "argv", ["subprocess_runner", str(path)]):
        assert subprocess_runner.main() == 0


def test_subprocess_runner_missing_task(tmp_path):
    payload = {
        "task_name": "definitely_not_a_task",
        "config": {},
        "context": {
            "execution_id": 1,
            "job_id": 1,
            "attempt_number": 1,
            "idempotency_key": "missing-task",
            "scheduled_for": None,
            "worker_id": "runner",
        },
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stdout = StringIO()
    with patch.object(subprocess_runner.sys, "argv", ["subprocess_runner", str(path)]):
        with patch.object(subprocess_runner.sys, "stdout", stdout):
            assert subprocess_runner.main() == 0
    result = json.loads(stdout.getvalue())
    assert result["status"] == ExecutionStatus.FAILED



@pytest.mark.django_db
def test_job_cli_edit(job_factory):
    job = job_factory(name="edit-me")
    call_command("job", "edit", str(job.id), "--name", "edited-name")
    job.refresh_from_db()
    assert job.name == "edited-name"


@pytest.mark.django_db
def test_health_command_empty_workers():
    out = StringIO()
    call_command("health", stdout=out)
    assert "Workers" in out.getvalue()


@pytest.mark.django_db
def test_execution_cancel_success_message(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now + timedelta(hours=3),
        run_after=now,
        idempotency_key="cancel-msg",
        status=ExecutionStatus.PENDING,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "cancel"])
    response = auth_client.post(url)
    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.CANCELLED


@pytest.mark.django_db
def test_create_alert_without_execution():
    from scheduler_app.services.alerts import create_alert

    alert = create_alert(execution=None, message="system alert")
    assert alert is not None
