""" Show alert and dead-letter records. """

from __future__ import annotations

from django.core.management.base import CommandError

from ._base import SchedulerCommand

from scheduler_app.models import Alert, DeadLetter
from scheduler_app.services.operator_audit import emit_cli_operator_action


class Command(SchedulerCommand):
    help = "Show alert and dead-letter records."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        subparsers.add_parser("list", help="List alerts and dead letters.")
        resolve = subparsers.add_parser("resolve", help="Mark an alert resolved.")
        resolve.add_argument("alert_id", type=int)
        self.add_cli_secret_argument(resolve)

    def handle(self, *args, **options):
        action = options["action"]
        if action == "list":
            self._list()
        elif action == "resolve":
            self.require_cli_secret(options)
            self._resolve(options["alert_id"])
        else:
            raise CommandError("unknown alerts action")

    def _list(self) -> None:
        self.stdout.write("Alerts")
        for alert in Alert.objects.select_related("job", "execution").order_by("-created_at")[:100]:
            self.stdout.write(
                f"{alert.id}\t{alert.created_at}\t{alert.severity}\t"
                f"resolved={alert.resolved}\t"
                f"job={alert.job_id or '-'}\texecution={alert.execution_id or '-'}\t{alert.message}"
            )
        self.stdout.write("\nDead letters")
        for item in DeadLetter.objects.select_related("job", "execution").order_by("-created_at")[:100]:
            self.stdout.write(
                f"{item.created_at}\tjob={item.job.name}\texecution={item.execution_id}\t"
                f"attempts={item.attempts_used}\t{item.reason}"
            )

    def _resolve(self, alert_id: int) -> None:
        try:
            alert = Alert.objects.get(pk=alert_id)
        except Alert.DoesNotExist as exc:
            raise CommandError(f"alert {alert_id} not found") from exc
        if alert.resolved:
            self.stdout.write(self.style.WARNING(f"alert {alert_id} is already resolved"))
            return
        alert.resolved = True
        alert.save(update_fields=["resolved"])
        emit_cli_operator_action(
            action="alert_resolve",
            message=f"Resolved alert {alert.id}",
            job=alert.job,
            execution=alert.execution,
            data={"alert_id": alert.id},
        )
        self.stdout.write(self.style.SUCCESS(f"resolved alert {alert_id}"))
