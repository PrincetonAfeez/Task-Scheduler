""" Views for the scheduler app. """

from __future__ import annotations

import json
from datetime import timedelta

from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, connection
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from scheduler_app.auth import webui_login_required, webui_public_read_or_login_required
from scheduler_app.forms import JobForm
from scheduler_app.models import Alert, ExecutionStatus, Job, JobExecution
from scheduler_app.services.cache import (
    dashboard_summary,
    invalidate_scheduler_cache,
    job_stats,
    queue_depth,
    upcoming_all_cached,
    upcoming_for_job_cached,
)
from scheduler_app.services.claiming import cancel_execution, cancel_queued_executions_for_job
from scheduler_app.services.clock import SystemClock
from scheduler_app.services.due import create_manual_execution
from scheduler_app.services.health import health_snapshot
from scheduler_app.services.job_schedule import apply_next_run_after_edit
from scheduler_app.services.operator_audit import emit_operator_action
from scheduler_app.services.retries import retry_execution

LIST_PAGE_SIZE = 50
DASHBOARD_JOBS_LIMIT = 25
DASHBOARD_ACTIVITY_LIMIT = 10


def home(request: HttpRequest) -> HttpResponse:
    return render(request, "scheduler_app/home.html")


def _paginate(request: HttpRequest, queryset, *, page_size: int = LIST_PAGE_SIZE):
    paginator = Paginator(queryset, page_size)
    return paginator.get_page(request.GET.get("page"))


@webui_login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    now = SystemClock().now()
    jobs_total = Job.objects.count()
    context = {
        "summary": dashboard_summary(),
        "queue": queue_depth(),
        "upcoming": upcoming_all_cached(count=10, now=now),
        "jobs": Job.objects.order_by("next_run_at", "name")[:DASHBOARD_JOBS_LIMIT],
        "jobs_total": jobs_total,
        "jobs_limit": DASHBOARD_JOBS_LIMIT,
        "executions": JobExecution.objects.select_related("job").order_by("-created_at")[
            :DASHBOARD_ACTIVITY_LIMIT
        ],
        **health_snapshot(),
    }
    return render(request, "scheduler_app/dashboard.html", context)


@webui_login_required
def summary_fragment(request: HttpRequest) -> HttpResponse:
    return render(request, "scheduler_app/fragments/summary.html", {"summary": dashboard_summary()})


@webui_login_required
def queue_fragment(request: HttpRequest) -> HttpResponse:
    return render(request, "scheduler_app/fragments/queue.html", {"queue": queue_depth()})


@webui_login_required
def health_fragment(request: HttpRequest) -> HttpResponse:
    return render(request, "scheduler_app/fragments/health.html", health_snapshot())


@webui_login_required
def activity_fragment(request: HttpRequest) -> HttpResponse:
    executions = JobExecution.objects.select_related("job").order_by("-created_at")[:10]
    return render(request, "scheduler_app/fragments/activity.html", {"executions": executions})


@webui_login_required
def upcoming_fragment(request: HttpRequest) -> HttpResponse:
    now = SystemClock().now()
    return render(
        request,
        "scheduler_app/fragments/upcoming.html",
        {"upcoming": upcoming_all_cached(count=10, now=now)},
    )


@webui_login_required
def jobs_fragment(request: HttpRequest) -> HttpResponse:
    jobs_total = Job.objects.count()
    jobs = Job.objects.order_by("next_run_at", "name")[:DASHBOARD_JOBS_LIMIT]
    return render(
        request,
        "scheduler_app/fragments/jobs.html",
        {"jobs": jobs, "jobs_total": jobs_total, "jobs_limit": DASHBOARD_JOBS_LIMIT},
    )


def job_list(request: HttpRequest) -> HttpResponse:
    jobs = _paginate(request, Job.objects.order_by("next_run_at", "name"))
    return render(request, "scheduler_app/jobs/list.html", {"jobs": jobs})


@webui_public_read_or_login_required
def job_detail(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(Job, pk=job_id)
    now = SystemClock().now()
    executions = _paginate(
        request,
        job.executions.order_by("-created_at"),
        page_size=30,
    )
    context = {
        "job": job,
        "executions": executions,
        "stats": job_stats(job),
        "upcoming": upcoming_for_job_cached(job, count=10, now=now) if job.enabled else [],
    }
    if request.user.is_authenticated:
        context["task_config_json"] = json.dumps(job.task_config, indent=2, sort_keys=True)
    return render(request, "scheduler_app/jobs/detail.html", context)


@webui_login_required
def job_create(request: HttpRequest) -> HttpResponse:
    form = JobForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            job = form.save()
        except IntegrityError:
            form.add_error("name", "A job with this name already exists.")
        else:
            invalidate_scheduler_cache("job created", job=job)
            emit_operator_action(action="job_create", message=f"Created job {job.name}", job=job, user=request.user)
            messages.success(request, f"Created job {job.name}.")
            return redirect("scheduler_app:job_detail", job_id=job.id)
    return render(request, "scheduler_app/jobs/form.html", {"form": form, "title": "Create Job"})


@webui_login_required
def job_edit(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(Job, pk=job_id)
    form = JobForm(request.POST or None, instance=job)
    if request.method == "POST" and form.is_valid():
        try:
            job = form.save()
        except IntegrityError:
            form.add_error("name", "A job with this name already exists.")
        else:
            invalidate_scheduler_cache("job edited", job=job)
            emit_operator_action(action="job_edit", message=f"Updated job {job.name}", job=job, user=request.user)
            messages.success(request, f"Updated job {job.name}.")
            return redirect("scheduler_app:job_detail", job_id=job.id)
    return render(request, "scheduler_app/jobs/form.html", {"form": form, "job": job, "title": "Edit Job"})


@webui_login_required
@require_POST
def job_delete(request: HttpRequest, job_id: int) -> HttpResponse:
    job = get_object_or_404(Job, pk=job_id)
    confirm_name = request.POST.get("confirm_name", "").strip()
    if confirm_name != job.name:
        messages.error(request, "Type the job name exactly to confirm deletion.")
        return redirect("scheduler_app:job_detail", job_id=job.id)
    name = job.name
    emit_operator_action(action="job_delete", message=f"Deleted job {name}", job=job, user=request.user)
    invalidate_scheduler_cache("job deleted", job=job)
    job.delete()
    messages.success(request, f"Deleted job {name}.")
    return redirect("scheduler_app:job_list")


@webui_login_required
@require_POST
def job_action(request: HttpRequest, job_id: int, action: str) -> HttpResponse:
    job = get_object_or_404(Job, pk=job_id)
    now = SystemClock().now()
    if action == "enable":
        previous = Job.objects.get(pk=job.pk)
        job.enabled = True
        try:
            apply_next_run_after_edit(
                job,
                now=now,
                schedule_changed=False,
                re_enabled=True,
                previous=previous,
            )
        except ValueError as exc:
            job.enabled = False
            messages.error(request, str(exc))
            return redirect("scheduler_app:job_detail", job_id=job.id)
        job.save(update_fields=["enabled", "next_run_at", "updated_at"])
        invalidate_scheduler_cache("job enabled", job=job)
        emit_operator_action(action="job_enable", message=f"Enabled {job.name}", job=job, user=request.user)
        messages.success(request, f"Enabled {job.name}.")
    elif action == "disable":
        job.enabled = False
        job.save(update_fields=["enabled", "updated_at"])
        cancelled = cancel_queued_executions_for_job(job, reason="job disabled")
        invalidate_scheduler_cache("job disabled", job=job)
        emit_operator_action(
            action="job_disable",
            message=f"Disabled {job.name} (cancelled {cancelled} queued execution(s))",
            job=job,
            user=request.user,
            data={"cancelled": cancelled},
        )
        messages.success(request, f"Disabled {job.name} (cancelled {cancelled} queued execution(s)).")
    elif action == "trigger":
        if not job.enabled:
            messages.error(request, "Enable the job before triggering a scheduled run, or re-enable it first.")
            return redirect("scheduler_app:job_detail", job_id=job.id)
        execution = create_manual_execution(job, now=now, requested_by=request.user.get_username())
        emit_operator_action(
            action="job_trigger",
            message=f"Manual trigger for {job.name} created execution {execution.id}",
            job=job,
            execution=execution,
            user=request.user,
        )
        messages.success(request, f"Created manual execution {execution.id}.")
    else:
        return HttpResponseBadRequest("unknown job action")
    return redirect("scheduler_app:job_detail", job_id=job.id)


@webui_public_read_or_login_required
def execution_list(request: HttpRequest) -> HttpResponse:
    executions = _paginate(
        request,
        JobExecution.objects.select_related("job").order_by("-created_at"),
    )
    return render(request, "scheduler_app/executions/list.html", {"executions": executions})


@webui_login_required
def execution_detail(request: HttpRequest, execution_id: int) -> HttpResponse:
    execution = get_object_or_404(JobExecution.objects.select_related("job"), pk=execution_id)
    return render(request, "scheduler_app/executions/detail.html", {"execution": execution})


@webui_login_required
@require_POST
def execution_action(request: HttpRequest, execution_id: int, action: str) -> HttpResponse:
    execution = get_object_or_404(JobExecution.objects.select_related("job"), pk=execution_id)
    now = SystemClock().now()
    if action == "retry":
        if execution.status not in [
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.DEAD_LETTERED,
            ExecutionStatus.CANCELLED,
        ]:
            return HttpResponseBadRequest("execution is not retryable")
        retry = retry_execution(execution, now=now)
        emit_operator_action(
            action="execution_retry",
            message=f"Manual retry of execution {execution.id} created {retry.id}",
            job=execution.job,
            execution=retry,
            user=request.user,
            data={"original_execution_id": execution.id},
        )
        messages.success(request, f"Created retry execution {retry.id}.")
    elif action == "cancel":
        try:
            cancel_execution(execution)
            emit_operator_action(
                action="execution_cancel",
                message=f"Cancelled execution {execution.id}",
                job=execution.job,
                execution=execution,
                user=request.user,
            )
            messages.success(request, f"Cancelled execution {execution.id}.")
        except ValueError as exc:
            messages.error(request, str(exc))
    else:
        return HttpResponseBadRequest("unknown execution action")
    return redirect("scheduler_app:execution_detail", execution_id=execution.id)


@webui_login_required
def alert_list(request: HttpRequest) -> HttpResponse:
    alerts = _paginate(request, Alert.objects.select_related("job", "execution").order_by("-created_at"))
    return render(request, "scheduler_app/alerts/list.html", {"alerts": alerts})


@webui_login_required
@require_POST
def alert_resolve(request: HttpRequest, alert_id: int) -> HttpResponse:
    alert = get_object_or_404(Alert, pk=alert_id)
    if not alert.resolved:
        alert.resolved = True
        alert.save(update_fields=["resolved"])
        emit_operator_action(
            action="alert_resolve",
            message=f"Resolved alert {alert.id}",
            job=alert.job,
            execution=alert.execution,
            user=request.user,
            data={"alert_id": alert.id},
        )
        messages.success(request, "Alert marked resolved.")
    return redirect("scheduler_app:alert_list")


@webui_login_required
@require_POST
def alert_resolve_bulk(request: HttpRequest) -> HttpResponse:
    raw_ids = request.POST.getlist("alert_ids")
    alert_ids: list[int] = []
    for raw in raw_ids:
        try:
            alert_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not alert_ids:
        messages.error(request, "Select at least one unresolved alert.")
        return redirect("scheduler_app:alert_list")
    alerts = list(Alert.objects.filter(id__in=alert_ids, resolved=False))
    for alert in alerts:
        alert.resolved = True
        alert.save(update_fields=["resolved"])
        emit_operator_action(
            action="alert_resolve",
            message=f"Resolved alert {alert.id}",
            job=alert.job,
            execution=alert.execution,
            user=request.user,
            data={"alert_id": alert.id, "bulk": True},
        )
    messages.success(request, f"Resolved {len(alerts)} alert(s).")
    return redirect("scheduler_app:alert_list")


@webui_login_required
def health_view(request: HttpRequest) -> HttpResponse:
    context = health_snapshot()
    context["queue"] = queue_depth()
    return render(request, "scheduler_app/health.html", context)


def healthz(request: HttpRequest) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain")


def readyz(request: HttpRequest) -> HttpResponse:
    try:
        connection.ensure_connection()
    except Exception:
        return HttpResponse("database unavailable", content_type="text/plain", status=503)

    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        return HttpResponse("migrations pending", content_type="text/plain", status=503)

    from django.conf import settings as django_settings
    from django.utils import timezone

    from scheduler_app.models import SchedulerHeartbeat, WorkerHeartbeat

    cache_backend = str(django_settings.CACHES.get("default", {}).get("BACKEND", ""))
    if django_settings.REDIS_URL and cache_backend.endswith("RedisCache"):
        try:
            from django_redis import get_redis_connection

            get_redis_connection("default").ping()
        except Exception:
            return HttpResponse("redis unavailable", content_type="text/plain", status=503)
    else:
        try:
            cache.set("readyz-probe", "1", timeout=1)
        except Exception:
            return HttpResponse("cache unavailable", content_type="text/plain", status=503)

    if getattr(django_settings, "READYZ_REQUIRE_HEARTBEATS", False):
        max_age = getattr(django_settings, "READYZ_HEARTBEAT_MAX_AGE_SECONDS", 300)
        cutoff = timezone.now() - timedelta(seconds=max_age)
        if not SchedulerHeartbeat.objects.filter(last_tick_at__gte=cutoff).exists():
            return HttpResponse("scheduler heartbeat stale", content_type="text/plain", status=503)

    if getattr(django_settings, "READYZ_REQUIRE_WORKER_HEARTBEAT", False):
        max_age = getattr(django_settings, "READYZ_HEARTBEAT_MAX_AGE_SECONDS", 300)
        cutoff = timezone.now() - timedelta(seconds=max_age)
        if not WorkerHeartbeat.objects.filter(last_heartbeat_at__gte=cutoff).exists():
            return HttpResponse("worker heartbeat stale", content_type="text/plain", status=503)

    return HttpResponse("ready", content_type="text/plain")
