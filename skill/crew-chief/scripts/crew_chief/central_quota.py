"""Share observed quota cooldowns across central coordinators (FR-015, FR-018, SC-004, US4/AC4).

Central coordinators observe quota events from their own CLI children and
from other coordinators sharing the same central directory. Observed
cooldowns are persisted under ``quota/<family>.json`` so that any
coordinator in the same central context holds new rounds of that family
until the latest known expiry, without blocking unrelated families or
loosening tier/budget gates.

Public API
----------
``observe(root, family, source_id, until)``
    Persist or extend a quota cooldown for ``family``.

``blocked(root, family, now)``
    True when ``family`` has an unexpired cooldown in this central context.

``cooling_families(root, now)``
    Every family with an active cooldown.

Context gate
------------
All functions are no-ops when ``CREW_CHIEF_CENTRAL_ID`` is absent, so
ordinary v0.3.4 jobs never depend on central quota state.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import write_atomic

__all__ = [
    "blocked",
    "cooling_families",
    "observe",
]


def _central_active() -> bool:
    """True when running inside a central coordinator context."""
    return bool(os.environ.get("CREW_CHIEF_CENTRAL_ID"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _quota_dir(root: Path) -> Path:
    return Path(root) / "quota"


def _family_path(root: Path, family: str) -> Path:
    safe = family.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _quota_dir(root) / f"{safe}.json"


def _read_family(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def observe(
    root: Path,
    family: str,
    source_id: str,
    until: str,
    *,
    now: datetime | None = None,
) -> None:
    """Persist or extend a quota cooldown for ``family``.

    No-op outside a central context. When the recorded expiry is already
    later than ``until``, the entry is left unchanged. ``source_id`` is
    stored for diagnostics only; it does not scope the cooldown.
    """
    if not _central_active():
        return
    if not isinstance(family, str) or not family.strip():
        return
    if not isinstance(until, str) or not until.strip():
        return
    family = family.strip()
    until_dt = _parse_iso(until)
    if until_dt is None:
        return
    path = _family_path(root, family)
    current = _read_family(path)
    if isinstance(current, dict):
        existing = _parse_iso(current.get("until"))
        if existing is not None and existing >= until_dt:
            return
    payload = {
        "schema_version": 1,
        "family": family,
        "source_id": source_id if isinstance(source_id, str) else "",
        "until": until_dt.isoformat(timespec="seconds"),
        "observed_at": (now or datetime.now(timezone.utc)).isoformat(
            timespec="seconds"
        ),
    }
    write_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def blocked(
    root: Path,
    family: str,
    now: datetime | None = None,
) -> bool:
    """True when ``family`` has an unexpired cooldown in this central context.

    Always False outside a central context or when the family file is
    missing/corrupt/expired.
    """
    if not _central_active():
        return False
    if not isinstance(family, str) or not family.strip():
        return False
    path = _family_path(root, family.strip())
    record = _read_family(path)
    if not isinstance(record, dict):
        return False
    until = _parse_iso(record.get("until"))
    if until is None:
        return False
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference < until


def cooling_families(
    root: Path,
    now: datetime | None = None,
) -> set[str]:
    """Every family with an active cooldown in this central context.

    Empty outside a central context. Corrupt or expired entries are skipped.
    """
    if not _central_active():
        return set()
    qdir = _quota_dir(root)
    if not qdir.is_dir():
        return set()
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    active: set[str] = set()
    for path in qdir.glob("*.json"):
        record = _read_family(path)
        if not isinstance(record, dict):
            continue
        family = record.get("family")
        if not isinstance(family, str) or not family.strip():
            continue
        until = _parse_iso(record.get("until"))
        if until is None:
            continue
        if reference < until:
            active.add(family.strip())
    return active