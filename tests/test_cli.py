""" Test CLI for the scheduler app. """

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_cli_job_add_and_list():
    call_command("job", "add", "cli-job", "always_succeed", "interval", "--every", "30s")
    output = StringIO()
    call_command("job", "list", stdout=output)
    assert "cli-job" in output.getvalue()


@pytest.mark.django_db
def test_cli_job_add_rejects_unknown_task():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("job", "add", "bad-job", "not_a_real_task", "interval", "--every", "30s")


@pytest.mark.django_db
def test_cli_missing_job_reports_clean_error():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("job", "preview", "999999")


@pytest.mark.django_db
def test_cli_execution_list_smoke():
    call_command("execution", "list", stdout=StringIO())


@pytest.mark.django_db
def test_cli_health_smoke():
    output = StringIO()
    call_command("health", stdout=output)
    assert "Schedulers" in output.getvalue()


@pytest.mark.django_db
def test_cli_alerts_smoke():
    output = StringIO()
    call_command("alerts", "list", stdout=output)
    assert "Alerts" in output.getvalue()


@pytest.mark.django_db
def test_cli_prune_history_smoke():
    output = StringIO()
    call_command("prune_history", stdout=output)
    assert "pruned" in output.getvalue()


@pytest.mark.django_db
def test_cli_demo_misfire_smoke():
    output = StringIO()
    call_command("demo", "misfire", stdout=output)
    assert "coalesce" in output.getvalue().lower()
