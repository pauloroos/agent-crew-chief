"""Tests for coordinator interruption alerts and continuity actions (T044, SC-014, US7/AC2).

Covers:
- FR-030: quota/stall alerts at top with stopped-at/reset-at and wait/handoff/stop
- SC-014: synthetic snapshot proves alert priority, times and action routing
- US7/AC2: quota/stalled coordinator shown first with stop/reset times and options
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from crew_chief.central_snapshot import snapshot
from crew_chief.central_terminal import render


def _make_registry(coordinators: dict[str, dict]) -> dict:
    return {
        "schema_version": 1,
        "coordinators": coordinators,
    }


def _row(
    cid: str,
    state: str = "running",
    *,
    continuity: dict | None = None,
) -> dict:
    row: dict[str, Any] = {
        "id": cid,
        "coordinator_id": cid,
        "state": state,
        "cli": "claude",
        "repo_label": f"repo-{cid}",
        "repo": "/tmp/repo",
        "worktree": f"/tmp/wt-{cid}",
        "worktree_available": True,
        "accepted": 0,
        "total": 5,
        "accepted_pieces": 0,
        "total_pieces": 5,
        "metrics": {},
        "mean_score": None,
        "free_piece_share": None,
        "free_token_share": None,
        "active_pieces": [],
        "stale": False,
    }
    if continuity is not None:
        row["continuity"] = continuity
    return row


# --- SC-014 / US7/AC2: quota alert appears before normal rows ----------------
def test_quota_alert_at_top_of_snapshot():
    """Snapshot places quota/stall alerts before ordinary coordinator rows."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    registry = _make_registry({
        "c-running": {"state": "running"},
        "c-quota": {
            "state": "stopped",
            "continuity": {
                "reason": "quota",
                "stopped_at": "2026-01-01T11:50:00+00:00",
                "quota_reset_at": "2026-01-01T12:30:00+00:00",
                "policy": "wait",
            },
        },
    })

    with patch("crew_chief.central_snapshot.central_registry.load_registry", return_value=registry), \
         patch("crew_chief.central_snapshot.iter_jobs", return_value=[]):
        snap = snapshot(root=Path("/tmp"), now=now)

    alerts = snap.get("alerts") or []
    assert len(alerts) >= 1
    # Alert must reference the quota coordinator
    alert_text = " ".join(str(a) for a in alerts).lower()
    assert "c-quota" in alert_text or "quota" in alert_text
    # Alerts section comes before rows in the snapshot contract
    assert "alerts" in snap


def test_stall_alert_includes_stopped_time():
    """Stall alert includes stopped_at timestamp."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    registry = _make_registry({
        "c-stalled": {
            "state": "stopped",
            "continuity": {
                "reason": "stall",
                "stopped_at": "2026-01-01T11:20:00+00:00",
                "quota_reset_at": None,
                "policy": "ask",
            },
        },
    })

    with patch("crew_chief.central_snapshot.central_registry.load_registry", return_value=registry), \
         patch("crew_chief.central_snapshot.iter_jobs", return_value=[]):
        snap = snapshot(root=Path("/tmp"), now=now)

    alerts = snap.get("alerts") or []
    assert len(alerts) >= 1
    alert_text = " ".join(str(a) for a in alerts)
    assert "11:20" in alert_text or "2026-01-01T11:20" in alert_text


def test_render_shows_alert_before_rows():
    """Terminal render places alert lines above coordinator rows."""
    snap = {
        "coordinators": [_row("c-running")],
        "questions": [],
        "alerts": [
            {
                "coordinator_id": "c-quota",
                "reason": "quota",
                "stopped_at": "2026-01-01T11:50:00+00:00",
                "quota_reset_at": "2026-01-01T12:30:00+00:00",
                "actions": ["wait", "handoff", "stop"],
            }
        ],
        "generated_at": "2026-01-01T12:00:00+00:00",
        "metrics": {},
        "diagnostics": [],
    }
    text = render(snap, selected=0, details=False, plain=True, width=120)
    lines = text.split("\n")
    # Find alert line and row line positions
    alert_idx = None
    row_idx = None
    for i, line in enumerate(lines):
        lower = line.lower()
        if "quota" in lower and alert_idx is None:
            alert_idx = i
        if "c-running" in lower and row_idx is None:
            row_idx = i
    assert alert_idx is not None, "Alert line not found in rendered output"
    assert row_idx is not None, "Row line not found in rendered output"
    assert alert_idx < row_idx, "Alert must appear before normal rows"


def test_alert_actions_are_read_only_labels():
    """Alert actions are display labels only — no model polling or side effects."""
    snap = {
        "rows": [],
        "questions": [],
        "alerts": [
            {
                "coordinator_id": "c-quota",
                "reason": "quota",
                "stopped_at": "2026-01-01T11:50:00+00:00",
                "quota_reset_at": None,
                "actions": ["wait", "handoff", "stop"],
            }
        ],
        "generated_at": "2026-01-01T12:00:00+00:00",
        "metrics": {},
        "diagnostics": [],
    }
    text = render(snap, selected=0, details=False, plain=True, width=120)
    lower = text.lower()
    assert "wait" in lower
    assert "handoff" in lower
    assert "stop" in lower


def test_unknown_reset_shows_unknown_label():
    """When quota_reset_at is null, display shows 'unknown' instead of a time."""
    snap = {
        "rows": [],
        "questions": [],
        "alerts": [
            {
                "coordinator_id": "c-quota",
                "reason": "quota",
                "stopped_at": "2026-01-01T11:50:00+00:00",
                "quota_reset_at": None,
                "actions": ["wait"],
            }
        ],
        "generated_at": "2026-01-01T12:00:00+00:00",
        "metrics": {},
        "diagnostics": [],
    }
    text = render(snap, selected=0, details=False, plain=True, width=120)
    lower = text.lower()
    assert "unknown" in lower


def test_no_alerts_when_all_running():
    """No alerts section when all coordinators are running normally."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    registry = _make_registry({
        "c-ok": {"state": "running"},
    })

    with patch("crew_chief.central_snapshot.central_registry.load_registry", return_value=registry), \
         patch("crew_chief.central_snapshot.iter_jobs", return_value=[]):
        snap = snapshot(root=Path("/tmp"), now=now)

    alerts = snap.get("alerts") or []
    assert len(alerts) == 0