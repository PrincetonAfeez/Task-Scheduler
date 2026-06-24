""" Checks for the scheduler app. """

from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register


@register()
def webui_auth_disabled_check(app_configs, **kwargs):
    if getattr(settings, "WEBUI_AUTH_ENABLED", True):
        return []
    return [
        Warning(
            "WEBUI_AUTH is disabled; mutating web actions are open to anonymous users.",
            hint="Set WEBUI_AUTH=1 in production.",
            id="scheduler_app.W001",
        )
    ]


@register()
def scheduler_cli_secret_missing_check(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    if getattr(settings, "SCHEDULER_CLI_SECRET", ""):
        return []
    return [
        Warning(
            "SCHEDULER_CLI_SECRET is empty; CLI destructive commands accept any caller.",
            hint="Set SCHEDULER_CLI_SECRET to a long random value in production.",
            id="scheduler_app.W002",
        )
    ]
