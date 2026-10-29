"""Tests for cooperative pause, resume and stop controls (T016).

Verification boundary (tasks.md):
    Pause finishes current bounded round; resume restores eligibility;
    the supervisor consumes the durable files between rounds (see
    test_central_supervisor.py); nothing here signals a process.

Proof: ``python -m pytest tests/test_central_controls.py -q``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crew_chief.central_controls import ControlError, request_control


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_root(tmp_path: Path, coord_id: str) -> Path:
    """Create the minimal central layout expected by request_control."""
    control_dir = tmp_path / "coordinators" / coord_id / "control"
    control_dir.mkdir(parents=True)
    return tmp_path


def _canonical() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# request_control
# ---------------------------------------------------------------------------


class TestRequestControl:
    def test_writes_valid_control_file(self, tmp_path: Path) -> None:
        coord = _canonical()
        root = _make_root(tmp_path, coord)
        message_id = request_control(root, coord, "pause")

        path = root / "coordinators" / coord / "control" / f"{message_id}.json"
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["id"] == message_id
        assert payload["command"] == "pause"
        assert payload["coordinator_id"] == coord
        assert payload["schema_version"] == 1
        assert "requested_at" in payload

    def test_resume_and_stop_accepted(self, tmp_path: Path) -> None:
        coord = _canonical()
        root = _make_root(tmp_path, coord)
        for action in ("resume", "stop"):
            mid = request_control(root, coord, action)
            payload = json.loads(
                (root / "coordinators" / coord / "control" / f"{mid}.json").read_text(
                    encoding="utf-8"
                )
            )
            assert payload["command"] == action

    def test_rejects_unknown_action(self, tmp_path: Path) -> None:
        coord = _canonical()
        root = _make_root(tmp_path, coord)
        with pytest.raises(ControlError, match="control action"):
            request_control(root, coord, "restart")

    def test_rejects_non_canonical_coordinator(self, tmp_path: Path) -> None:
        root = _make_root(tmp_path, _canonical())
        with pytest.raises(ControlError, match="canonical UUID"):
            request_control(root, "not-a-uuid", "pause")

    def test_custom_timestamp(self, tmp_path: Path) -> None:
        coord = _canonical()
        root = _make_root(tmp_path, coord)
        fixed = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        mid = request_control(root, coord, "pause", now=fixed)
        payload = json.loads(
            (root / "coordinators" / coord / "control" / f"{mid}.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["requested_at"] == fixed.isoformat(timespec="seconds")
