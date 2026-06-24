# Changelog

All notable changes to this project are documented here.

## 1.1.1

### Added
- CLI operator audit trail (`operator_action` events with `source=cli`) for destructive commands
- Regression tests for overlap `queue` FIFO claiming and `include_pending=False` at claim time

### Fixed
- Overlap `queue` could stall when multiple `pending` rows existed with nothing in flight (sibling pending blocked claims)

### Changed
- `requirements.lock` regenerated with pinned `gunicorn`; CI deploy check sets `SCHEDULER_CLI_SECRET`

## 1.1.0

### Added
- Overlap hardening: pending and due `retry_scheduled` rows block overlap `skip`; per-occurrence re-check during catch-up ticks
- `scheduler_app.W002` deploy check when `SCHEDULER_CLI_SECRET` is empty with `DEBUG=0`
- Graceful SIGTERM/SIGINT shutdown for scheduler and worker loops
- Web operator audit trail (`operator_action` events); bulk alert resolve on alerts page
- `/readyz` returns 503 when database migrations are pending
- IANA timezone validation on job forms; HTMX 401 fragments include a sign-in link
- Production compose override runs gunicorn and requires `SECRET_KEY` / `SCHEDULER_CLI_SECRET`

### Changed
- Heal path uses per-job `select_for_update` transactions
- README/runbook overlap, cancel-vs-running, admin superuser, and ops docs updated

### Fixed
- Overlap `skip` could create multiple catch-up pendings in one tick when no run was in flight
- README inaccurately listed `retry_scheduled` as overlap-active while code ignored pending rows

## 1.0.1 (audit rounds)

### Added
- Completed one-time guard blocks timezone-only resurrection; multi-job claim cache invalidation
- POST sign-out form; fully view-only Django admin; web create/edit `IntegrityError` handling
- Login throttle TTL refresh on `incr`; scheduler periodic `prune_history` via `PRUNE_HISTORY_EVERY_N_TICKS`
- Self-heal for enabled interval/cron jobs missing `next_run_at` during scheduler tick (with cache invalidation)
- One-time `run_at` comparison normalized to UTC instants; heal cache and IntegrityError race regression tests

### Changed
- README/runbook: accurate admin capabilities, 325 test count, skipped-marker note; overlap `skip` limitation documented
- `docker-compose.prod.yml` scheduler sets `PRUNE_HISTORY_EVERY_N_TICKS=720`

### Fixed
- Sign out returned HTTP 405 (GET link vs Django 5 POST-only logout)
- Completed one-time jobs could re-fire after timezone-only edit + enable
- Multi-job `worker run --once` left some job stats cached until TTL

## 1.0.0

Initial academic capstone: PostgreSQL-backed scheduler, worker, web UI, CLI, Redis cache, Docker demo stack.
