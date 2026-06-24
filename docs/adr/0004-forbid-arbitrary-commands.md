# ADR 0004: Arbitrary Commands Are Forbidden

## Status

Accepted

## Decision

Jobs reference a fixed registry of code-defined Python tasks. Operators cannot enter raw shell commands or arbitrary Python code through the CLI or web UI.

## Context

A scheduler that executes arbitrary user input becomes a remote-code-execution system. That is explicitly outside the project scope.

## Consequences

The task catalog exposes metadata, expected config, idempotency, and demo safety. Adding new executable behavior requires a code change and review.

