""" Auth for the scheduler app. """

from __future__ import annotations

from functools import wraps
from typing import Callable, TypeVar

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.urls import reverse

F = TypeVar("F", bound=Callable[..., HttpResponse])


def webui_login_required(view_func: F) -> F:
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if getattr(settings, "WEBUI_AUTH_ENABLED", True) and not request.user.is_authenticated:
            if request.headers.get("HX-Request"):
                login_url = reverse("login")
                return HttpResponse(
                    f'<p class="muted">Sign in to view live operational data. '
                    f'<a href="{login_url}">Sign in</a></p>',
                    status=401,
                )
            return redirect_to_login(request.get_full_path())
        return view_func(request, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def webui_public_read_or_login_required(view_func: F) -> F:
    """When WEBUI_PUBLIC_READ is off, require sign-in for otherwise public list/detail pages."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        auth_on = getattr(settings, "WEBUI_AUTH_ENABLED", True)
        public_read = getattr(settings, "WEBUI_PUBLIC_READ", True)
        if auth_on and not public_read and not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view_func(request, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
