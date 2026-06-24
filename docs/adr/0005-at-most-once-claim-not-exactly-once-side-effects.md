# ADR 0005: At-Most-Once Claim, Not Exactly-Once Side Effects

## Status

Accepted

## Decision

The system guarantees at-most-once durable claim per scheduled occurrence. It does not claim exactly-once side effects.

## Context

Database constraints and row locks can prevent duplicate durable occurrence rows and duplicate durable claims. They cannot prove what happened in an external system if a worker crashes after performing side effects but before recording success.

## Consequences

Side-effecting registered tasks should be idempotent or documented as unsafe to retry. Operator docs and README use the precise guarantee wording.

