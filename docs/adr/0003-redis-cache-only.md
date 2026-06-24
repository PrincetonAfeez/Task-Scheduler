# ADR 0003: Redis Is Cache Only

## Status

Accepted

## Decision

Redis is used only for derived dashboard summaries, task catalog metadata, and upcoming-run previews.

## Context

Redis is fast and useful for repeated display queries, but the capstone requires schedules and executions to survive process restarts and cache loss.

## Consequences

Clearing Redis may slow dashboard rendering until values are recomputed. It cannot lose jobs, executions, retries, dead letters, or heartbeats. PostgreSQL remains authoritative.

