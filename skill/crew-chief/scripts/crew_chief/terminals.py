"""Standard names for the crew's terminals (ACC 0.12.0; engineer without job since 0.12.1)."""

from __future__ import annotations

import pathlib
import re

ROLES = ("engineer", "driver", "scrutineer")

_CLEAN_RE = re.compile(r"[a-z0-9-]+")


def _clean(value: str, argument: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{argument}' must be a string")
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError(f"'{argument}' must not be empty")
    if _CLEAN_RE.fullmatch(cleaned) is None:
        raise ValueError(f"'{argument}' must use only [a-z0-9-]")
    return cleaned


def terminal_name(slug: str, lane: str, job: str | None, role: str, attempt: int = 1) -> str:
    """`acc-<slug>-<lane>[-<job>]-<role>` for attempt 1, `...-<role>-<n>` from 2 on.

    The engineer covers the whole lane, so it takes no job (`job=None`);
    drivers and scrutineers require one.
    """
    slug = _clean(slug, "slug")
    lane = _clean(lane, "lane")
    role = _clean(role, "role")
    if role not in ROLES:
        raise ValueError(f"'role' must be one of {', '.join(ROLES)}")
    if role == "engineer":
        if job is not None:
            raise ValueError("'job' must be None for the engineer (one per lane)")
    elif job is None:
        raise ValueError("'job' is required for driver and scrutineer")
    else:
        job = _clean(job, "job")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("'attempt' must be an int >= 1")
    name = f"acc-{slug}-{lane}-{role}" if job is None else f"acc-{slug}-{lane}-{job}-{role}"
    return name if attempt == 1 else f"{name}-{attempt}"


def log_file(logs_dir, name: str) -> pathlib.Path:
    """`<logs_dir>/<name>.log`, the standard log path in background mode.

    Orca-mode workers produce no `.log` files; only background mode writes them.
    """
    return pathlib.Path(logs_dir) / f"{name}.log"