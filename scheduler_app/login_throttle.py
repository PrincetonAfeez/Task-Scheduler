""" Login throttle for the scheduler app. """

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


def _cache_key(*, username: str, ip: str) -> str:
    return f"login-fail:{ip}:{username.lower()}"


def is_login_blocked(*, username: str, ip: str) -> bool:
    limit = getattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 10)
    if limit <= 0:
        return False
    return int(cache.get(_cache_key(username=username, ip=ip), 0)) >= limit


def record_login_failure(*, username: str, ip: str) -> None:
    limit = getattr(settings, "LOGIN_RATE_LIMIT_ATTEMPTS", 10)
    window = getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300)
    if limit <= 0:
        return
    key = _cache_key(username=username, ip=ip)
    if not cache.add(key, 1, timeout=window):
        try:
            new_count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window)
        else:
            try:
                cache.touch(key, window)
            except Exception:
                cache.set(key, new_count, timeout=window)


def clear_login_failures(*, username: str, ip: str) -> None:
    cache.delete(_cache_key(username=username, ip=ip))
