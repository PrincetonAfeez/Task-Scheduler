# Task Scheduler

A DB-backed cron-like task scheduler built with Python, Django, HTMX, PostgreSQL, Redis, and Django management-command CLI tooling.

This project builds the scheduling primitive directly. Celery Beat, APScheduler, cron, and Redis queues are not the source of truth. PostgreSQL stores schedules, durable execution rows, run history, retries, leases, dead letters, alerts, and heartbeat records.

## System Boundary

- Scheduler decides **when** work is due.
- The **dispatcher** (claim + lease logic in `scheduler_app/services/claiming.py` and `dispatcher.py`) runs **inside the worker process** — there is no separate dispatcher container.
- Worker executes only registered Python tasks.
- PostgreSQL preserves the truth.
- Redis accelerates dashboards and previews only.

The strongest guarantee is: **the system guarantees at-most-once durable claim per scheduled occurrence. It does not guarantee exactly-once side effects across crashes, network failures, or non-idempotent external actions.**

## Architecture

```mermaid
flowchart LR
  subgraph operators [Operators]
    WebUI[Web UI / HTMX]
    CLI[Management CLI]
  end
  subgraph processes [Processes]
    Scheduler[Scheduler tick]
    Worker[Worker pool]
  end
  subgraph storage [Storage]
    PG[(PostgreSQL)]
    Redis[(Redis cache)]
  end
  WebUI --> PG
  CLI --> PG
  Scheduler --> PG
  Scheduler --> Redis
  Worker --> PG
  Worker --> Redis
  Scheduler -->|creates due rows| PG
  Worker -->|claims, leases, executes| PG
```

The worker process performs both dispatch (row locks + leases) and execution (subprocess tasks).

## Demo Flow (60 seconds)

1. `docker compose up --build`
2. Open `http://127.0.0.1:8000` (public home) and sign in (`admin` / `admin`)
3. Dashboard → **New Job** → task `always_succeed`, interval `60s`
4. Watch **Recent Activity** populate; open execution detail for output
5. Disable the job and confirm queued rows cancel; re-enable to resume schedule

## Quick Start With Docker

```powershell
docker compose up --build
```

Open `http://127.0.0.1:8000` for the public home page. Sign in with the default dev operator account (`admin` / `admin`) to open the live dashboard and manage jobs.

Create a demo job from another terminal:

```powershell
docker compose exec web python manage.py job add "Every minute" always_succeed interval --every 60s
docker compose exec web python manage.py job list
docker compose exec web python manage.py execution list
```

## Local Non-Docker Setup

Create the virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Dependencies are declared with version ranges in [requirements.txt](requirements.txt). [requirements.lock](requirements.lock) pins exact runtime versions for reproducible installs (`pip install -r requirements.lock`). Development tools (pytest, ruff, mypy) are in [requirements-dev.txt](requirements-dev.txt).

Project metadata and tool configuration live in [pyproject.toml](pyproject.toml).

**Environment variables:** Django reads `os.environ` directly. Copy [.env.example](.env.example) for reference, then export variables in your shell (or rely on Docker Compose `environment:` blocks). A `.env` file is **not** loaded automatically unless you add tooling such as `python-dotenv`.

Start PostgreSQL and Redis locally, or point to existing services:

```powershell
$env:DATABASE_URL="postgres://scheduler:scheduler@localhost:5432/task_scheduler"
$env:REDIS_URL="redis://localhost:6379/0"
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Run the scheduler and worker in separate terminals:

```powershell
.\.venv\Scripts\python.exe manage.py scheduler run
.\.venv\Scripts\python.exe manage.py worker run --workers 2
```

For unit and integration tests, the default pytest settings use SQLite and LocMem cache (`task_scheduler.test_settings`). PostgreSQL is required for row-locking concurrency tests and the `demo single-fire` command test.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=scheduler_app --cov-report=term-missing
```

The suite collects **339 tests** (336 run locally; 3 skip without PostgreSQL/Redis markers). Coverage is ~**97%** on `scheduler_app` (CI enforces **93%** minimum). Tests are organized under `tests/` including `test_coverage_*` modules and `test_management_loops.py` for scheduler/worker loop and PostgreSQL demo coverage.

The PostgreSQL-specific tests are marked `postgresql` and skip under SQLite (where `SELECT ... FOR UPDATE SKIP LOCKED` is a no-op). To exercise real row-locking behaviour:

```powershell
$env:DATABASE_URL="postgres://scheduler:scheduler@localhost:5432/task_scheduler"
.\.venv\Scripts\python.exe -m pytest -m postgresql --ds=task_scheduler.test_settings_postgres
```

With `REDIS_URL` set, readiness tests verify Redis-backed cache probes on `/readyz`. Three tests are marked `postgresql` or require Redis and skip under the default SQLite/LocMem pytest settings — that is expected on a laptop without Docker Postgres.

**Full evaluation evidence (Docker):**

```powershell
docker compose up --build -d
docker compose exec web python -m pytest --cov=scheduler_app --cov-report=term-missing --cov-fail-under=93
docker compose exec web python -m pytest -m postgresql --ds=task_scheduler.test_settings_postgres -v
```

## CLI

```powershell
python manage.py scheduler run --once
python manage.py worker run --workers 2 --once
python manage.py job catalog
python manage.py job add "Daily report" generate_report cron --cron "0 9 * * 1-5" --timezone America/Los_Angeles
python manage.py job preview 1
python manage.py job trigger 1
python manage.py execution list
python manage.py execution inspect 1
python manage.py execution retry 1
python manage.py alerts resolve 1
python manage.py execution cancel 1
python manage.py ensure_dev_user
python manage.py health
python manage.py prune_history
python manage.py demo single-fire
python manage.py demo misfire
python manage.py demo timeout
```

## Registered Tasks

Operators can choose only from a fixed task catalog:

- `sleep_for_seconds`
- `always_succeed`
- `always_fail`
- `fail_once_then_succeed`
- `generate_report`
- `cleanup_old_runs`
- `write_file_artifact`
- `http_health_check_local`

Users cannot type arbitrary shell commands or raw Python code into the CLI or web UI.

## Scheduling

Supported schedule types:

- `one_time`: `{"run_at": "2026-01-01T12:00:00Z"}`
- `interval`: `{"every": "30s"}` or `{"every": {"minutes": 5}}`
- `cron`: `{"expression": "0 9 * * 1-5"}`

All persisted datetimes are UTC. Cron expressions are interpreted in the job timezone. The documented DST policy is:

- Nonexistent spring-forward local times collapse to the first valid instant after the gap (for example, a `02:30` job on the US spring-forward day runs at `03:00`).
- Ambiguous fall-back local times run once.

## Reliability Policies

Retries use exponential backoff:

```text
delay = retry_backoff_seconds * 2 ** (attempt_number - 1)
```

Misfire policies:

- `coalesce`: collapse the entire missed backlog into the single latest occurrence (emitting one misfire event) and advance past it in one tick; no per-occurrence `missed` rows are written.
- `catch_up`: create each missed occurrence up to `MISFIRE_CATCH_UP_CAP` per tick.
- `skip`: mark occurrences outside the grace window as missed.

Overlap policies:

- `skip`: mark the new due occurrence missed if an older run is active.
- `queue`: create the occurrence but do not claim it until no active run exists.
- `allow`: permit concurrent executions for the same job.

Timeouts use a subprocess executor. If a task exceeds `timeout_seconds`, the subprocess is killed and the execution is marked timed out, then retried or dead-lettered according to policy.

Captured stdout and error/traceback are each truncated to 20,000 characters before being stored on the execution row, so a runaway task cannot bloat the database.

Terminal status model: when retries are exhausted the execution keeps its truthful terminal status (`failed` or `timed_out`) so run history stays informative, and a `DeadLetter` record plus an alert flag it for operators. The `dead_lettered` status is reserved for executions abandoned through expired-lease recovery with no attempts remaining.

Alerts are delivered per job via `alert_mode`:

- `web` (default): emit a structured ALERT event/log **and** store an operator-visible `Alert` row.
- `log_only`: emit the structured ALERT event/log only (no `Alert` row).

## Redis Cache

Redis caches derived dashboard data:

- dashboard summary counts
- queue depth
- registered task catalog metadata
- upcoming-run previews

Invalidation is **targeted**: only the affected keys are deleted (dashboard summary, queue depth, all-upcoming, and the touched job's stats/upcoming). It does not flush the whole Redis database, and the code-defined task catalog is left to expire on its own TTL. Deletions are deferred to `transaction.on_commit`, so a concurrent reader cannot repopulate the cache with pre-commit data.

Invalidation happens when jobs are created, edited, enabled, disabled, deleted, manually triggered, scheduler ticks create occurrences, executions complete, executions retry, executions dead-letter, leases recover, and retention pruning runs. If Redis is cleared, schedules and execution correctness are unaffected because PostgreSQL is authoritative.

## Retention

Each job stores `retention_count` and `retention_days`. Set either value to **0** to disable that axis (for example, `retention_days=0` keeps all ages and prunes only by count).

## Web UI Authentication

Mutating web actions (create/edit/delete jobs, trigger/disable, retry/cancel executions, resolve alerts) require sign-in when `WEBUI_AUTH=1` (the default). The live dashboard (`/dashboard/`), health page, alerts list, execution detail (including output/error), and HTMX operational fragments also require sign-in.

The public home page (`/`), job list, job detail (stats and metadata), and execution list remain readable without sign-in. List pages paginate at **50** rows (job detail executions: **30**). The dashboard jobs panel shows the first **25** jobs with a link to the full paginated job list.

Destructive CLI operations (`job add`, `job edit`, `job enable`, `job trigger`, `job disable`, `job delete`, `execution retry`, `execution cancel`, `alerts resolve`) accept an optional `--cli-secret` when `SCHEDULER_CLI_SECRET` is set in the environment.

Set `WEBUI_PUBLIC_READ=0` to require sign-in for job detail and execution list (not just output/health). Sign-in attempts are rate-limited via cache (`LOGIN_RATE_LIMIT_ATTEMPTS`, default 10 per 5 minutes).

After sign-in, operators land on `/dashboard/`. Dashboard HTMX fragments pause polling while the browser tab is hidden.

Docker Compose runs `ensure_dev_user` on startup to create `admin` / `admin` unless that user already exists (with a stderr warning). Override with `DEV_ADMIN_USERNAME`, `DEV_ADMIN_PASSWORD`, and `DEV_ADMIN_EMAIL`. With `DEBUG=0`, default credentials are refused unless `DEV_ADMIN_PASSWORD` is set.

## Health Endpoints

- `/healthz` — process liveness (always returns `ok` if Django is running)
- `/readyz` — readiness (returns `503` if PostgreSQL or Redis is unreachable when Redis is configured; optionally requires fresh scheduler and/or worker heartbeats when `READYZ_REQUIRE_HEARTBEATS=1` / `READYZ_REQUIRE_WORKER_HEARTBEAT=1`)

## Demo vs Production

| Setting | Docker demo default | Production guidance |
|---------|---------------------|---------------------|
| `DEBUG` | `1` | `0` |
| `SECRET_KEY` | compose dev value | long random secret |
| `ALLOWED_HOSTS` | `*` | real hostnames |
| `WEBUI_AUTH` | `1` | keep enabled |
| TLS cookie flags | off (plain HTTP) | enable via reverse proxy |

Use `docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build` as a starting point for production-like settings. The prod override runs **gunicorn**, requires `SECRET_KEY` and `SCHEDULER_CLI_SECRET`, and sets `CSRF_TRUSTED_ORIGINS`. Copy [.env.example](.env.example) for local configuration.

**Production hardening checklist:** run behind a TLS reverse proxy; use gunicorn/uvicorn instead of `runserver`; set a long random `SECRET_KEY`, real `ALLOWED_HOSTS`, and `SCHEDULER_CLI_SECRET`; keep `WEBUI_AUTH=1`; enable `SECURE_COOKIES=1` and HSTS at the proxy or via settings.

## Known Limitations

- **At-most-once claim, not exactly-once side effects.** Crashes after external work but before completion can duplicate side effects unless tasks are idempotent (see [ADRs](docs/adr)).
- **Leases must cover task timeouts.** Claims set `lease_expires_at = max(LEASE_SECONDS, timeout_seconds + LEASE_BUFFER_SECONDS)` so a subprocess cannot outlive its lease.
- **Disabling a job cancels queued `pending`, `retry_scheduled`, and `claimed` rows** but does not stop an already-`running` execution. The same applies to **manual cancel** on the web UI and CLI — `running` rows must finish, time out, or recover through lease handling. `next_run_at` is preserved so re-enable can resume the schedule (except completed **one-time** jobs — re-enable requires editing `run_at` or creating a new job). The same cancel behavior applies when disabling via the web form, CLI, or at claim time when a job is already disabled.
- **Overlap `skip` treats `claimed`, `running`, queued `pending`, and due `retry_scheduled` rows as active at scheduler tick time.** Catch-up ticks re-check overlap before each occurrence, so only one new `pending` row is created per busy job per tick. At **claim** time, only in-flight work blocks another claim so overlap `queue` can drain multiple pending rows in FIFO order.
- **Manual trigger requires the job to be enabled** on web UI, CLI, and in `create_manual_execution()`.
- **Web UI auth is application-level.** CLI accepts an optional shared secret; Django admin is a separate privileged surface — jobs, executions, alerts, and events are view-only in admin (no add/edit/delete). A Django **superuser** can still manage auth users/groups; use separate credentials from the web operator account in production.
- **Login rate limiting requires a shared cache** (Redis) when running multiple web workers; LocMem is per-process.
- **Demo task `fail_once_then_succeed` uses filesystem markers** and is not safe under multi-worker concurrency with the same idempotency key.
- **Redis cache uses `IGNORE_EXCEPTIONS`.** If Redis is down, dashboards recompute from PostgreSQL instead of erroring.
- **Stale scheduler and worker heartbeat rows** older than `HEARTBEAT_PRUNE_SECONDS` (default 24h) are pruned during each scheduler tick.

## Evaluation Evidence

| Claim | Evidence |
|-------|----------|
| At-most-once durable claim | `SELECT … FOR UPDATE SKIP LOCKED` claiming + idempotency keys + PostgreSQL concurrency test |
| Not exactly-once side effects | Documented in README/ADRs; subprocess timeout + lease recovery tests |
| Disable cancels queued work | Web, CLI, form, and claim-time guards + tests |
| Auth layers | Web UI login (POST logout), optional CLI secret, view-only Django admin |
| Cache is non-authoritative | Redis `IGNORE_EXCEPTIONS`, targeted invalidation tests |
| Production-minded ops | `/healthz`, `/readyz`, heartbeat pruning, retention pruning, structured JSON logs |

Automated suite: **339 tests** (336 run / 3 skipped without Postgres/Redis), **97%** line coverage on `scheduler_app` (CI floor **93%**). See [.github/workflows/ci.yml](.github/workflows/ci.yml) for Ruff, Mypy, deploy check, SQLite coverage gate, PostgreSQL marker tests, and Redis smoke tests.

## License

This project is licensed under the [MIT License](LICENSE).

## Documentation

- [Technical design](docs/technical_design.md)
- [Runbook](docs/runbook.md)
- [Lessons learned](docs/lessons_learned.md)
- [ADRs](docs/adr)
