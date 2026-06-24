""" Run scheduler behavior demonstrations. """

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.core.management.base import CommandError

from ._base import SchedulerCommand
from django.db import connections
from django.utils import timezone

from scheduler_app.models import ExecutionStatus, Job, JobExecution, MisfirePolicy, OverlapPolicy, ScheduleType
from scheduler_app.services.clock import FrozenClock
from scheduler_app.services.dispatcher import dispatch_once
from scheduler_app.services.due import SchedulerService, create_manual_execution
from scheduler_app.services.executors import SubprocessExecutor


class Command(SchedulerCommand):
    help = "Run scheduler behavior demonstrations."

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)
        subparsers.add_parser("single-fire", help="Run two scheduler instances against one due job.")
        subparsers.add_parser("misfire", help="Demonstrate misfire handling.")
        subparsers.add_parser("timeout", help="Demonstrate hard timeout behavior.")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "single-fire":
            self._single_fire()
        elif action == "misfire":
            self._misfire()
        elif action == "timeout":
            self._timeout()
        else:
            raise CommandError("unknown demo action")

    def _single_fire(self) -> None:
        from django.db import connection

        if connection.vendor != "postgresql":
            raise CommandError(
                "demo single-fire requires PostgreSQL. SQLite :memory: gives each "
                "thread a private database, so the row-locking demo cannot run. Set "
                "DATABASE_URL to a PostgreSQL instance and retry."
            )
        now = timezone.now()
        job = Job.objects.create(
            name=f"demo-single-fire-{now.timestamp()}",
            registered_task_name="always_succeed",
            schedule_type=ScheduleType.INTERVAL,
            schedule_value={"every": "30s", "start_at": now.isoformat()},
            timezone="UTC",
            overlap_policy=OverlapPolicy.ALLOW,
            next_run_at=now,
        )
        clock = FrozenClock(now)

        def run_tick(name: str):
            connections.close_all()
            return SchedulerService(clock=clock, scheduler_id=name).tick()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_tick, ["demo-scheduler-a", "demo-scheduler-b"]))

        count = JobExecution.objects.filter(job=job, scheduled_for=now, is_manual=False).count()
        self.stdout.write(f"scheduler_results={results}")
        self.stdout.write(self.style.SUCCESS(f"scheduled occurrence rows={count}"))
        self.stdout.write(
            "Guarantee: at-most-once durable claim/occurrence, not exactly-once external side effects."
        )

    def _misfire(self) -> None:
        now = timezone.now()
        for policy in [MisfirePolicy.COALESCE, MisfirePolicy.CATCH_UP, MisfirePolicy.SKIP]:
            job = Job.objects.create(
                name=f"demo-misfire-{policy}-{now.timestamp()}",
                registered_task_name="always_succeed",
                schedule_type=ScheduleType.INTERVAL,
                schedule_value={"every": "60s", "start_at": (now - timedelta(minutes=5)).isoformat()},
                timezone="UTC",
                misfire_policy=policy,
                misfire_grace_seconds=30,
                next_run_at=now - timedelta(minutes=5),
            )
            SchedulerService(clock=FrozenClock(now), scheduler_id=f"demo-misfire-{policy}").tick()
            statuses = list(job.executions.order_by("scheduled_for").values_list("status", flat=True))
            self.stdout.write(f"{policy}: {statuses}")

    def _timeout(self) -> None:
        now = timezone.now()
        job = Job.objects.create(
            name=f"demo-timeout-{now.timestamp()}",
            registered_task_name="sleep_for_seconds",
            task_config={"seconds": 5},
            schedule_type=ScheduleType.ONE_TIME,
            schedule_value={"run_at": now.isoformat()},
            timezone="UTC",
            timeout_seconds=1,
            max_attempts=1,
            next_run_at=now,
        )
        execution = create_manual_execution(job, now=now, requested_by="demo")
        result = dispatch_once(
            worker_id="demo-timeout-worker",
            limit=1,
            clock=FrozenClock(now),
            executor=SubprocessExecutor(),
        )
        execution.refresh_from_db()
        has_dead_letter = hasattr(execution, "dead_letter")
        self.stdout.write(f"dispatch={result}")
        self.stdout.write(
            self.style.SUCCESS(
                f"execution={execution.id} status={execution.status} "
                f"expected={ExecutionStatus.TIMED_OUT} dead_letter_recorded={has_dead_letter}"
            )
        )

