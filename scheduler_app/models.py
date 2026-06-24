""" Models for the scheduler app. """

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class ScheduleType(models.TextChoices):
    ONE_TIME = "one_time", "One time"
    INTERVAL = "interval", "Fixed interval"
    CRON = "cron", "Cron"


class OverlapPolicy(models.TextChoices):
    SKIP = "skip", "Skip"
    QUEUE = "queue", "Queue"
    ALLOW = "allow", "Allow"


class MisfirePolicy(models.TextChoices):
    COALESCE = "coalesce", "Coalesce"
    CATCH_UP = "catch_up", "Catch up"
    SKIP = "skip", "Skip"


class ExecutionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CLAIMED = "claimed", "Claimed"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
    TIMED_OUT = "timed_out", "Timed out"
    MISSED = "missed", "Missed"
    CANCELLED = "cancelled", "Cancelled"
    DEAD_LETTERED = "dead_lettered", "Dead lettered"


ACTIVE_EXECUTION_STATUSES = [
    ExecutionStatus.CLAIMED,
    ExecutionStatus.RUNNING,
]

RUNNABLE_EXECUTION_STATUSES = [
    ExecutionStatus.PENDING,
    ExecutionStatus.RETRY_SCHEDULED,
]

TERMINAL_EXECUTION_STATUSES = [
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.TIMED_OUT,
    ExecutionStatus.MISSED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.DEAD_LETTERED,
]


class EventType(models.TextChoices):
    DUE_DETECTED = "due_detected", "Due detected"
    OCCURRENCE_CREATED = "occurrence_created", "Occurrence created"
    OCCURRENCE_EXISTS = "occurrence_exists", "Occurrence already exists"
    CLAIM = "claim", "Claim"
    DISPATCH = "dispatch", "Dispatch"
    WORKER_START = "worker_start", "Worker start"
    WORKER_FINISH = "worker_finish", "Worker finish"
    FAILURE = "failure", "Failure"
    RETRY_SCHEDULED = "retry_scheduled", "Retry scheduled"
    TIMEOUT = "timeout", "Timeout"
    MISFIRE = "misfire", "Misfire"
    LEASE_RECOVERY = "lease_recovery", "Lease recovery"
    STALE_CLAIM_REJECTED = "stale_claim_rejected", "Stale claim rejected"
    STALE_RESULT_DISCARDED = "stale_result_discarded", "Stale result discarded"
    MANUAL_RETRY = "manual_retry", "Manual retry"
    DEAD_LETTER = "dead_letter", "Dead letter"
    ALERT = "alert", "Alert"
    CACHE_INVALIDATION = "cache_invalidation", "Cache invalidation"
    CANCELLED = "cancelled", "Cancelled"
    OPERATOR_ACTION = "operator_action", "Operator action"


class AlertSeverity(models.TextChoices):
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"


class AlertMode(models.TextChoices):
    LOG_ONLY = "log_only", "Log only"
    WEB = "web", "Web admin"


def default_alert_mode() -> str:
    """New jobs inherit the deployment-wide ALERT_MODE; a per-job value overrides it."""
    mode = getattr(settings, "ALERT_MODE", AlertMode.WEB)
    return mode if mode in AlertMode.values else AlertMode.WEB


class Job(models.Model):
    name = models.CharField(max_length=160, unique=True)
    description = models.TextField(blank=True)
    registered_task_name = models.CharField(max_length=160)
    schedule_type = models.CharField(max_length=24, choices=ScheduleType.choices)
    schedule_value = models.JSONField(default=dict)
    timezone = models.CharField(max_length=64, default=getattr(settings, "APP_TIMEZONE", "UTC"))
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    overlap_policy = models.CharField(
        max_length=16,
        choices=OverlapPolicy.choices,
        default=OverlapPolicy.SKIP,
    )
    misfire_policy = models.CharField(
        max_length=16,
        choices=MisfirePolicy.choices,
        default=MisfirePolicy.COALESCE,
    )
    misfire_grace_seconds = models.PositiveIntegerField(default=60)
    max_attempts = models.PositiveIntegerField(default=getattr(settings, "DEFAULT_MAX_ATTEMPTS", 3))
    retry_backoff_seconds = models.PositiveIntegerField(
        default=getattr(settings, "DEFAULT_RETRY_BACKOFF_SECONDS", 10)
    )
    timeout_seconds = models.PositiveIntegerField(
        default=getattr(settings, "DEFAULT_TIMEOUT_SECONDS", 30)
    )
    retention_count = models.PositiveIntegerField(default=getattr(settings, "RETENTION_COUNT", 500))
    retention_days = models.PositiveIntegerField(default=getattr(settings, "RETENTION_DAYS", 30))
    alert_mode = models.CharField(
        max_length=16,
        choices=AlertMode.choices,
        default=default_alert_mode,
        help_text="log_only emits structured log/event records; web also stores an operator-visible Alert row.",
    )
    task_config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["enabled", "next_run_at"], name="job_enabled_next_run_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(max_attempts__gt=0), name="job_max_attempts_positive"),
            models.CheckConstraint(condition=Q(timeout_seconds__gt=0), name="job_timeout_positive"),
            models.CheckConstraint(
                condition=Q(retry_backoff_seconds__gte=0),
                name="job_retry_backoff_nonnegative",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class JobExecution(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="executions")
    scheduled_for = models.DateTimeField(null=True, blank=True)
    run_after = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=24,
        choices=ExecutionStatus.choices,
        default=ExecutionStatus.PENDING,
        db_index=True,
    )
    attempt_number = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=220, unique=True)
    is_manual = models.BooleanField(default=False)
    claimed_by = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    worker_id = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "run_after"], name="execution_status_run_idx"),
            models.Index(fields=["job", "scheduled_for"], name="execution_job_sched_idx"),
            models.Index(fields=["lease_expires_at"], name="execution_lease_idx"),
            models.Index(fields=["job", "created_at"], name="execution_job_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "scheduled_for"],
                condition=Q(scheduled_for__isnull=False, is_manual=False),
                name="unique_scheduled_occurrence",
            ),
            models.CheckConstraint(
                condition=Q(attempt_number__gt=0),
                name="execution_attempt_positive",
            ),
        ]
        ordering = ["-created_at"]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_EXECUTION_STATUSES

    def __str__(self) -> str:
        return f"{self.job.name} #{self.pk or 'new'} {self.status}"


class JobEvent(models.Model):
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    execution = models.ForeignKey(
        JobExecution,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    message = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["event_type", "created_at"], name="event_type_created_idx"),
            models.Index(fields=["job", "created_at"], name="event_job_created_idx"),
            models.Index(fields=["execution", "created_at"], name="event_exec_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} at {self.created_at}"


class DeadLetter(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="dead_letters")
    execution = models.OneToOneField(
        JobExecution,
        on_delete=models.CASCADE,
        related_name="dead_letter",
    )
    reason = models.CharField(max_length=160)
    final_error = models.TextField(blank=True)
    attempts_used = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.job.name}: {self.reason}"


class Alert(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts")
    execution = models.ForeignKey(
        JobExecution,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alerts",
    )
    severity = models.CharField(
        max_length=16,
        choices=AlertSeverity.choices,
        default=AlertSeverity.WARNING,
    )
    message = models.TextField()
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["resolved", "created_at"], name="alert_resolved_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.message[:80]


class SchedulerHeartbeat(models.Model):
    scheduler_id = models.CharField(max_length=160, unique=True)
    hostname = models.CharField(max_length=160)
    process_id = models.PositiveIntegerField()
    last_tick_at = models.DateTimeField()
    recent_occurrences_created = models.PositiveIntegerField(
        default=0,
        help_text="Occurrences created on the most recent scheduler tick (not worker claims).",
    )
    recent_failure_count = models.PositiveIntegerField(default=0)
    health_state = models.CharField(max_length=32, default="starting")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduler_id"]

    def __str__(self) -> str:
        return f"{self.scheduler_id}: {self.health_state}"


class WorkerHeartbeat(models.Model):
    worker_id = models.CharField(max_length=160, unique=True)
    hostname = models.CharField(max_length=160)
    process_id = models.PositiveIntegerField()
    last_heartbeat_at = models.DateTimeField()
    active_execution_count = models.PositiveIntegerField(default=0)
    completed_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    current_execution = models.ForeignKey(
        JobExecution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="worker_heartbeats",
    )
    health_state = models.CharField(max_length=32, default="starting")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["worker_id"]

    def __str__(self) -> str:
        return f"{self.worker_id}: {self.health_state}"
