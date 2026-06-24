""" Test views for the scheduler app. """

from __future__ import annotations

import pytest
from django.db import IntegrityError
from django.urls import reverse

from scheduler_app.forms import JobForm
from scheduler_app.models import ExecutionStatus, Job, JobExecution, ScheduleType
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.due import SchedulerService


PUBLIC_GET_ROUTES = [
    "scheduler_app:home",
    "scheduler_app:job_list",
    "scheduler_app:execution_list",
    "scheduler_app:healthz",
]

AUTH_GET_ROUTES = [
    "scheduler_app:dashboard",
    "scheduler_app:alert_list",
    "scheduler_app:health",
    "scheduler_app:job_create",
    "scheduler_app:summary_fragment",
    "scheduler_app:queue_fragment",
    "scheduler_app:health_fragment",
    "scheduler_app:activity_fragment",
    "scheduler_app:upcoming_fragment",
    "scheduler_app:jobs_fragment",
]


@pytest.mark.django_db
@pytest.mark.parametrize("name", PUBLIC_GET_ROUTES)
def test_public_get_routes_render(client, name):
    assert client.get(reverse(name)).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("name", AUTH_GET_ROUTES)
def test_auth_get_routes_require_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


@pytest.mark.django_db
@pytest.mark.parametrize("name", AUTH_GET_ROUTES)
def test_auth_get_routes_render_for_signed_in_user(auth_client, name):
    assert auth_client.get(reverse(name)).status_code == 200


@pytest.mark.django_db
def test_job_detail_is_public(client, job_factory):
    job = job_factory()
    assert client.get(reverse("scheduler_app:job_detail", args=[job.id])).status_code == 200


@pytest.mark.django_db
def test_job_edit_requires_login(client, job_factory):
    job = job_factory()
    assert client.get(reverse("scheduler_app:job_edit", args=[job.id])).status_code == 302


@pytest.mark.django_db
def test_job_edit_renders_for_signed_in_user(auth_client, job_factory):
    job = job_factory()
    assert auth_client.get(reverse("scheduler_app:job_edit", args=[job.id])).status_code == 200


@pytest.mark.django_db
def test_execution_detail_requires_login(client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="view-detail-anon",
        status=ExecutionStatus.SUCCEEDED,
    )
    url = reverse("scheduler_app:execution_detail", args=[execution.id])
    assert client.get(url).status_code == 302


@pytest.mark.django_db
def test_execution_detail_renders_for_signed_in_user(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="view-detail-auth",
        status=ExecutionStatus.SUCCEEDED,
    )
    url = reverse("scheduler_app:execution_detail", args=[execution.id])
    assert auth_client.get(url).status_code == 200


@pytest.mark.django_db
def test_job_create_post_creates_job(auth_client):
    data = {
        "name": "web-created",
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
    }
    response = auth_client.post(reverse("scheduler_app:job_create"), data)
    assert response.status_code == 302
    assert Job.objects.filter(name="web-created").exists()


@pytest.mark.django_db
def test_job_create_rejects_duplicate_name(auth_client, job_factory):
    job_factory(name="duplicate-name")
    data = {
        "name": "duplicate-name",
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
    }
    response = auth_client.post(reverse("scheduler_app:job_create"), data)
    assert response.status_code == 200
    assert "already exists" in response.content.decode()


def _valid_job_form_data(**overrides):
    data = {
        "name": "web-created",
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
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_job_create_handles_integrity_error_on_save(auth_client, monkeypatch):
    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError

    monkeypatch.setattr(JobForm, "save", raise_integrity_error)
    response = auth_client.post(
        reverse("scheduler_app:job_create"),
        _valid_job_form_data(name="race-create"),
    )
    assert response.status_code == 200
    assert "already exists" in response.content.decode()
    assert not Job.objects.filter(name="race-create").exists()


@pytest.mark.django_db
def test_job_edit_handles_integrity_error_on_save(auth_client, job_factory, monkeypatch):
    job = job_factory(name="edit-target")
    job_factory(name="other-job")

    def raise_integrity_error(*args, **kwargs):
        raise IntegrityError

    monkeypatch.setattr(JobForm, "save", raise_integrity_error)
    response = auth_client.post(
        reverse("scheduler_app:job_edit", args=[job.id]),
        _valid_job_form_data(name="other-job"),
    )
    assert response.status_code == 200
    assert "already exists" in response.content.decode()
    job.refresh_from_db()
    assert job.name == "edit-target"


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["enable", "disable", "trigger"])
def test_job_action_routes_redirect(auth_client, job_factory, action):
    job = job_factory()
    response = auth_client.post(reverse("scheduler_app:job_action", args=[job.id, action]))
    assert response.status_code == 302


@pytest.mark.django_db
def test_unknown_job_action_is_bad_request(auth_client, job_factory):
    job = job_factory()
    response = auth_client.post(reverse("scheduler_app:job_action", args=[job.id, "bogus"]))
    assert response.status_code == 400


@pytest.mark.django_db
def test_job_delete_requires_name_confirmation(auth_client, job_factory):
    job = job_factory()
    response = auth_client.post(reverse("scheduler_app:job_delete", args=[job.id]))
    assert response.status_code == 302
    assert Job.objects.filter(pk=job.id).exists()
    response = auth_client.post(
        reverse("scheduler_app:job_delete", args=[job.id]),
        {"confirm_name": job.name},
    )
    assert response.status_code == 302
    assert not Job.objects.filter(pk=job.id).exists()


@pytest.mark.django_db
def test_execution_retry_action_redirects(auth_client, job_factory, now):
    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="view-retry",
        status=ExecutionStatus.FAILED,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "retry"])
    response = auth_client.post(url)
    assert response.status_code == 302
    assert JobExecution.objects.filter(job=job, is_manual=True).exists()


@pytest.mark.django_db
def test_one_time_reenable_refused(auth_client, job_factory, now):
    job = job_factory(
        schedule_type=ScheduleType.ONE_TIME,
        schedule_value={"run_at": now.isoformat()},
        next_run_at=now,
    )
    SchedulerService(clock=FrozenClock(now), scheduler_id="reenable-test").tick()
    job.refresh_from_db()
    assert job.enabled is False
    assert job.next_run_at is None
    response = auth_client.post(reverse("scheduler_app:job_action", args=[job.id, "enable"]))
    assert response.status_code == 302
    job.refresh_from_db()
    assert job.enabled is False
