"""Standard names for the crew's terminals (ACC 0.12.0)."""

from __future__ import annotations

ROLES = ("engineer", "driver", "scrutineer")


def _clean(value: str, argument: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{argument}' must be a string")
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError(f"'{argument}' must not be empty")
    return cleaned


def terminal_name(slug: str, lane: str, role: str, attempt: int = 1) -> str:
    """`acc-<slug>-<lane>-<role>` for attempt 1, `...-<role>-<n>` from 2 on."""
    slug = _clean(slug, "slug")
    lane = _clean(lane, "lane")
    role = _clean(role, "role")
    if role not in ROLES:
        raise ValueError(f"'role' must be one of {', '.join(ROLES)}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("'attempt' must be an int >= 1")
    name = f"acc-{slug}-{lane}-{role}"
    return name if attempt == 1 else f"{name}-{attempt}"
