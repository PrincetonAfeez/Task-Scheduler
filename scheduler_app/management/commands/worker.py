""" Run worker operations. """

from __future__ import annotations

from django.conf import settings
from django.core.management.base import CommandError

from ._base import SchedulerCommand

from scheduler_app.services.dispatcher import dispatch_once
from scheduler_app.services.shutdown import install_shutdown_handlers
from scheduler_app.services.worker import default_worker_id, run_worker_loop


class Command(SchedulerCommand):
    help = "Run worker operations."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        run = subparsers.add_parser("run", help="Run worker process or worker pool.")
        run.add_argument("--workers", type=int, default=settings.WORKER_COUNT)
        run.add_argument("--worker-id", default=None)
        run.add_argument("--once", action="store_true", help="Claim and execute one batch.")

    def handle(self, *args, **options):
        if options["action"] != "run":
            raise CommandError("unknown worker action")
        worker_id = options["worker_id"] or default_worker_id()
        workers = max(options["workers"], 1)
        if options["once"]:
            result = dispatch_once(worker_id=worker_id, limit=workers)
            self.stdout.write(
                self.style.SUCCESS(f"claimed={result.claimed} completed={result.completed}")
            )
            return
        install_shutdown_handlers()
        completed = run_worker_loop(worker_id=worker_id, workers=workers)
        self.stdout.write(self.style.SUCCESS(f"completed={completed}"))

