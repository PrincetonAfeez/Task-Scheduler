""" Admin for the scheduler app. """

from __future__ import annotations

from django.contrib import admin

from .models import (
    Alert,
    DeadLetter,
    Job,
    JobEvent,
    JobExecution,
    SchedulerHeartbeat,
    WorkerHeartbeat,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Job)
class JobAdmin(ReadOnlyAdmin):
    list_display = ("name", "registered_task_name", "schedule_type", "enabled", "next_run_at")
    list_filter = ("enabled", "schedule_type", "overlap_policy", "misfire_policy")
    search_fields = ("name", "registered_task_name")
    readonly_fields = (
        "name",
        "description",
        "registered_task_name",
        "task_config",
        "schedule_type",
        "schedule_value",
        "timezone",
        "overlap_policy",
        "misfire_policy",
        "misfire_grace_seconds",
        "max_attempts",
        "retry_backoff_seconds",
        "timeout_seconds",
        "retention_count",
        "retention_days",
        "alert_mode",
        "enabled",
        "next_run_at",
        "last_run_at",
        "created_at",
        "updated_at",
    )


@admin.register(JobExecution)
class JobExecutionAdmin(ReadOnlyAdmin):
    list_display = ("id", "job", "status", "scheduled_for", "attempt_number", "worker_id")
    list_filter = ("status", "is_manual")
    search_fields = ("job__name", "idempotency_key", "worker_id")
    readonly_fields = (
        "job",
        "scheduled_for",
        "run_after",
        "status",
        "attempt_number",
        "is_manual",
        "idempotency_key",
        "claimed_by",
        "claimed_at",
        "worker_id",
        "started_at",
        "finished_at",
        "duration_ms",
        "output",
        "error",
        "lease_expires_at",
        "created_at",
        "updated_at",
    )


@admin.register(JobEvent)
class JobEventAdmin(ReadOnlyAdmin):
    list_display = ("event_type", "job", "execution", "created_at")
    list_filter = ("event_type",)
    search_fields = ("message", "job__name")


@admin.register(Alert)
class AlertAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "severity", "job", "execution", "resolved", "message")
    list_filter = ("severity", "resolved")


@admin.register(DeadLetter)
class DeadLetterAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "job", "execution", "attempts_used", "reason")
    search_fields = ("reason", "job__name")


@admin.register(SchedulerHeartbeat)
class SchedulerHeartbeatAdmin(ReadOnlyAdmin):
    list_display = ("scheduler_id", "health_state", "last_tick_at")


@admin.register(WorkerHeartbeat)
class WorkerHeartbeatAdmin(ReadOnlyAdmin):
    list_display = ("worker_id", "health_state", "last_heartbeat_at")
