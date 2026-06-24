""" Prune old run history according to each job retention policy. """

from __future__ import annotations

from ._base import SchedulerCommand

from scheduler_app.services.retention import prune_all_jobs


class Command(SchedulerCommand):
    help = "Prune old run history according to each job retention policy."

    def handle(self, *args, **options):
        deleted = prune_all_jobs()
        self.stdout.write(self.style.SUCCESS(f"pruned {deleted} execution rows"))

