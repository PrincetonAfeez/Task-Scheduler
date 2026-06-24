""" Apps for the scheduler app. """

from django.apps import AppConfig


class SchedulerAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scheduler_app"

    def ready(self) -> None:
        from . import checks  # noqa: F401
