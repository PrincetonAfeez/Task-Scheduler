# ADR 0002: PostgreSQL Is Required

## Status

Accepted

## Decision

PostgreSQL is required for local development and deployment.

## Context

The scheduler depends on transactions, unique constraints, row locking, and `SKIP LOCKED` behavior. SQLite is useful for fast unit and smoke tests, but it does not prove the concurrency behavior this project is meant to teach.

## Consequences

Docker Compose includes PostgreSQL. Real single-fire and claim tests must run against PostgreSQL. The test settings use SQLite only for non-locking unit and interface tests.

