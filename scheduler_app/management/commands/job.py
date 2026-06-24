""" Create and operate scheduled jobs. """

from __future__ import annotations

import json

from django.core.management.base import CommandError
from django.db import IntegrityError

from ._base import SchedulerCommand
from django.utils import timezone

from scheduler_app.models import (
    AlertMode,
    Job,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleType,
)
from scheduler_app.services.cache import invalidate_scheduler_cache
from scheduler_app.services.claiming import cancel_queued_executions_for_job
from scheduler_app.services.due import create_manual_execution
from scheduler_app.services.job_schedule import (
    apply_next_run_after_edit,
    schedule_fields_changed,
    validate_schedule_for_job,
)
from scheduler_app.services.operator_audit import emit_cli_operator_action
from scheduler_app.services.schedules import initial_next_run, upcoming_runs_for_job
from scheduler_app.services.task_config import validate_task_config
from scheduler_app.tasks.registry import registered_tasks


def _get_job_or_error(job_id: int) -> Job:
    try:
        return Job.objects.get(pk=job_id)
    except Job.DoesNotExist as exc:
        raise CommandError(f"job {job_id} not found") from exc


def _safe_initial_next_run(schedule_type, schedule_value, *, now, timezone_name):
    try:
        return initial_next_run(schedule_type, schedule_value, now=now, timezone_name=timezone_name)
    except (ValueError, KeyError) as exc:
        raise CommandError(f"invalid schedule: {exc}") from exc


def _json(value: str | None, default: dict | None = None) -> dict:
    if not value:
        return default or {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CommandError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CommandError("JSON value must be an object")
    return parsed


def _schedule_value(options: dict) -> dict:
    if options.get("schedule_value"):
        return _json(options["schedule_value"])
    schedule_type = options["schedule_type"]
    if schedule_type == ScheduleType.ONE_TIME:
        if not options.get("run_at"):
            raise CommandError("one_time jobs require --run-at or --schedule-value")
        return {"run_at": options["run_at"]}
    if schedule_type == ScheduleType.INTERVAL:
        if not options.get("every"):
            raise CommandError("interval jobs require --every or --schedule-value")
        return {"every": options["every"]}
    if schedule_type == ScheduleType.CRON:
        if not options.get("cron"):
            raise CommandError("cron jobs require --cron or --schedule-value")
        return {"expression": options["cron"]}
    raise CommandError(f"unknown schedule type {schedule_type}")


class Command(SchedulerCommand):
    help = "Create and operate scheduled jobs."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        add = subparsers.add_parser("add", help="Create a job.")
        self.add_cli_secret_argument(add)
        add.add_argument("name")
        add.add_argument("task")
        add.add_argument("schedule_type", choices=[choice.value for choice in ScheduleType])
        add.add_argument("--schedule-value", help='JSON object, for example {"every": "30s"}.')
        add.add_argument("--run-at", help="ISO datetime for one_time schedules.")
        add.add_argument("--every", help="Interval duration such as 30s, 5m, 2h, 1d.")
        add.add_argument("--cron", help='Cron expression, for example "0 9 * * 1-5".')
        add.add_argument("--timezone", default="UTC")
        add.add_argument("--description", default="")
        add.add_argument("--config", default="{}")
        add.add_argument("--overlap-policy", choices=[choice.value for choice in OverlapPolicy], default=OverlapPolicy.SKIP)
        add.add_argument("--misfire-policy", choices=[choice.value for choice in MisfirePolicy], default=MisfirePolicy.COALESCE)
        add.add_argument("--misfire-grace-seconds", type=int, default=60)
        add.add_argument("--max-attempts", type=int, default=3)
        add.add_argument("--retry-backoff-seconds", type=int, default=10)
        add.add_argument("--timeout-seconds", type=int, default=30)
        add.add_argument(
            "--alert-mode",
            choices=[choice.value for choice in AlertMode],
            default=AlertMode.WEB,
        )

        edit = subparsers.add_parser("edit", help="Edit a job.")
        self.add_cli_secret_argument(edit)
        edit.add_argument("job_id", type=int)
        edit.add_argument("--name")
        edit.add_argument("--task")
        edit.add_argument("--schedule-type", choices=[choice.value for choice in ScheduleType])
        edit.add_argument("--schedule-value")
        edit.add_argument("--timezone")
        edit.add_argument("--config")
        edit.add_argument("--enabled", action="store_true")
        edit.add_argument("--disabled", action="store_true")

        for action in ["enable", "disable", "delete", "preview", "trigger"]:
            sub = subparsers.add_parser(action, help=f"{action} a job.")
            sub.add_argument("job_id", type=int)
            if action in {"delete", "trigger", "disable", "enable"}:
                self.add_cli_secret_argument(sub)
            if action == "delete":
                sub.add_argument("--yes", action="store_true")

        subparsers.add_parser("list", help="List jobs.")
        subparsers.add_parser("catalog", help="List registered tasks.")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "add":
            self.require_cli_secret(options)
            self._add(options)
        elif action == "edit":
            self.require_cli_secret(options)
            self._edit(options)
        elif action == "list":
            self._list()
        elif action == "catalog":
            self._catalog()
        elif action == "enable":
            self.require_cli_secret(options)
            self._set_enabled(options["job_id"], True)
        elif action == "disable":
            self.require_cli_secret(options)
            self._set_enabled(options["job_id"], False)
        elif action == "delete":
            self.require_cli_secret(options)
            self._delete(options)
        elif action == "preview":
            self._preview(options["job_id"])
        elif action == "trigger":
            self.require_cli_secret(options)
            self._trigger(options)
        else:
            raise CommandError("unknown job action")

    def _add(self, options: dict) -> None:
        if options["task"] not in registered_tasks():
            raise CommandError(f"unknown registered task: {options['task']}")
        now = timezone.now()
        schedule_value = _schedule_value(options)
        try:
            task_config = validate_task_config(options["task"], _json(options["config"]))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        next_run_at = _safe_initial_next_run(
            options["schedule_type"],
            schedule_value,
            now=now,
            timezone_name=options["timezone"],
        )
        try:
            job = Job.objects.create(
                name=options["name"],
                description=options["description"],
                registered_task_name=options["task"],
                schedule_type=options["schedule_type"],
                schedule_value=schedule_value,
                timezone=options["timezone"],
                task_config=task_config,
                overlap_policy=options["overlap_policy"],
                misfire_policy=options["misfire_policy"],
                misfire_grace_seconds=options["misfire_grace_seconds"],
                max_attempts=options["max_attempts"],
                retry_backoff_seconds=options["retry_backoff_seconds"],
                timeout_seconds=options["timeout_seconds"],
                alert_mode=options["alert_mode"],
                next_run_at=next_run_at,
            )
        except IntegrityError as exc:
            raise CommandError(f"job name {options['name']!r} already exists") from exc
        invalidate_scheduler_cache("job created", job=job)
        emit_cli_operator_action(action="job_create", message=f"Created job {job.name}", job=job)
        self.stdout.write(self.style.SUCCESS(f"created job {job.id}: {job.name} next={job.next_run_at}"))

    def _edit(self, options: dict) -> None:
        job = _get_job_or_error(options["job_id"])
        previous = Job.objects.get(pk=job.pk)
        was_enabled = job.enabled
        changed: set[str] = set()
        if options.get("name"):
            job.name = options["name"]
            changed.add("name")
        if options.get("task"):
            if options["task"] not in registered_tasks():
                raise CommandError(f"unknown registered task: {options['task']}")
            job.registered_task_name = options["task"]
            changed.add("registered_task_name")
        if options.get("schedule_type"):
            job.schedule_type = options["schedule_type"]
            changed.add("schedule_type")
        if options.get("schedule_value"):
            job.schedule_value = _json(options["schedule_value"])
            changed.add("schedule_value")
        if options.get("timezone"):
            job.timezone = options["timezone"]
            changed.add("timezone")
        if options.get("config"):
            try:
                job.task_config = validate_task_config(job.registered_task_name, _json(options["config"]))
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            changed.add("task_config")
        if options["enabled"] and options["disabled"]:
            raise CommandError("choose either --enabled or --disabled")
        if options["enabled"]:
            job.enabled = True
            changed.add("enabled")
        if options["disabled"]:
            job.enabled = False
            changed.add("enabled")
        if "registered_task_name" in changed and "task_config" not in changed:
            try:
                job.task_config = validate_task_config(job.registered_task_name, job.task_config)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        now = timezone.now()
        if schedule_fields_changed(changed):
            try:
                validate_schedule_for_job(job, now=now)
            except ValueError as exc:
                raise CommandError(f"invalid schedule: {exc}") from exc
        re_enabled = job.enabled and not was_enabled and "enabled" in changed
        try:
            apply_next_run_after_edit(
                job,
                now=now,
                schedule_changed=schedule_fields_changed(changed),
                re_enabled=re_enabled,
                previous=previous,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        try:
            job.save()
        except IntegrityError as exc:
            raise CommandError(f"job name {job.name!r} already exists") from exc
        cancelled = 0
        if was_enabled and not job.enabled:
            cancelled = cancel_queued_executions_for_job(job, reason="job disabled")
        invalidate_scheduler_cache("job edited", job=job)
        emit_cli_operator_action(
            action="job_edit",
            message=f"Updated job {job.name}",
            job=job,
            data={"cancelled": cancelled} if cancelled else None,
        )
        message = f"updated job {job.id}: next={job.next_run_at}"
        if cancelled:
            message += f" (cancelled {cancelled} queued execution(s))"
        self.stdout.write(self.style.SUCCESS(message))

    def _list(self) -> None:
        for job in Job.objects.order_by("next_run_at", "name"):
            self.stdout.write(
                f"{job.id}\t{job.enabled}\t{job.name}\t{job.registered_task_name}\t"
                f"last={job.last_run_at or '-'}\tnext={job.next_run_at or '-'}"
            )

    def _catalog(self) -> None:
        for name, spec in sorted(registered_tasks().items()):
            self.stdout.write(
                f"{name}\tidempotent={spec.idempotent}\tdemo={spec.safe_for_demo}\t{spec.description}"
            )

    def _set_enabled(self, job_id: int, enabled: bool) -> None:
        job = _get_job_or_error(job_id)
        previous = Job.objects.get(pk=job.pk)
        now = timezone.now()
        cancelled = 0
        if enabled:
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
                raise CommandError(str(exc)) from exc
            job.save(update_fields=["enabled", "next_run_at", "updated_at"])
        else:
            job.enabled = False
            job.save(update_fields=["enabled", "updated_at"])
            cancelled = cancel_queued_executions_for_job(job, reason="job disabled")
        invalidate_scheduler_cache("job enabled" if enabled else "job disabled", job=job)
        emit_cli_operator_action(
            action="job_enable" if enabled else "job_disable",
            message=f"{'Enabled' if enabled else 'Disabled'} {job.name}",
            job=job,
            data={"cancelled": cancelled} if cancelled else None,
        )
        message = f"{'enabled' if enabled else 'disabled'} job {job.id}"
        if cancelled:
            message += f" (cancelled {cancelled} queued execution(s))"
        self.stdout.write(self.style.SUCCESS(message))

    def _delete(self, options: dict) -> None:
        if not options["yes"]:
            raise CommandError("pass --yes to delete a job")
        job = _get_job_or_error(options["job_id"])
        name = job.name
        emit_cli_operator_action(action="job_delete", message=f"Deleted job {name}", job=job)
        invalidate_scheduler_cache("job deleted", job=job)
        job.delete()
        self.stdout.write(self.style.SUCCESS(f"deleted job {name}"))

    def _preview(self, job_id: int) -> None:
        job = _get_job_or_error(job_id)
        for fire_time in upcoming_runs_for_job(job, count=10, now=timezone.now()):
            self.stdout.write(fire_time.isoformat())

    def _trigger(self, options: dict) -> None:
        job = _get_job_or_error(options["job_id"])
        if not job.enabled:
            raise CommandError(f"job {job.id} is disabled; enable it before triggering")
        execution = create_manual_execution(job, now=timezone.now(), requested_by="cli")
        emit_cli_operator_action(
            action="job_trigger",
            message=f"Manual trigger for {job.name} created execution {execution.id}",
            job=job,
            execution=execution,
        )
        self.stdout.write(self.style.SUCCESS(f"created execution {execution.id}"))

