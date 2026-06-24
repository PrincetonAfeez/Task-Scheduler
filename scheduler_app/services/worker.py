""" Worker operations for the scheduler app. """

from __future__ import annotations

import os
import socket
import threading

from django.db import transaction

from scheduler_app.models import EventType, ExecutionStatus, JobExecution
from scheduler_app.tasks.registry import TaskContext

from .cache import invalidate_scheduler_cache
from .clock import Clock, SystemClock
from .shutdown import interruptible_sleep, shutdown_requested
from .events import emit_event
from .claiming import claim_runnable_executions
from .executors import ExecutorBackend, executor_from_settings
from .health import update_worker_heartbeat
from .retries import apply_failure_transition


def default_worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def thread_worker_id(base_worker_id: str) -> str:
    return f"{base_worker_id}-t{threading.get_ident()}"


def _context_for_execution(execution: JobExecution, worker_id: str) -> TaskContext:
    return TaskContext(
        execution_id=execution.id,
        job_id=execution.job_id,
        attempt_number=execution.attempt_number,
        idempotency_key=execution.idempotency_key,
        scheduled_for=execution.scheduled_for,
        worker_id=worker_id,
    )


def execute_claimed_execution(
    execution_id: int,
    *,
    worker_id: str,
    clock: Clock | None = None,
    executor: ExecutorBackend | None = None,
) -> JobExecution:
    clock = clock or SystemClock()
    executor = executor or executor_from_settings()
    now = clock.now()

    with transaction.atomic():
        execution = JobExecution.objects.select_for_update().select_related("job").get(pk=execution_id)
        if execution.status != ExecutionStatus.CLAIMED or execution.worker_id != worker_id:
            emit_event(
                EventType.STALE_CLAIM_REJECTED,
                job=execution.job,
                execution=execution,
                message=f"{worker_id} rejected stale claim on execution {execution.id}",
                data={"current_status": execution.status, "current_worker": execution.worker_id},
            )
            return execution
        execution.status = ExecutionStatus.RUNNING
        execution.started_at = now
        execution.worker_id = worker_id
        execution.save(update_fields=["status", "started_at", "worker_id", "updated_at"])
        emit_event(
            EventType.WORKER_START,
            job=execution.job,
            execution=execution,
            message=f"{worker_id} started execution {execution.id}",
        )

    update_worker_heartbeat(
        worker_id=worker_id,
        now=now,
        active_execution_count=1,
        health_state="running",
        current_execution_id=execution.id,
    )

    result = executor.run(
        task_name=execution.job.registered_task_name,
        config=execution.job.task_config,
        context=_context_for_execution(execution, worker_id),
        timeout_seconds=execution.job.timeout_seconds,
    )
    finish = clock.now()

    with transaction.atomic():
        execution = JobExecution.objects.select_for_update().select_related("job").get(pk=execution_id)
        if execution.status != ExecutionStatus.RUNNING or execution.worker_id != worker_id:
            # The lease expired mid-run and recovery requeued it, or another worker
            # re-claimed it. Discard this result rather than clobbering the new owner.
            emit_event(
                EventType.STALE_RESULT_DISCARDED,
                job=execution.job,
                execution=execution,
                message=f"{worker_id} discarded stale result for execution {execution.id}",
                data={"current_status": execution.status, "current_worker": execution.worker_id},
            )
            return execution
        execution.finished_at = finish
        execution.duration_ms = result.duration_ms
        execution.output = result.output[:20_000]
        execution.error = result.error[:20_000]
        execution.lease_expires_at = None
        execution.claimed_by = ""
        execution.claimed_at = None
        if result.status == ExecutionStatus.SUCCEEDED:
            execution.status = ExecutionStatus.SUCCEEDED
            execution.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "duration_ms",
                    "output",
                    "error",
                    "lease_expires_at",
                    "claimed_by",
                    "claimed_at",
                    "updated_at",
                ]
            )
            execution.job.last_run_at = finish
            execution.job.save(update_fields=["last_run_at", "updated_at"])
            emit_event(
                EventType.WORKER_FINISH,
                job=execution.job,
                execution=execution,
                message=f"{worker_id} finished execution {execution.id}",
                data={"duration_ms": result.duration_ms},
            )
            invalidate_scheduler_cache("execution completed", job=execution.job, execution=execution)
        else:
            execution.status = (
                ExecutionStatus.TIMED_OUT
                if result.status == ExecutionStatus.TIMED_OUT
                else ExecutionStatus.FAILED
            )
            execution.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "duration_ms",
                    "output",
                    "error",
                    "lease_expires_at",
                    "claimed_by",
                    "claimed_at",
                    "updated_at",
                ]
            )
            emit_event(
                EventType.TIMEOUT if execution.status == ExecutionStatus.TIMED_OUT else EventType.FAILURE,
                job=execution.job,
                execution=execution,
                message=execution.error[:500],
                data={"duration_ms": result.duration_ms, "attempt_number": execution.attempt_number},
            )
            execution = apply_failure_transition(
                execution,
                terminal_status=execution.status,
                error=execution.error,
                now=finish,
            )

    succeeded = execution.status == ExecutionStatus.SUCCEEDED
    update_worker_heartbeat(
        worker_id=worker_id,
        now=finish,
        active_execution_count=0,
        completed_delta=1 if succeeded else 0,
        failed_delta=0 if succeeded else 1,
        health_state="idle",
        current_execution_id=None,
    )
    return execution


def _worker_thread_loop(
    *,
    base_worker_id: str,
    clock: Clock,
    executor: ExecutorBackend,
    sleep_seconds: float,
    stop_after: int | None,
    completed_counter: list[int],
    counter_lock: threading.Lock,
) -> None:
    worker_id = thread_worker_id(base_worker_id)
    while True:
        if shutdown_requested():
            return
        with counter_lock:
            if stop_after is not None and completed_counter[0] >= stop_after:
                return
        claimed = claim_runnable_executions(worker_id=worker_id, now=clock.now(), limit=1)
        if not claimed:
            update_worker_heartbeat(
                worker_id=worker_id,
                now=clock.now(),
                active_execution_count=0,
                health_state="idle",
                current_execution_id=None,
            )
            if interruptible_sleep(sleep_seconds):
                return
            continue
        for execution in claimed:
            execute_claimed_execution(
                execution.id,
                worker_id=worker_id,
                clock=clock,
                executor=executor,
            )
            with counter_lock:
                completed_counter[0] += 1


def run_worker_loop(
    *,
    worker_id: str | None = None,
    clock: Clock | None = None,
    executor: ExecutorBackend | None = None,
    workers: int = 1,
    sleep_seconds: float = 1.0,
    stop_after: int | None = None,
) -> int:
    from concurrent.futures import ThreadPoolExecutor, wait

    base_worker_id = worker_id or default_worker_id()
    clock = clock or SystemClock()
    executor = executor or executor_from_settings()
    worker_count = max(workers, 1)
    completed_counter = [0]
    counter_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(
                _worker_thread_loop,
                base_worker_id=base_worker_id,
                clock=clock,
                executor=executor,
                sleep_seconds=sleep_seconds,
                stop_after=stop_after,
                completed_counter=completed_counter,
                counter_lock=counter_lock,
            )
            for _ in range(worker_count)
        ]
        wait(futures)
    return completed_counter[0]
