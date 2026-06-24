# ADR 0008: Deadlock and Starvation Avoidance Under Concurrent Claiming

## Status

Accepted

## Decision

Concurrent schedulers and workers coordinate through PostgreSQL row locks using
`SELECT ... FOR UPDATE SKIP LOCKED`, short lock-holding transactions, and a
consistent lock ordering. Leases plus a recovery pass bound the worst-case wait
when a lock holder crashes.

## Context

Multiple scheduler and worker processes compete for the same `Job` and
`JobExecution` rows. Naive `SELECT ... FOR UPDATE` (without `SKIP LOCKED`) makes
late arrivers *block* on locked rows, which creates two risks:

- **Deadlock** — two transactions acquire row locks in opposite orders and wait
  on each other.
- **Starvation / head-of-line blocking** — a slow lock holder stalls every other
  process queued behind the same row.

## How the design avoids them

- **`SKIP LOCKED` instead of blocking waits.** Both the scheduler's due-job query
  (`due.py`) and the dispatcher's claim query (`claiming.py`) use
  `select_for_update(skip_locked=True)`. A process never waits on a row another
  process already holds; it skips to the next available row. No blocking wait
  means no lock-wait deadlock and no head-of-line starvation.
- **Consistent lock ordering.** Both queries lock rows in a deterministic order
  (`order_by("next_run_at", "id")` for jobs, `order_by("run_after", "id")` for
  executions), so even if `SKIP LOCKED` were removed, locks would be acquired in
  a single global order — the standard deadlock-prevention rule.
- **Short transactions; work runs outside locks.** Locks are held only for the
  claim/advance writes. The actual task runs in a subprocess *after* the claiming
  transaction commits (`worker.py`), so a long-running or hung task never holds a
  database lock.
- **Single-row ownership + unique constraint.** The `unique(job_id, scheduled_for)`
  partial constraint and per-row claim make a given occurrence claimable at most
  once, so contention cannot corrupt state even under a race.
- **Leases bound crash impact.** A crashed holder's `lease_expires_at` lets
  `recover_expired_leases` requeue or dead-letter the row, so a dead process
  cannot starve an occurrence indefinitely.

## Consequences

- The system is free of lock-wait deadlock by construction (no blocking waits),
  and starvation is bounded by the lease duration rather than by another
  process's runtime.
- Residual, accepted risks: a task whose lease expires mid-run can be re-claimed
  and its side effects run more than once (see ADR 0005 — at-most-once *claim*,
  not exactly-once side effects); and `SKIP LOCKED` trades strict FIFO fairness
  for liveness, so ordering across processes is best-effort, not guaranteed.
