""" Executor operations for the scheduler app. """

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from django.conf import settings

from scheduler_app.models import ExecutionStatus
from scheduler_app.tasks.registry import TaskContext, get_task


@dataclass(frozen=True)
class TaskRunResult:
    status: str
    output: str = ""
    error: str = ""
    duration_ms: int = 0


class ExecutorBackend(Protocol):
    def run(
        self,
        *,
        task_name: str,
        config: dict,
        context: TaskContext,
        timeout_seconds: int,
    ) -> TaskRunResult:
        """Run a registered task and return the captured result."""


def _context_payload(context: TaskContext) -> dict:
    payload = asdict(context)
    if context.scheduled_for is not None:
        payload["scheduled_for"] = context.scheduled_for.isoformat()
    return payload


class SubprocessExecutor:
    def run(
        self,
        *,
        task_name: str,
        config: dict,
        context: TaskContext,
        timeout_seconds: int,
    ) -> TaskRunResult:
        payload = {
            "task_name": task_name,
            "config": config,
            "context": _context_payload(context),
        }
        start = time.perf_counter()
        path: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
                json.dump(payload, handle)
                path = handle.name
            env = os.environ.copy()
            env.setdefault("DJANGO_SETTINGS_MODULE", "task_scheduler.settings")
            completed = subprocess.run(
                [sys.executable, "-m", "scheduler_app.services.subprocess_runner", path],
                cwd=str(settings.BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            return TaskRunResult(
                status=ExecutionStatus.TIMED_OUT,
                output=partial[:20_000],
                error=f"Task exceeded hard timeout of {timeout_seconds} seconds",
                duration_ms=duration_ms,
            )
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

        duration_ms = int((time.perf_counter() - start) * 1000)
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if completed.returncode != 0:
            return TaskRunResult(
                status=ExecutionStatus.FAILED,
                output=stdout[:20_000],
                error=(stderr or f"subprocess exited with {completed.returncode}")[:20_000],
                duration_ms=duration_ms,
            )
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return TaskRunResult(
                status=ExecutionStatus.FAILED,
                output=stdout[:20_000],
                error=f"task runner returned invalid JSON: {stderr}",
                duration_ms=duration_ms,
            )
        return TaskRunResult(
            status=data.get("status", ExecutionStatus.FAILED),
            output=data.get("output", "")[:20_000],
            error=data.get("error", "")[:20_000],
            duration_ms=data.get("duration_ms") or duration_ms,
        )


class InProcessExecutor:
    """Testing/comparison backend. It cannot enforce hard timeouts."""

    def run(
        self,
        *,
        task_name: str,
        config: dict,
        context: TaskContext,
        timeout_seconds: int,
    ) -> TaskRunResult:
        start = time.perf_counter()
        try:
            spec = get_task(task_name)
            if spec.func is None:
                raise RuntimeError(f"task has no callable: {task_name}")
            output = spec.func(config, context)
            return TaskRunResult(
                status=ExecutionStatus.SUCCEEDED,
                output=str(output),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - user demo tasks intentionally raise
            return TaskRunResult(
                status=ExecutionStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )


def executor_from_settings() -> ExecutorBackend:
    backend = getattr(settings, "EXECUTOR_BACKEND", "subprocess")
    if backend == "inprocess":
        return InProcessExecutor()
    return SubprocessExecutor()

