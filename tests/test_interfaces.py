""" Test interfaces for the scheduler app. """

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from django.core.cache import cache

from scheduler_app.services.cache import (
    DASHBOARD_SUMMARY_KEY,
    invalidate_scheduler_cache,
    job_stats_key,
)


@pytest.mark.django_db
def test_cache_invalidation_is_targeted(job_factory, django_capture_on_commit_callbacks):
    job = job_factory()
    cache.set(DASHBOARD_SUMMARY_KEY, "stale")
    cache.set(job_stats_key(job.id), "stale")
    cache.set("unrelated-key", "keep")
    with django_capture_on_commit_callbacks(execute=True):
        invalidate_scheduler_cache("test", job=job)
    # Scheduler-owned keys are cleared; unrelated keys are preserved (not a FLUSHDB).
    assert cache.get(DASHBOARD_SUMMARY_KEY) is None
    assert cache.get(job_stats_key(job.id)) is None
    assert cache.get("unrelated-key") == "keep"


@pytest.mark.django_db
def test_dashboard_and_job_list_render(client, auth_client):
    assert client.get(reverse("scheduler_app:home")).status_code == 200
    assert auth_client.get(reverse("scheduler_app:dashboard")).status_code == 200
    assert client.get(reverse("scheduler_app:job_list")).status_code == 200


@pytest.mark.django_db
def test_cli_catalog_smoke():
    output = StringIO()
    call_command("job", "catalog", stdout=output)
    assert "always_succeed" in output.getvalue()


@pytest.mark.django_db
def test_cancel_running_execution_redirects_without_500(job_factory, now, auth_client):
    from scheduler_app.models import ExecutionStatus, JobExecution

    job = job_factory()
    execution = JobExecution.objects.create(
        job=job,
        scheduled_for=now,
        run_after=now,
        idempotency_key="cancel-running",
        status=ExecutionStatus.RUNNING,
    )
    url = reverse("scheduler_app:execution_action", args=[execution.id, "cancel"])
    response = auth_client.post(url)
    # A running execution cannot be cancelled: surface a message, not a 500.
    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.status == ExecutionStatus.RUNNING

