"""Test settings that keep PostgreSQL as the database

Use this to exercise the single-fire / row-locking concurrency tests against a
real PostgreSQL instance (SELECT ... FOR UPDATE SKIP LOCKED is a no-op on SQLite):

    # with DATABASE_URL pointing at a PostgreSQL test database
    pytest -m postgresql --ds=task_scheduler.test_settings_postgres

The database (and cache, for speed) come from the base settings via DATABASE_URL;
only password hashing and email/cache backends are swapped for fast, isolated tests.
"""

from .settings import *  # noqa: F401,F403

# DATABASES is inherited from settings.py (PostgreSQL via DATABASE_URL). We do NOT
# override it to SQLite here -- that is the whole point of this module.

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "task-scheduler-tests",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
WEBUI_AUTH_ENABLED = True
