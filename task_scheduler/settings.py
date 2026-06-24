""" Settings for the scheduler app. """

from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-task-scheduler-secret")
# DEBUG defaults off when a SECRET_KEY is explicitly provided (production-like),
# and on for local development. Override with DEBUG=1/0 in any environment.
DEBUG = os.getenv("DEBUG", "0" if "SECRET_KEY" in os.environ else "1") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "*").split(",") if host]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "scheduler_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "task_scheduler.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "task_scheduler.wsgi.application"
ASGI_APPLICATION = "task_scheduler.asgi.application"

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgres://scheduler:scheduler@localhost:5432/task_scheduler",
)

if os.getenv("USE_SQLITE", "0") == "1":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        # dj_database_url returns a TypedDict that django-stubs does not recognise
        # as a DATABASES entry; the value is correct at runtime.
        "default": dj_database_url.parse(  # type: ignore[dict-item]
            DATABASE_URL,
            conn_max_age=60,
            conn_health_checks=True,
        )
    }

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL and os.getenv("USE_LOCMEM_CACHE", "0") != "1":
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "IGNORE_EXCEPTIONS": True,
            },
            "KEY_PREFIX": "task_scheduler",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "task-scheduler-dev",
        }
    }

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "UTC")
SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "5"))
WORKER_COUNT = int(os.getenv("WORKER_COUNT", "2"))
EXECUTOR_BACKEND = os.getenv("EXECUTOR_BACKEND", "subprocess")
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "30"))
DEFAULT_MAX_ATTEMPTS = int(os.getenv("DEFAULT_MAX_ATTEMPTS", "3"))
DEFAULT_RETRY_BACKOFF_SECONDS = int(os.getenv("DEFAULT_RETRY_BACKOFF_SECONDS", "10"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
RETENTION_COUNT = int(os.getenv("RETENTION_COUNT", "500"))
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "30"))
ALERT_MODE = os.getenv("ALERT_MODE", "web")
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "120"))
LEASE_BUFFER_SECONDS = int(os.getenv("LEASE_BUFFER_SECONDS", "30"))
MISFIRE_CATCH_UP_CAP = int(os.getenv("MISFIRE_CATCH_UP_CAP", "50"))
PRUNE_HISTORY_EVERY_N_TICKS = int(os.getenv("PRUNE_HISTORY_EVERY_N_TICKS", "0"))

WEBUI_AUTH_ENABLED = os.getenv("WEBUI_AUTH", "1") == "1"
WEBUI_PUBLIC_READ = os.getenv("WEBUI_PUBLIC_READ", "1") == "1"
SCHEDULER_CLI_SECRET = os.getenv("SCHEDULER_CLI_SECRET", "")
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"

HEARTBEAT_PRUNE_SECONDS = int(os.getenv("HEARTBEAT_PRUNE_SECONDS", "86400"))
READYZ_REQUIRE_HEARTBEATS = os.getenv("READYZ_REQUIRE_HEARTBEATS", "0") == "1"
READYZ_REQUIRE_WORKER_HEARTBEAT = os.getenv("READYZ_REQUIRE_WORKER_HEARTBEAT", "0") == "1"
READYZ_HEARTBEAT_MAX_AGE_SECONDS = int(os.getenv("READYZ_HEARTBEAT_MAX_AGE_SECONDS", "300"))
LOGIN_RATE_LIMIT_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_ATTEMPTS", "10"))
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))

_session_age = os.getenv("SESSION_COOKIE_AGE", "")
if _session_age:
    SESSION_COOKIE_AGE = int(_session_age)

_csrf_origins = os.getenv("CSRF_TRUSTED_ORIGINS", "")
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in _csrf_origins.split(",") if origin.strip()]

if os.getenv("USE_X_FORWARDED_PROTO", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if os.getenv("SECURE_COOKIES", "0") == "1":
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "1") == "1"
    _hsts = os.getenv("SECURE_HSTS_SECONDS", "")
    if _hsts:
        SECURE_HSTS_SECONDS = int(_hsts)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "task_scheduler.logging.JsonFormatter"},
        "console": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": os.getenv("LOG_FORMAT", "json"),
        }
    },
    "loggers": {
        "scheduler_app": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO"},
    },
}

