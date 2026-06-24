""" Alert and dead-letter operations. """

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from scheduler_app.models import Alert, AlertMode, AlertSeverity, DeadLetter, EventType, JobExecution

from .events import emit_event


def _alert_mode(execution: JobExecution | None) -> str:
    if execution is not None and execution.job_id is not None:
        return execution.job.alert_mode
    return getattr(settings, "ALERT_MODE", AlertMode.WEB)


@transaction.atomic
def create_alert(
    *,
    execution: JobExecution | None,
    message: str,
    severity: str = AlertSeverity.WARNING,
) -> Alert | None:
    job = execution.job if execution else None
    # An ALERT event is always emitted (structured log + audit trail). The
    # operator-visible Alert row is only stored when the mode is not log-only.
    alert: Alert | None = None
    if _alert_mode(execution) != AlertMode.LOG_ONLY:
        alert = Alert.objects.create(
            job=job,
            execution=execution,
            severity=severity,
            message=message,
        )
    emit_event(
        EventType.ALERT,
        job=job,
        execution=execution,
        message=message,
        data={"severity": severity, "alert_id": alert.id if alert else None},
    )
    return alert


@transaction.atomic
def dead_letter_execution(execution: JobExecution, *, reason: str, final_error: str = "") -> DeadLetter:
    dead_letter, created = DeadLetter.objects.get_or_create(
        execution=execution,
        defaults={
            "job": execution.job,
            "reason": reason,
            "final_error": final_error or execution.error,
            "attempts_used": execution.attempt_number,
        },
    )
    if not created:
        # Already dead-lettered (e.g. retried via lease recovery and re-exhausted);
        # don't raise a duplicate alert/event for the same execution.
        return dead_letter
    create_alert(
        execution=execution,
        message=f"{execution.job.name} dead-lettered: {reason}",
        severity=AlertSeverity.ERROR,
    )
    emit_event(
        EventType.DEAD_LETTER,
        job=execution.job,
        execution=execution,
        message=reason,
        data={"dead_letter_id": dead_letter.id, "attempts_used": execution.attempt_number},
    )
    return dead_letter

