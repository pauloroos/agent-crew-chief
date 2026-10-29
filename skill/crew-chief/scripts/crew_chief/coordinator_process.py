"""Coordinator process groups and exit classification.

The coordinator runs in its own process group so every child can be reaped
together on stop, round exit or timeout; :func:`classify_exit` tells a quota
or server failure from a crash. Only stdlib is used.
"""
from __future__ import annotations

import errno
import os
import re
import signal
import subprocess
from typing import Any

__all__ = ["classify_exit"]

#: Patterns that mark a failure as quota/rate-limit related (mirrors tools.py).
_QUOTA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"429", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
]

#: Patterns that mark a failure as an HTTP 5xx server error.
_SERVER_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"HTTP\s+5\d{2}", re.IGNORECASE),
]

#: Patterns that mark a connection reset.
_CONNECTION_RESET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"connection reset", re.IGNORECASE),
]


def classify_exit(
    returncode: int | None,
    stderr: str,
    *,
    last_output_age: float | None = None,
    coordinator_stall_s: float | None = None,
) -> str | None:
    """Classify a coordinator exit into an error category.

    Returns one of ``"quota"``, ``"server_error"``, ``"connection_reset"``,
    ``"crash"``, ``"stall"``, or ``None`` for a clean exit.

    Parameters
    ----------
    returncode:
        Process exit code, or ``None`` when the process is still running
        (used for stall detection).
    stderr:
        Captured standard-error text from the coordinator.
    last_output_age:
        Seconds since the coordinator last produced output.
    coordinator_stall_s:
        Threshold in seconds; when *last_output_age* exceeds this value the
        coordinator is considered stalled.
    """
    # Stall detection takes priority when the process is still alive.
    if (
        returncode is None
        and last_output_age is not None
        and coordinator_stall_s is not None
        and last_output_age > coordinator_stall_s
    ):
        return "stall"

    # Clean exit.
    if returncode == 0:
        return None

    text = stderr or ""

    for pat in _QUOTA_PATTERNS:
        if pat.search(text):
            return "quota"

    for pat in _SERVER_ERROR_PATTERNS:
        if pat.search(text):
            return "server_error"

    for pat in _CONNECTION_RESET_PATTERNS:
        if pat.search(text):
            return "connection_reset"

    # Non-zero exit with no recognised pattern → crash.
    if returncode is not None and returncode != 0:
        return "crash"

    return None


def _safe_killpg(
    pgid: int,
    sig: int,
    proc: subprocess.Popen | Any = None,
) -> None:
    """Send *sig* to *pgid*, ignoring ESRCH/EPERM when the group is already gone.

    On macOS, ``os.killpg`` on a group whose leader has already exited (and was
    reaped or is a zombie) raises ``EPERM`` instead of ``ESRCH``.  We treat that
    as "group already gone" only when the group leader has already exited
    (checked via *proc.poll()* or *pid_alive*).  If the leader is still alive,
    ``PermissionError`` is re-raised.

    On Windows, ``os.killpg`` does not exist, so terminate process tree via taskkill.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pgid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
        return

    if not hasattr(os, "killpg"):
        if proc is not None:
            try:
                sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                if sig == sigkill:
                    proc.kill()
                else:
                    proc.terminate()
            except OSError:
                pass
        return

    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS: group leader exited; treat as gone only if leader has exited.
        if proc is not None and proc.poll() is not None:
            return
        from crew_chief._os_compat import pid_alive

        if not pid_alive(pgid):
            return
        raise
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            pass
        elif exc.errno == errno.EPERM:
            if proc is not None and proc.poll() is not None:
                return
            from crew_chief._os_compat import pid_alive

            if not pid_alive(pgid):
                return
            raise
        else:
            raise


def _pgid_alive(pgid: int) -> bool:
    """Return ``True`` when at least one process in *pgid* still exists."""
    if not hasattr(os, "killpg") or os.name == "nt":
        from crew_chief._os_compat import pid_alive

        return pid_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Group exists but belongs to another user.
        return True
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.EINVAL):
            return False
        raise
    return True
