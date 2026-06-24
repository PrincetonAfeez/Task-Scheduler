""" Inspect and operate executions. """

from __future__ import annotations

from django.core.management.base import CommandError

from ._base import SchedulerCommand
from django.utils import timezone

from scheduler_app.models import ExecutionStatus, JobExecution
from scheduler_app.services.claiming import cancel_execution
from scheduler_app.services.operator_audit import emit_cli_operator_action
from scheduler_app.services.retries import retry_execution


def _get_execution_or_error(execution_id: int) -> JobExecution:
    try:
        return JobExecution.objects.select_related("job").get(pk=execution_id)
    except JobExecution.DoesNotExist as exc:
        raise CommandError(f"execution {execution_id} not found") from exc


class Command(SchedulerCommand):
    help = "Inspect and operate executions."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        list_parser = subparsers.add_parser("list", help="Show run history.")
        list_parser.add_argument("--status", choices=[choice.value for choice in ExecutionStatus])
        list_parser.add_argument("--limit", type=int, default=50)

        inspect = subparsers.add_parser("inspect", help="Inspect one execution.")
        inspect.add_argument("execution_id", type=int)

        retry = subparsers.add_parser("retry", help="Retry a failed/timed-out/dead-lettered execution.")
        retry.add_argument("execution_id", type=int)
        self.add_cli_secret_argument(retry)

        cancel = subparsers.add_parser("cancel", help="Cancel a pending/claimed/retry_scheduled execution.")
        cancel.add_argument("execution_id", type=int)
        self.add_cli_secret_argument(cancel)

    def handle(self, *args, **options):
        if options["action"] == "list":
            queryset = JobExecution.objects.select_related("job").order_by("-created_at")
            if options.get("status"):
                queryset = queryset.filter(status=options["status"])
            for execution in queryset[: options["limit"]]:
                self.stdout.write(
                    f"{execution.id}\t{execution.job.name}\t{execution.status}\t"
                    f"attempt={execution.attempt_number}\tscheduled={execution.scheduled_for or '-'}"
                )
        elif options["action"] == "inspect":
            execution = _get_execution_or_error(options["execution_id"])
            self.stdout.write(f"id: {execution.id}")
            self.stdout.write(f"job: {execution.job.name}")
            self.stdout.write(f"status: {execution.status}")
            self.stdout.write(f"scheduled_for: {execution.scheduled_for}")
            self.stdout.write(f"run_after: {execution.run_after}")
            self.stdout.write(f"attempt_number: {execution.attempt_number}")
            self.stdout.write(f"worker_id: {execution.worker_id or '-'}")
            self.stdout.write(f"duration_ms: {execution.duration_ms or '-'}")
            self.stdout.write(f"output:\n{execution.output}")
            self.stdout.write(f"error:\n{execution.error}")
        elif options["action"] == "retry":
            self.require_cli_secret(options)
            execution = _get_execution_or_error(options["execution_id"])
            if execution.status not in [
                ExecutionStatus.FAILED,
                ExecutionStatus.TIMED_OUT,
                ExecutionStatus.DEAD_LETTERED,
                ExecutionStatus.CANCELLED,
            ]:
                raise CommandError(f"execution status {execution.status} is not retryable")
            retry = retry_execution(execution, now=timezone.now())
            emit_cli_operator_action(
                action="execution_retry",
                message=f"Manual retry of execution {execution.id} created {retry.id}",
                job=execution.job,
                execution=retry,
                data={"original_execution_id": execution.id},
            )
            self.stdout.write(self.style.SUCCESS(f"created retry execution {retry.id}"))
        elif options["action"] == "cancel":
            self.require_cli_secret(options)
            execution = _get_execution_or_error(options["execution_id"])
            try:
                cancel_execution(execution)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            emit_cli_operator_action(
                action="execution_cancel",
                message=f"Cancelled execution {execution.id}",
                job=execution.job,
                execution=execution,
            )
            self.stdout.write(self.style.SUCCESS(f"cancelled execution {execution.id}"))
        else:
            raise CommandError("unknown execution action")

