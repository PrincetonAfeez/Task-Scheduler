# Technical Design

## Data Flow

1. A `Job` stores the schedule definition and policy knobs.
2. The scheduler loop reads enabled jobs where `next_run_at <= now`.
3. It creates `JobExecution` rows for due occurrences and advances `next_run_at`.
4. The worker process dispatches work: it claims runnable executions with row locks and leases (`claiming.py`, `dispatcher.py`), then executes registered task code in a subprocess.
5. Failures move through retry or dead-letter handling.
6. Redis caches dashboard summaries and upcoming previews, but never owns correctness.

## Transaction Boundaries

Occurrence creation and schedule advancement happen inside one database transaction. The unique constraint on `(job_id, scheduled_for)` for scheduled runs prevents duplicate durable occurrences.

Claiming happens in a separate transaction using `select_for_update(skip_locked=True)`. Claimed rows receive `claimed_by`, `claimed_at`, `worker_id`, and `lease_expires_at`.

Execution completion is written back in a transaction. A failed or timed-out execution with attempts remaining moves to `retry_scheduled` with a future `run_after`. Once attempts are exhausted it keeps its truthful terminal status (`failed` or `timed_out`) and gets a `DeadLetter` record plus an alert. The `dead_lettered` status itself is reserved for executions abandoned through expired-lease recovery with no attempts remaining.

## Scheduler Loop

The scheduler uses an injectable clock in core logic. Tests can freeze time with `FrozenClock`. The loop may sleep with monotonic process time, but due decisions are made against the injected current UTC time.

Misfire handling depends on the per-job policy: `catch_up` creates each missed occurrence from `next_run_at` up to `now` (bounded by `MISFIRE_CATCH_UP_CAP` per tick), `skip` marks occurrences outside the grace window as missed, and `coalesce` collapses the entire missed backlog into the single latest run and advances past it in one tick.

Overlap `skip` treats `claimed`, `running`, queued `pending`, and due `retry_scheduled` rows as active during scheduler ticks. During catch-up ticks the scheduler re-checks overlap before materializing each occurrence so a busy job receives at most one new `pending` row per tick. Claiming uses a narrower busy check (in-flight and due retries only) so overlap `queue` can claim pending rows in FIFO order.

## Worker Lifecycle

Workers claim executions, mark them running, spawn an isolated subprocess, and persist the result. The required backend is `SubprocessExecutor`, which enforces hard timeouts by killing the process. `InProcessExecutor` exists only for tests and comparison. Captured stdout and error/traceback are each truncated to 20,000 characters before being written to the execution row.

## Cache Invalidation

The cache layer deletes only the affected keys (dashboard summary, queue depth, all-upcoming, and per-job stats/upcoming preview) rather than flushing the whole database; the code-defined task catalog is left to expire on its own TTL. Scheduler ticks invalidate per-job keys for every job that materialized occurrences during the tick. Deletions are deferred to `transaction.on_commit`, so a concurrent reader cannot repopulate the cache with pre-commit data. Each invalidation also writes a `JobEvent` so operators can see why derived data was refreshed. Redis is configured with `IGNORE_EXCEPTIONS` so cache outages degrade to PostgreSQL-backed recompute.

## Failure Handling

Expired leases are recovered by the scheduler command. If attempts remain, the execution is requeued as `retry_scheduled`; otherwise it is dead-lettered. This does not imply that external side effects did not happen. Task authors should make side-effecting tasks idempotent.

## Scheduler Maintenance

Each scheduler tick also performs housekeeping beyond occurrence creation:

- **Lease recovery** — expired leases are requeued or dead-lettered (see Failure Handling).
- **Heartbeat pruning** — scheduler and worker heartbeat rows older than `HEARTBEAT_PRUNE_SECONDS` are deleted.
- **History pruning** — when `PRUNE_HISTORY_EVERY_N_TICKS` is greater than zero, every N ticks the scheduler runs retention pruning for old executions, events, and alerts (see `retention.py`).
- **Self-heal** — enabled interval/cron jobs with a null `next_run_at` (for example after manual DB edits) are re-anchored via `heal_enabled_jobs_missing_next_run()`, with targeted cache invalidation for affected jobs.

These steps run in the same management command loop as due-job processing; they do not replace external monitoring of `/healthz` and `/readyz`.

