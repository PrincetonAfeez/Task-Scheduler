# ADR 0001: Build The Scheduler Instead Of Using Celery Beat

## Status

Accepted

## Decision

The project builds its own scheduler engine instead of delegating schedule decisions to Celery Beat, APScheduler, cron, or Django-Q.

## Context

The capstone goal is to learn scheduling, durable work creation, concurrency, misfire behavior, and recovery. A wrapper around an existing scheduler would hide the main lesson.

## Consequences

The codebase owns due detection, `next_run_at` advancement, misfire policy, overlap policy, and durable execution creation. Existing schedulers remain useful references, but they are not runtime dependencies for correctness.

