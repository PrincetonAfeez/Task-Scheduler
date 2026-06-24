""" Base command for scheduler commands """

from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from scheduler_app import __version__


class SchedulerCommand(BaseCommand):
    """Base command that reports the project version (not Django's) via --version."""

    def get_version(self) -> str:
        return __version__

    def add_cli_secret_argument(self, parser) -> None:
        parser.add_argument(
            "--cli-secret",
            default=os.getenv("SCHEDULER_CLI_SECRET", ""),
            help="Required when SCHEDULER_CLI_SECRET is set in the environment.",
        )

    def require_cli_secret(self, options: dict) -> None:
        expected = getattr(settings, "SCHEDULER_CLI_SECRET", "") or os.getenv("SCHEDULER_CLI_SECRET", "")
        if not expected:
            return
        provided = options.get("cli_secret") or ""
        if provided != expected:
            raise CommandError("pass --cli-secret matching SCHEDULER_CLI_SECRET for this operation")
