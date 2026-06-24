""" Operator audit operations for the scheduler app. """

from __future__ import annotations

from typing import Any

from django.contrib.auth.models import AbstractBaseUser, AnonymousUser

from scheduler_app.models import EventType, Job, JobExecution

from .events import emit_event


def emit_operator_action(
    *,
    action: str,
    message: str,
    job: Job | None = None,
    execution: JobExecution | None = None,
    user: AbstractBaseUser | AnonymousUser | None = None,
    source: str = "web",
    actor: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"action": action, "source": source}
    if user is not None and getattr(user, "is_authenticated", False):
        payload["username"] = user.get_username()
    elif actor:
        payload["username"] = actor
    if data:
        payload.update(data)
    emit_event(
        EventType.OPERATOR_ACTION,
        job=job,
        execution=execution,
        message=message,
        data=payload,
    )


def emit_cli_operator_action(
    *,
    action: str,
    message: str,
    job: Job | None = None,
    execution: JobExecution | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    emit_operator_action(
        action=action,
        message=message,
        job=job,
        execution=execution,
        source="cli",
        actor="cli",
        data=data,
    )
