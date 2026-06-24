""" Subprocess runner for the scheduler app. """

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "task_scheduler.settings")


def main() -> int:
    import django

    django.setup()

    from scheduler_app.models import ExecutionStatus
    from scheduler_app.tasks.registry import TaskContext, get_task

    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    context_data = payload["context"]
    scheduled_for = context_data.get("scheduled_for")
    if scheduled_for:
        context_data["scheduled_for"] = datetime.fromisoformat(scheduled_for)
    context = TaskContext(**context_data)

    output_stream = io.StringIO()
    error_stream = io.StringIO()
    start = time.perf_counter()
    try:
        task = get_task(payload["task_name"])
        if task.func is None:
            raise RuntimeError(f"task has no callable: {payload['task_name']}")
        with contextlib.redirect_stdout(output_stream), contextlib.redirect_stderr(error_stream):
            returned = task.func(payload.get("config") or {}, context)
        output = output_stream.getvalue()
        if returned is not None:
            output = f"{output}{returned}"
        result = {
            "status": ExecutionStatus.SUCCEEDED,
            "output": output,
            "error": error_stream.getvalue(),
            "duration_ms": int((time.perf_counter() - start) * 1000),
        }
    except Exception:  # noqa: BLE001 - task failures are part of the scheduler domain
        result = {
            "status": ExecutionStatus.FAILED,
            "output": output_stream.getvalue(),
            "error": f"{error_stream.getvalue()}{traceback.format_exc()}",
            "duration_ms": int((time.perf_counter() - start) * 1000),
        }
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

