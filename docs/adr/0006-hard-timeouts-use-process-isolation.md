# ADR 0006: Hard Timeouts Use Process Isolation

## Status

Accepted

## Decision

The required executor backend runs each task in an isolated subprocess.

## Context

Python threads and normal `Future.cancel()` calls cannot reliably stop work that is already running. A scheduler needs honest timeout semantics.

## Consequences

The worker can kill a timed-out subprocess and mark the execution timed out. There is some process startup overhead, but the behavior is clear and safe for a capstone scheduler.

