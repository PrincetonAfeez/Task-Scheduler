# Architecture Decision Record
## App — Task Scheduler
**Scheduling Infrastructure Group | Document 1 of 5**
**Status: Accepted**

---

## Context

The Scheduling Infrastructure group requires a portfolio-grade task scheduler that demonstrates how durable scheduling systems work underneath Celery Beat, APScheduler, cron, or Redis queues. The project must decide when work is due, create durable execution rows, claim them safely, execute only registered Python tasks, retry failures, recover expired leases, store history, expose operator controls, and provide observable health.

The app is a Django-based system using PostgreSQL as the authoritative store and Redis as a cache/previews accelerator. Redis is explicitly not the source of truth. The system’s strongest guarantee is **at-most-once durable claim per scheduled occurrence**, not exactly-once external side effects.

The selected architecture stores jobs, schedules, execution rows, events, dead letters, alerts, and scheduler/worker heartbeats in the database. The scheduler tick creates due `JobExecution` rows. The worker process performs both dispatch and execution: it claims runnable rows with row locks and leases, then runs registered task functions through an executor backend.

---

## Decision Drivers

- Preserve correctness in the database, not in process memory.
- Demonstrate scheduling primitives directly rather than delegating to Celery Beat, APScheduler, cron, or Redis queues.
- Make worker concurrency safe with durable row claims and leases.
- Keep task execution bounded by a registered catalog.
- Provide operator visibility through CLI, HTMX web UI, events, alerts, and heartbeats.
- Be honest about guarantees: at-most-once durable claim, not exactly-once side effects.
- Keep Redis as an optimization layer only.
- Support reviewers with repeatable Docker, CLI, and test flows.

---

## Decisions

### 1. PostgreSQL is the source of truth

Schedules, execution rows, events, retries, leases, dead letters, alerts, and heartbeats live in the database. Redis is used only for derived dashboard/previews and can be cleared without changing scheduling correctness.

### 2. Scheduler and worker are separate loops, but dispatch lives in the worker

The scheduler process creates due `JobExecution` rows. The worker process claims runnable rows and executes them. There is no separate dispatcher container because the claim-and-run loop is simple enough to keep in the worker.

### 3. Claiming uses row locks and leases

Workers claim rows with transactional locking and `select_for_update(skip_locked=True)`. A claimed row stores `claimed_by`, `worker_id`, `claimed_at`, and `lease_expires_at`. The effective lease is long enough to cover the task timeout plus buffer.

### 4. The system guarantees at-most-once durable claim

The scheduler does not claim exactly-once external side effects. A task can still duplicate external actions if it performs side effects and crashes before completion is written. Registered tasks should use the execution idempotency key.

### 5. Only registered Python tasks can run

Operators choose from a fixed task registry. They cannot type shell commands or arbitrary Python code into the CLI or web UI. This keeps the scheduler safer and reviewable.

### 6. Subprocess execution is the default

Tasks run through a subprocess backend so hard timeouts are enforceable. The in-process executor exists for tests and comparison but cannot enforce true hard timeouts.

### 7. Output/error are stored with hard truncation

Captured stdout and error text are truncated to 20,000 characters before storage, preventing runaway tasks from bloating the database.

### 8. Misfire and overlap are explicit policies

Misfire policies are `coalesce`, `catch_up`, and `skip`. Overlap policies are `skip`, `queue`, and `allow`. These are business semantics, not incidental implementation details.

### 9. Cache invalidation happens after DB commit

Dashboard/previews use Redis cache keys, but invalidation is targeted and deferred with `transaction.on_commit` so a concurrent reader cannot repopulate cache with pre-commit state.

### 10. Operators get both CLI and web UI

The CLI supports scheduling, worker, job, execution, health, pruning, and demo commands. The HTMX UI supports live dashboards, job management, execution details, alerts, and operator actions.

---

## Consequences

**Positive**
- The app demonstrates durable scheduling rather than wrapping a scheduler library.
- Worker concurrency is safe under PostgreSQL row locking.
- Redis improves responsiveness without becoming critical to correctness.
- Operators can inspect executions, output, errors, alerts, and heartbeats.
- Registered tasks reduce execution risk.
- Subprocess execution makes hard timeout behavior demonstrable.
- The design is honest about failure modes and side effects.

**Trade-offs**
- PostgreSQL is required for the strongest concurrency behavior.
- SQLite test mode cannot fully model `SKIP LOCKED`.
- Subprocess execution is heavier than in-process execution.
- The app is more operationally complex than a pure CLI scheduler.
- Disabling/cancelling does not kill already-running subprocesses.
- Exactly-once external effects remain out of scope.

---

## Alternatives Not Chosen

- Celery Beat as schedule truth.
- APScheduler as schedule truth.
- System cron.
- Redis sorted-set scheduler.
- Arbitrary shell command jobs.
- Workflow DAG engine.
- Exactly-once external side-effect guarantees.
- Multi-tenant RBAC.
- Kubernetes CronJob integration.

---

*Constitution reference: Article 1, Article 3.3, Article 4, Article 5, Article 6, and Article 7.*

---


# Technical Design Document
## App — Task Scheduler
**Scheduling Infrastructure Group | Document 2 of 5**

---

## Overview

Task Scheduler is a Django-based, database-backed cron-like scheduler. It stores authoritative state in PostgreSQL, uses Redis for dashboard/previews, exposes Django management-command CLI tooling, and provides an HTMX operator dashboard.

**Project:** `task-scheduler`  
**Django app:** `scheduler_app`  
**Python:** `>=3.12`  
**Primary database:** PostgreSQL  
**Cache:** Redis in production/demo  
**Core commands:** `scheduler`, `worker`, `job`, `execution`, `alerts`, `health`, `prune_history`, `demo`

---

## System Context

```text
Operators
  ├── HTMX Web UI
  └── Django management CLI

Scheduler process
  ├── recover expired leases
  ├── prune stale heartbeats/history
  └── create due JobExecution rows

Worker process
  ├── claim runnable rows
  ├── run registered task
  ├── write result
  ├── retry or dead-letter
  └── update heartbeat

PostgreSQL
  ├── Job
  ├── JobExecution
  ├── JobEvent
  ├── DeadLetter
  ├── Alert
  ├── SchedulerHeartbeat
  └── WorkerHeartbeat

Redis
  └── derived dashboard/previews only
```

---

## Core Models

### `Job`

Defines scheduled work.

Important fields:
- `registered_task_name`
- `schedule_type`
- `schedule_value`
- `timezone`
- `enabled`
- `next_run_at`
- `last_run_at`
- `overlap_policy`
- `misfire_policy`
- `misfire_grace_seconds`
- `max_attempts`
- `retry_backoff_seconds`
- `timeout_seconds`
- `retention_count`
- `retention_days`
- `alert_mode`
- `task_config`

Constraints:
- unique job name
- positive attempts
- positive timeout
- non-negative retry backoff
- index on `(enabled, next_run_at)`

### `JobExecution`

Represents one scheduled or manual occurrence.

Important fields:
- `scheduled_for`
- `run_after`
- `status`
- `attempt_number`
- `idempotency_key`
- `is_manual`
- `claimed_by`
- `claimed_at`
- `lease_expires_at`
- `started_at`
- `finished_at`
- `duration_ms`
- `output`
- `error`
- `worker_id`

Constraints:
- unique `idempotency_key`
- unique scheduled occurrence per job/scheduled_for for non-manual rows
- attempt positive
- indexes on status/run time, job/schedule, lease expiry, and job history

### `JobEvent`

Durable event log for due detection, occurrence creation, claims, worker starts/finishes, failures, retries, timeouts, misfires, lease recovery, stale result discards, alerts, cache invalidation, cancellations, and operator actions.

### `DeadLetter`

Stores exhausted or abandoned execution failures while preserving the truthful execution terminal status.

### `Alert`

Stores operator-visible alerts when the job alert mode is `web`; `log_only` emits structured logs/events without creating an alert row.

### Heartbeats

`SchedulerHeartbeat` and `WorkerHeartbeat` record process identity, health state, last activity, and recent activity counters for readiness and operator dashboards.

---

## Schedule Types

Supported schedule types:

- `one_time`
- `interval`
- `cron`

All persisted datetimes are UTC. Cron expressions are interpreted in the job timezone. DST policy:
- nonexistent spring-forward local times collapse to the first valid instant after the gap
- ambiguous fall-back local times run once

---

## Scheduler Tick Flow

```text
scheduler run
  ├── recover_expired_leases(now)
  ├── prune_stale_worker_heartbeats(now)
  ├── prune_stale_scheduler_heartbeats(now)
  ├── SchedulerService.tick()
  │   ├── heal enabled interval/cron jobs missing next_run_at
  │   ├── select due jobs with select_for_update(skip_locked=True)
  │   ├── compute due fire times
  │   ├── apply misfire policy
  │   ├── apply overlap policy
  │   ├── create JobExecution rows with deterministic idempotency keys
  │   ├── advance next_run_at
  │   ├── disable completed one-time schedules
  │   └── update scheduler heartbeat
  └── periodically prune history
```

Scheduled idempotency key:
```text
scheduled:{job_id}:{scheduled_for_utc_iso}
```

Manual idempotency key:
```text
manual:{job_id}:{uuid}
```

---

## Misfire Policies

### `coalesce`
Collapse the missed backlog into one latest due run.

### `catch_up`
Create missed occurrences up to the configured cap per scheduler tick.

### `skip`
Mark occurrences outside the grace window as missed.

---

## Overlap Policies

### `skip`
Mark new occurrence missed when prior work is active.

### `queue`
Create the occurrence but leave it pending until active work clears.

### `allow`
Permit concurrent executions for the same job.

---

## Claiming Flow

```text
claim_runnable_executions()
  ├── select pending/retry_scheduled rows with row locks
  ├── order by run_after then id
  ├── cancel queued rows for disabled jobs
  ├── enforce overlap at claim time
  ├── bump attempt_number on retry rows
  ├── mark claimed
  ├── set claimed_by/worker_id
  ├── set claimed_at
  ├── set lease_expires_at
  ├── emit claim event
  └── invalidate affected cache keys after commit
```

Claimable statuses:
- `pending`
- `retry_scheduled`

The claim layer also cancels queued rows when a job becomes disabled.

---

## Worker Execution Flow

```text
execute_claimed_execution()
  ├── lock row
  ├── reject stale claim if status/worker mismatch
  ├── mark running
  ├── update worker heartbeat
  ├── run task through executor backend
  ├── re-lock row
  ├── discard stale result if owner/state changed
  ├── write duration/output/error
  ├── clear lease/claim fields
  ├── mark succeeded OR failed/timed_out
  ├── apply retry/dead-letter transition
  └── update worker heartbeat
```

Output and error are each truncated to 20,000 characters.

---

## Executor Backends

### `SubprocessExecutor`

Default backend.

Behavior:
- writes task payload to a temporary JSON file
- runs `python -m scheduler_app.services.subprocess_runner`
- captures stdout and stderr
- enforces hard timeout
- parses task runner JSON
- deletes temp file

### `InProcessExecutor`

Test/comparison backend.

Behavior:
- calls registered task directly
- cannot enforce hard timeouts
- catches exceptions as failed results

---

## Registered Task Catalog

Task metadata includes:
- name
- description
- expected config
- idempotent flag
- safe-for-demo flag
- callable

Default tasks:
- `sleep_for_seconds`
- `always_succeed`
- `always_fail`
- `fail_once_then_succeed`
- `generate_report`
- `cleanup_old_runs`
- `write_file_artifact`
- `http_health_check_local`

Operators cannot schedule arbitrary shell commands or arbitrary Python code.

---

## Retry and Dead Letter Flow

Retry delay:

```text
retry_backoff_seconds * 2 ** (attempt_number - 1)
```

If attempts remain:
- execution becomes `retry_scheduled`
- `run_after` moves to the backoff time
- retry event is emitted

If attempts are exhausted:
- execution keeps `failed` or `timed_out`
- `DeadLetter` row is created
- alert/event is emitted

Manual retry creates a new manual `pending` execution.

---

## Cache Design

Redis stores:
- dashboard summary
- queue depth
- task catalog metadata
- upcoming-run previews
- per-job stats

Invalidation:
- targeted to affected keys
- excludes task catalog unless code changes
- deferred through `transaction.on_commit`
- never used for correctness

---

## Health Design

`/healthz` is liveness.

`/readyz` checks:
- database
- Redis when configured
- optional scheduler heartbeat freshness
- optional worker heartbeat freshness

Scheduler and worker heartbeat rows are updated during normal loops and pruned when stale.

---

## Known Limits

- At-most-once durable claim, not exactly-once side effects.
- PostgreSQL required for full claim semantics.
- SQLite cannot fully model row-lock concurrency.
- Disabling/cancelling does not stop already-running subprocesses.
- Redis is a disposable optimization layer.
- Demo task `fail_once_then_succeed` uses filesystem markers and is not multi-worker safe for the same idempotency key.

---

## Verification Summary

The repository declares:
- Python 3.12+
- coverage over `scheduler_app`
- coverage floor 93%
- Ruff and Mypy checks
- django-stubs configuration
- PostgreSQL marker tests
- CI with PostgreSQL 16 and Redis 8
- Django deploy check
- Redis readiness and integration smoke tests

The README documents 339 tests, about 97% coverage, and separate PostgreSQL/Redis validation flows.

---

*Constitution reference: Article 4, Article 6, Article 7, and Article 8.*

---


# Interface Design Specification
## App — Task Scheduler
**Scheduling Infrastructure Group | Document 3 of 5**

---

## CLI Interface

All primary operations use Django management commands.

### Scheduler

```powershell
python manage.py scheduler run
python manage.py scheduler run --once
python manage.py scheduler run --tick-seconds 5
python manage.py scheduler run --scheduler-id scheduler-a
```

### Worker

```powershell
python manage.py worker run
python manage.py worker run --workers 2
python manage.py worker run --once
python manage.py worker run --worker-id worker-a
```

### Jobs

```powershell
python manage.py job catalog
python manage.py job add "Daily report" generate_report cron --cron "0 9 * * 1-5" --timezone America/Los_Angeles
python manage.py job list
python manage.py job preview 1
python manage.py job trigger 1
python manage.py job enable 1
python manage.py job disable 1
python manage.py job delete 1
```

### Executions

```powershell
python manage.py execution list
python manage.py execution inspect 1
python manage.py execution retry 1
python manage.py execution cancel 1
```

### Alerts and operations

```powershell
python manage.py alerts resolve 1
python manage.py ensure_dev_user
python manage.py health
python manage.py prune_history
python manage.py demo single-fire
python manage.py demo misfire
python manage.py demo timeout
```

---

## CLI Secret Contract

When `SCHEDULER_CLI_SECRET` is set, destructive commands accept:

```powershell
--cli-secret <secret>
```

Applies to:
- job add/edit/enable/trigger/disable/delete
- execution retry/cancel
- alerts resolve

---

## Web Interface

### Public by default

- `/`
- job list
- job detail
- execution list
- `/healthz`

### Auth-required by default

- `/dashboard/`
- health/readiness page
- alerts
- execution detail output/error
- HTMX operational fragments
- mutating job/execution/alert actions

Settings:
- `WEBUI_AUTH=1` enables sign-in requirement for operator actions.
- `WEBUI_PUBLIC_READ=0` makes job detail and execution list private too.

---

## Schedule JSON Contract

### One-time

```json
{"run_at": "2026-01-01T12:00:00Z"}
```

### Interval

```json
{"every": "30s"}
```

or:

```json
{"every": {"minutes": 5}}
```

### Cron

```json
{"expression": "0 9 * * 1-5"}
```

---

## Job Configuration Contract

A job defines:
- registered task name
- schedule type/value
- timezone
- enabled flag
- overlap policy
- misfire policy
- max attempts
- retry backoff
- timeout
- retention policy
- alert mode
- task config JSON

Task config is passed to the registered task callable.

---

## Execution Status Contract

Runnable:
```text
pending
retry_scheduled
```

Active:
```text
claimed
running
```

Terminal:
```text
succeeded
failed
timed_out
missed
cancelled
dead_lettered
```

---

## Misfire Policy Contract

| Policy | Behavior |
|---|---|
| `coalesce` | Collapse missed backlog into one latest due occurrence |
| `catch_up` | Create missed occurrences up to cap |
| `skip` | Mark late occurrences missed |

---

## Overlap Policy Contract

| Policy | Behavior |
|---|---|
| `skip` | New occurrence becomes missed when older work is active |
| `queue` | New occurrence waits pending until active work clears |
| `allow` | Concurrent executions allowed |

---

## Registered Tasks

Operators may select only:

```text
sleep_for_seconds
always_succeed
always_fail
fail_once_then_succeed
generate_report
cleanup_old_runs
write_file_artifact
http_health_check_local
```

---

## Health Endpoint Contract

### `/healthz`
Process liveness. Returns OK when Django is running.

### `/readyz`
Readiness. Checks database and configured Redis, and may require fresh scheduler/worker heartbeats.

---

## Output Contract

Each execution stores:
- output text
- error text
- duration in ms
- status
- attempt number
- event history

Output and error are each capped at 20,000 characters.

---

## Side Effects

| Operation | Side Effect |
|---|---|
| scheduler tick | creates execution rows and advances schedules |
| worker run | claims rows, launches tasks, writes result |
| job trigger | creates manual execution |
| job disable | cancels queued rows, not running rows |
| execution retry | creates manual retry execution |
| execution cancel | cancels pending/claimed/retry rows |
| prune history | deletes old execution history |
| Redis invalidation | deletes derived dashboard/previews after commit |

---

*Constitution reference: Article 4, Article 6, and Article 8.*

---


# Runbook
## App — Task Scheduler
**Scheduling Infrastructure Group | Document 4 of 5**

---

## Requirements

### Runtime

- Python 3.12+
- Django
- PostgreSQL for production/concurrency correctness
- Redis for production cache/readiness/dashboard support

### Development

- pytest
- pytest-cov
- ruff
- mypy
- django-stubs

---

## Docker Quick Start

```powershell
docker compose up --build
```

Open:

```text
http://127.0.0.1:8000
```

Default dev operator:

```text
admin / admin
```

---

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:DATABASE_URL="postgres://scheduler:scheduler@localhost:5432/task_scheduler"
$env:REDIS_URL="redis://localhost:6379/0"
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Run scheduler and worker in separate terminals:

```powershell
.\.venv\Scripts\python.exe manage.py scheduler run
.\.venv\Scripts\python.exe manage.py worker run --workers 2
```

---

## Smoke Test

```powershell
python manage.py job add "Every minute" always_succeed interval --every 60s
python manage.py scheduler run --once
python manage.py worker run --workers 2 --once
python manage.py execution list
```

Expected:
- scheduler creates a pending occurrence
- worker claims and runs it
- execution becomes succeeded

---

## Common Operations

### View task catalog

```powershell
python manage.py job catalog
```

### Add cron job

```powershell
python manage.py job add "Daily report" generate_report cron --cron "0 9 * * 1-5" --timezone America/Los_Angeles
```

### Preview upcoming runs

```powershell
python manage.py job preview 1
```

### Manual trigger

```powershell
python manage.py job trigger 1
```

### Disable job

```powershell
python manage.py job disable 1
```

Expected:
- queued pending/retry/claimed rows cancel
- already running rows continue until finish, timeout, or lease recovery

### Retry execution

```powershell
python manage.py execution retry 1
```

### Cancel execution

```powershell
python manage.py execution cancel 1
```

### Resolve alert

```powershell
python manage.py alerts resolve 1
```

---

## Health Checks

```powershell
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Expected:
- `/healthz` confirms Django process liveness
- `/readyz` returns 200 when dependencies/required heartbeats are healthy and 503 when degraded

---

## Testing

### Default local suite

```powershell
python -m pytest
python -m pytest --cov=scheduler_app --cov-report=term-missing
```

### PostgreSQL row-lock tests

```powershell
$env:DATABASE_URL="postgres://scheduler:scheduler@localhost:5432/task_scheduler"
python -m pytest -m postgresql --ds=task_scheduler.test_settings_postgres
```

### Docker evidence

```powershell
docker compose up --build -d
docker compose exec web python -m pytest --cov=scheduler_app --cov-report=term-missing --cov-fail-under=93
docker compose exec web python -m pytest -m postgresql --ds=task_scheduler.test_settings_postgres -v
```

---

## CI Parity

CI runs:
- PostgreSQL 16
- Redis 8
- Python 3.12
- Ruff
- Mypy
- Django deploy check
- SQLite coverage gate
- PostgreSQL concurrency tests
- Redis readiness/integration smoke tests

---

## Troubleshooting

### Scheduler creates no executions

Check:
- job enabled
- `next_run_at` is set and due
- schedule value valid
- one-time job was not already completed
- scheduler is pointing at the expected database

### Worker claims nothing

Check:
- execution status is `pending` or `retry_scheduled`
- `run_after <= now`
- job enabled
- overlap policy is not blocking
- another worker has not claimed the row
- expired leases have been recovered

### Row stuck in claimed/running

Check:
- `lease_expires_at`
- worker heartbeat
- scheduler tick recovery
- task timeout vs lease buffer

### Duplicate external side effects

The scheduler guarantees at-most-once durable claim, not exactly-once external effects. Use `TaskContext.idempotency_key` in tasks that call external systems.

### Redis outage

Expected:
- dashboard/previews may degrade or recompute
- scheduling correctness remains in PostgreSQL
- readiness may be degraded if Redis is configured

### Running job did not cancel

Expected:
- disabling/cancelling affects queued work
- running work must finish, time out, or recover through lease handling

---

## Maintenance Notes

- Keep PostgreSQL authoritative.
- Keep Redis non-authoritative.
- Preserve registered-task-only execution.
- Preserve at-most-once claim wording.
- Add PostgreSQL tests before changing claim logic.
- Add tests before changing misfire/overlap policy.
- Preserve `transaction.on_commit` cache invalidation.
- Do not claim exactly-once side effects.
- Keep lease duration aligned with timeout.
- Preserve CLI/web operator boundaries.

---

*Constitution reference: Article 6, Article 5, and Article 8.*

---


# Lessons Learned
## App — Task Scheduler
**Scheduling Infrastructure Group | Document 5 of 5**

---

## Why This Design Was Chosen

This design was chosen because scheduling is more than a timer. Durable scheduling needs due detection, idempotency, overlap policy, misfire handling, worker claiming, lease recovery, retry policy, dead-letter handling, alerts, and health checks.

The most important decision was making PostgreSQL the source of truth. That gives the app durable rows, uniqueness constraints, transactions, and row-lock semantics. Redis is useful for responsiveness, but it does not decide whether work exists.

The registered-task catalog keeps execution safe. A scheduler that accepts arbitrary commands becomes a remote command runner. This app stays reviewable by executing only known task functions.

The app also states its guarantee honestly: at-most-once durable claim. Exactly-once side effects are not promised.

---

## What Was Intentionally Omitted

- Arbitrary shell command execution.
- Celery Beat/APScheduler/cron as source of truth.
- Exactly-once external side effects.
- Workflow DAGs.
- Multi-tenant RBAC.
- Cross-region scheduling.
- Distributed workflow orchestration.
- Forced kill from a job-disable toggle.
- Redis-authoritative scheduling.

---

## Biggest Weakness

The biggest weakness is side-effect duplication after crashes. If a subprocess performs external work and then crashes before completion is recorded, a recovered retry can duplicate that external action. The scheduler mitigates duplicate claims, but task authors still need idempotent external behavior.

The second weakness is operational complexity. PostgreSQL, Redis, Django, scheduler loop, worker loop, web UI, and subprocesses are realistic, but they are heavier than a simple script.

The third weakness is that SQLite cannot validate the most important concurrency behavior; PostgreSQL tests are necessary.

---

## Scaling Considerations

If task volume grows:
- increase worker count
- keep claim batches bounded
- monitor queue depth by status
- tune indexes
- keep retention pruning enabled

If side effects matter:
- require idempotency keys in tasks
- store external operation IDs
- add side-effect dedupe helpers
- make retries idempotency-aware

If operator use grows:
- add role-based permissions
- harden audit logging
- separate readonly/operator/admin permissions
- reduce public read surfaces

---

## Next Refactor

1. **Task side-effect idempotency helper table** — make external dedupe easier.
2. **Schedule versioning** — record which schedule definition produced each execution.
3. **Cooperative cancellation** — let running tasks observe cancellation.
4. **Metrics endpoint** — expose structured queue depth and latency data.
5. **Operator audit hardening** — separate operator actions from system events.

---

## What This Project Taught

- Durable scheduling is a database problem.
- Row locks define safe worker scaling.
- Misfires are product decisions.
- Overlap policy is separate from misfire policy.
- Leases are recovery tools, not magic.
- Subprocess execution makes hard timeouts enforceable.
- Redis is useful but should not hold truth.
- Honest guarantees are better than impossible claims.

---

*Constitution v2.0 checklist: This document satisfies Article 5, Article 6, and Article 7 for Task Scheduler.*
