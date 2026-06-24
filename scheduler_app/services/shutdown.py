""" Shutdown operations for the scheduler app. """

from __future__ import annotations

import signal
import time

_shutdown_requested = False


def _handle_shutdown_signal(signum: int, frame: object | None) -> None:
    del signum, frame
    global _shutdown_requested
    _shutdown_requested = True


def install_shutdown_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def shutdown_requested() -> bool:
    return _shutdown_requested


def reset_shutdown_flag() -> None:
    global _shutdown_requested
    _shutdown_requested = False


def interruptible_sleep(seconds: float) -> bool:
    """Sleep up to ``seconds``; return True when shutdown was requested."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if shutdown_requested():
            return True
        time.sleep(min(0.25, deadline - time.monotonic()))
    return shutdown_requested()
