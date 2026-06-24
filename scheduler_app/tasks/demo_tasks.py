""" Demo tasks for the scheduler app """

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from django.conf import settings

from .registry import TaskContext, register_task


@register_task(
    name="sleep_for_seconds",
    description="Sleep for a bounded number of seconds to demonstrate workers and timeout handling.",
    expected_config={"seconds": "Number of seconds to sleep, default 1, max 300"},
    idempotent=True,
    safe_for_demo=True,
)
def sleep_for_seconds(config: dict, context: TaskContext) -> str:
    seconds = min(float(config.get("seconds", 1)), 300.0)
    time.sleep(seconds)
    return f"Slept for {seconds:g} seconds"


@register_task(
    name="always_succeed",
    description="Return successfully with a short message.",
    idempotent=True,
    safe_for_demo=True,
)
def always_succeed(config: dict, context: TaskContext) -> str:
    return f"Task succeeded on attempt {context.attempt_number}"


@register_task(
    name="always_fail",
    description="Raise an exception every time to demonstrate retries and dead letters.",
    idempotent=True,
    safe_for_demo=True,
)
def always_fail(config: dict, context: TaskContext) -> str:
    raise RuntimeError("Intentional demo failure")


@register_task(
    name="fail_once_then_succeed",
    description="Fail the first time for an idempotency key and succeed on later attempts.",
    expected_config={"key": "Optional stable key. Defaults to execution idempotency key."},
    idempotent=True,
    safe_for_demo=True,
)
def fail_once_then_succeed(config: dict, context: TaskContext) -> str:
    raw_key = str(config.get("key") or context.idempotency_key)
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_key)
    marker_dir = Path(settings.BASE_DIR) / "artifacts" / "fail_once_markers"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{safe_key}.json"
    if not marker.exists():
        marker.write_text(json.dumps({"failed": True}), encoding="utf-8")
        raise RuntimeError("Intentional first-attempt failure")
    marker.unlink(missing_ok=True)
    return "Succeeded after the first failure"


@register_task(
    name="generate_report",
    description="Write a tiny demo report under reports/ and return its path.",
    expected_config={"name": "Optional report file stem"},
    idempotent=True,
    safe_for_demo=True,
)
def generate_report(config: dict, context: TaskContext) -> str:
    report_dir = Path(settings.BASE_DIR) / "reports"
    report_dir.mkdir(exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(config.get("name") or context.idempotency_key))
    path = report_dir / f"{stem}.txt"
    path.write_text(
        "\n".join(
            [
                "Task Scheduler Demo Report",
                f"execution_id={context.execution_id}",
                f"attempt_number={context.attempt_number}",
                f"scheduled_for={context.scheduled_for}",
            ]
        ),
        encoding="utf-8",
    )
    return f"Generated report at {path}"


@register_task(
    name="cleanup_old_runs",
    description="Prune run history according to per-job retention policy.",
    idempotent=True,
    safe_for_demo=True,
)
def cleanup_old_runs(config: dict, context: TaskContext) -> str:
    from scheduler_app.services.retention import prune_all_jobs

    deleted = prune_all_jobs()
    return f"Pruned {deleted} execution rows"


@register_task(
    name="write_file_artifact",
    description="Write a bounded text artifact under artifacts/.",
    expected_config={"file_name": "Optional safe file name", "content": "Text content"},
    idempotent=True,
    safe_for_demo=True,
)
def write_file_artifact(config: dict, context: TaskContext) -> str:
    artifact_dir = Path(settings.BASE_DIR) / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    raw_name = str(config.get("file_name") or f"execution-{context.execution_id}.txt")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name)
    path = artifact_dir / safe_name
    content = str(config.get("content") or f"artifact from execution {context.execution_id}")
    path.write_text(content[:100_000], encoding="utf-8")
    return f"Wrote artifact at {path}"


@register_task(
    name="http_health_check_local",
    description="Fetch a local HTTP endpoint and report the status code.",
    expected_config={"url": "Local URL, default http://127.0.0.1:8000/healthz"},
    idempotent=True,
    safe_for_demo=True,
)
def http_health_check_local(config: dict, context: TaskContext) -> str:
    url = str(config.get("url") or "http://127.0.0.1:8000/healthz")
    if not (url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
        raise ValueError("http_health_check_local only accepts localhost/127.0.0.1 URLs")
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read(512).decode("utf-8", errors="replace")
        return f"HTTP {response.status}: {body[:120]}"

