# Lessons Learned

Time and concurrency are the center of the project. The scheduler cannot rely on sleeps for correctness; it needs pure schedule functions and an injectable clock.

PostgreSQL is doing real systems work here. Unique constraints prevent duplicate durable occurrences, and row locks with `SKIP LOCKED` let multiple dispatchers compete without double-claiming the same row.

`JobExecution` is the durable queue. This keeps retry state, leases, run history, output, and errors in one authoritative place.

Redis is useful for fast dashboards, but unsafe as the correctness layer for this capstone. Clearing Redis must never lose schedules or executions.

Hard timeouts require process isolation. Normal thread or future cancellation cannot reliably stop already-running Python work.

The project guarantees at-most-once durable claim per scheduled occurrence. It cannot guarantee exactly-once side effects, especially if a worker crashes after doing external work but before writing completion state.

