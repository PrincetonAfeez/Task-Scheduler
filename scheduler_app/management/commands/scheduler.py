""" Run scheduler operations. """

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import CommandError

from ._base import SchedulerCommand

from scheduler_app.services.clock import SystemClock
from scheduler_app.services.due import SchedulerService
from scheduler_app.services.health import prune_stale_scheduler_heartbeats, prune_stale_worker_heartbeats
from scheduler_app.services.leases import recover_expired_leases
from scheduler_app.services.retention import prune_all_jobs
from scheduler_app.services.shutdown import install_shutdown_handlers, interruptible_sleep, shutdown_requested


class Command(SchedulerCommand):
    help = "Run scheduler operations."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        run = subparsers.add_parser("run", help="Run the scheduler tick loop.")
        run.add_argument("--once", action="store_true", help="Run one tick and exit.")
        run.add_argument("--tick-seconds", type=float, default=settings.SCHEDULER_TICK_SECONDS)
        run.add_argument("--scheduler-id", default=None)

    def handle(self, *args, **options):
        if options["action"] != "run":
            raise CommandError("unknown scheduler action")
        clock = SystemClock()
        service = SchedulerService(clock=clock, scheduler_id=options["scheduler_id"])
        tick_seconds = options["tick_seconds"]
        prune_every = getattr(settings, "PRUNE_HISTORY_EVERY_N_TICKS", 0)
        tick_counter = 0
        install_shutdown_handlers()
        # Monotonic deadline so the cadence does not drift by each tick's own duration.
        next_deadline = time.monotonic()
        while True:
            now = clock.now()
            recovered = recover_expired_leases(now=now)
            pruned_workers = prune_stale_worker_heartbeats(now=now)
            pruned_schedulers = prune_stale_scheduler_heartbeats(now=now)
            result = service.tick()
            pruned_history = 0
            tick_counter += 1
            if prune_every > 0 and tick_counter % prune_every == 0:
                pruned_history = prune_all_jobs()
            self.stdout.write(
                self.style.SUCCESS(
                    "tick "
                    f"due_jobs={result.due_jobs} created={result.created} "
                    f"missed={result.missed} duplicates={result.duplicates} "
                    f"recovered={recovered} pruned_workers={pruned_workers} "
                    f"pruned_schedulers={pruned_schedulers} "
                    f"pruned_history={pruned_history}"
                )
            )
            if options["once"]:
                return
            if shutdown_requested():
                self.stdout.write(self.style.WARNING("shutdown requested; exiting scheduler loop"))
                return
            next_deadline += tick_seconds
            if interruptible_sleep(max(0.0, next_deadline - time.monotonic())):
                self.stdout.write(self.style.WARNING("shutdown requested; exiting scheduler loop"))
                return
