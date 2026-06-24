""" Show scheduler, worker, and queue health. """

from __future__ import annotations

from ._base import SchedulerCommand

from scheduler_app.services.cache import queue_depth
from scheduler_app.services.health import health_snapshot


class Command(SchedulerCommand):
    help = "Show scheduler, worker, and queue health."

    def handle(self, *args, **options):
        snapshot = health_snapshot()
        self.stdout.write("Schedulers")
        for scheduler in snapshot["schedulers"]:
            self.stdout.write(
                f"{scheduler.scheduler_id}\t{scheduler.health_state}\tlast_tick={scheduler.last_tick_at}"
            )
        self.stdout.write("\nWorkers")
        for worker in snapshot["workers"]:
            self.stdout.write(
                f"{worker.worker_id}\t{worker.health_state}\tlast_heartbeat={worker.last_heartbeat_at}"
            )
        self.stdout.write("\nQueue")
        for status, count in queue_depth().items():
            self.stdout.write(f"{status}\t{count}")

