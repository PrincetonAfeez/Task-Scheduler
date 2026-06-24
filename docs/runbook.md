# Runbook

## Start Services

```powershell
docker compose up --build
```

Local mode:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
.\.venv\Scripts\python.exe manage.py scheduler run
.\.venv\Scripts\python.exe manage.py worker run --workers 2
```

## Stop Services

Press `Ctrl+C` for local processes, or:

```powershell
docker compose down
```

## Inspect Health

```powershell
python manage.py health
```

Check `/health/` in the web admin for scheduler heartbeats, worker heartbeats, and queue depth.

Liveness and readiness probes:

- `/healthz` — process is up
- `/readyz` — PostgreSQL (and Redis when configured) are reachable; returns 503 when database migrations are pending; optionally requires fresh scheduler and/or worker heartbeats

## CLI Shared Secret

When `SCHEDULER_CLI_SECRET` is set, pass `--cli-secret <value>` to destructive commands such as `job add`, `job edit`, `job enable`, `job trigger`, `job disable`, `job delete`, `execution retry`, `execution cancel`, and `alerts resolve`.

Each scheduler tick prunes scheduler and worker heartbeat rows older than `HEARTBEAT_PRUNE_SECONDS` (default 86400 / 24h).

When `PRUNE_HISTORY_EVERY_N_TICKS` is greater than zero, the scheduler loop also runs `prune_history` every N ticks (Docker demo default: `720` ≈ one hour at a 5s tick). Set to `0` to disable automatic pruning and run `python manage.py prune_history` manually instead.

Set `READYZ_REQUIRE_HEARTBEATS=1` when the web process should not report ready until a scheduler heartbeat has ticked within `READYZ_HEARTBEAT_MAX_AGE_SECONDS`.

Set `READYZ_REQUIRE_WORKER_HEARTBEAT=1` for the same check against worker heartbeats (uses the same max-age setting).

Set `WEBUI_PUBLIC_READ=0` to require sign-in for job detail and execution list pages.

## Session, CSRF, and reverse-proxy hardening

For deployments behind TLS termination:

- Set `SECURE_COOKIES=1` (and usually `SECURE_SSL_REDIRECT=1` when Django terminates HTTPS directly).
- Set `CSRF_TRUSTED_ORIGINS` to a comma-separated list of origins, e.g. `https://scheduler.example.com`.
- Optionally set `SESSION_COOKIE_AGE` (seconds) to shorten operator sessions.
- When a reverse proxy sets `X-Forwarded-Proto: https`, set `USE_X_FORWARDED_PROTO=1` so Django treats requests as secure for cookie and redirect logic.

Run `python manage.py check --deploy` after setting `DEBUG=0`, `SECRET_KEY`, and `ALLOWED_HOSTS`. With `DEBUG=0`, the app also emits `scheduler_app.W002` when `SCHEDULER_CLI_SECRET` is empty.

## Graceful shutdown

Scheduler and worker management commands install SIGTERM/SIGINT handlers. Docker/Kubernetes stop signals finish the current tick or in-flight claim batch, then exit instead of sleeping until the next interval.

## Django admin vs web operator

Jobs and executions are view-only in Django admin, but a **superuser** can still manage Django auth users and groups. Create web operators with `ensure_dev_user` or `createsuperuser`, and treat admin credentials as a separate privileged surface in production.

## Web operator audit trail

Mutating web actions (create/edit/delete jobs, enable/disable/trigger, retry/cancel executions, resolve alerts) emit `operator_action` events on the related job for traceability. The same event type is emitted for destructive CLI commands (`job`, `execution`, `alerts`) with `source=cli`.

The Docker demo runs `ensure_dev_user` with default `admin`/`admin` credentials and prints a warning. With `DEBUG=0`, that command refuses to create a user unless `DEV_ADMIN_PASSWORD` is set explicitly.

## Investigate Failed Runs

```powershell
python manage.py execution list --status failed
python manage.py execution list --status timed_out
python manage.py execution list --status dead_lettered
python manage.py execution inspect <execution-id>
python manage.py alerts list
python manage.py alerts resolve <alert-id>
```

Retry-exhausted runs keep their truthful terminal status (`failed`/`timed_out`) and are flagged by a dead-letter record; `dead_lettered` status itself marks lease-abandoned runs. `alerts list` and the dead-letter records cover all three. Use the execution detail page to inspect output, traceback summary, worker id, timestamps, and attempt number.

## Retry Dead-Lettered Runs

```powershell
python manage.py execution retry <execution-id>
python manage.py execution cancel <execution-id>
```

Only retry idempotent tasks or tasks documented as safe to retry.

## Recover Expired Leases

The scheduler command runs lease recovery each tick:

```powershell
python manage.py scheduler run --once
```

Recovered executions appear in events, alerts when dead-lettered, CLI health, and web health/history.

## Prune History

```powershell
python manage.py prune_history
```

Each job controls retention by count and age. Set `retention_count=0` or `retention_days=0` on a job to disable that axis.

## Web Sign-In

Docker Compose creates a dev operator account via `ensure_dev_user` (default `admin` / `admin`). Sign in at `/accounts/login/` before using mutating web actions when `WEBUI_AUTH=1`.

## Production Settings

The default settings target local development: `DEBUG` is on and `ALLOWED_HOSTS` is `*`. `DEBUG` automatically defaults **off** once a `SECRET_KEY` is provided. For a real deployment set:

- `SECRET_KEY` — a long, random value (Django's `check --deploy` warns on the dev default)
- `DEBUG=0` and `ALLOWED_HOSTS` — your real hostnames, not `*`
- `DATABASE_URL` / `REDIS_URL` — managed PostgreSQL and Redis

Then review the remaining transport/cookie hardening:

```powershell
python manage.py check --deploy
```

Enable HTTPS redirect, HSTS, and secure session/CSRF cookies via a TLS-terminating proxy or the matching Django settings before exposing the app publicly. These are intentionally left unset so the local and Docker demos run over plain HTTP.

## Reset Local Development Data

Docker:

```powershell
docker compose down -v
docker compose up --build
```

SQLite test database is in memory during pytest. For local SQLite experiments only:

```powershell
Remove-Item .\db.sqlite3 -ErrorAction SilentlyContinue
$env:USE_SQLITE="1"
python manage.py migrate
```

