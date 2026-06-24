# ADR 0007: JobExecution Is The Durable Queue

## Status

Accepted

## Decision

`JobExecution` rows are the durable work queue.

## Context

The project needs each scheduled occurrence to be inspectable, retryable, claimable, recoverable, and auditable. A database row naturally carries that state.

## Consequences

The scheduler creates `JobExecution` rows, the dispatcher claims them, and the worker updates them. PostgreSQL stores pending work, run history, retry state, dead-letter state, and operator-facing evidence.

