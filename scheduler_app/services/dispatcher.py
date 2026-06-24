""" Dispatch operations for the scheduler app. """

from __future__ import annotations

from dataclasses import dataclass

from .claiming import claim_runnable_executions
from .clock import Clock, SystemClock
from .executors import ExecutorBackend, executor_from_settings
from .worker import execute_claimed_execution


@dataclass
class DispatchResult:
    claimed: int
    completed: int


def dispatch_once(
    *,
    worker_id: str,
    limit: int,
    clock: Clock | None = None,
    executor: ExecutorBackend | None = None,
) -> DispatchResult:
    clock = clock or SystemClock()
    executor = executor or executor_from_settings()
    claimed = claim_runnable_executions(worker_id=worker_id, now=clock.now(), limit=limit)
    completed = 0
    for execution in claimed:
        execute_claimed_execution(
            execution.id,
            worker_id=worker_id,
            clock=clock,
            executor=executor,
        )
        completed += 1
    return DispatchResult(claimed=len(claimed), completed=completed)

