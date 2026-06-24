""" Create a development superuser when DEV_ADMIN credentials are set. """

from __future__ import annotations

import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a development superuser when DEV_ADMIN credentials are set."

    def handle(self, *args, **options):
        username = os.getenv("DEV_ADMIN_USERNAME", "admin")
        password = os.getenv("DEV_ADMIN_PASSWORD", "admin")
        email = os.getenv("DEV_ADMIN_EMAIL", "admin@example.com")
        using_default_password = not os.getenv("DEV_ADMIN_PASSWORD") and password == "admin"

        if using_default_password and not settings.DEBUG:
            raise CommandError(
                "Refusing to create a dev user with default password while DEBUG=0. "
                "Set DEV_ADMIN_PASSWORD in the environment."
            )

        if using_default_password:
            self.stderr.write(
                self.style.WARNING(
                    "Creating demo superuser with default credentials admin/admin. "
                    "Override DEV_ADMIN_USERNAME and DEV_ADMIN_PASSWORD for non-local use."
                )
            )

        user_model = get_user_model()
        if user_model.objects.filter(username=username).exists():
            self.stdout.write(self.style.SUCCESS(f"dev user {username!r} already exists"))
            return
        user_model.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"created dev superuser {username!r}"))
