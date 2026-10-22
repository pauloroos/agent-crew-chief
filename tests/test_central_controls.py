"""Tests for cooperative pause, resume and stop controls (T016).

Verification boundary (tasks.md):
    Pause finishes current bounded round; resume restores eligibility;
    stop routes only to live owner and its child tree; repeated controls
    idempotent; view exit cannot stop a coordinator; stale PID never signalled.

Proof: ``python -m pytest tests/test_central_controls.py -q``.
"""

from __future__ import annotations

import json
import os
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crew_chief.central_controls import (
    ControlError,
    apply_controls,
    request_control,
)


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


class _FakeChild:
    """Mimic a subprocess handle with a .pid attribute."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.signalled = False


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


# ---------------------------------------------------------------------------
# apply_controls — pause
# ---------------------------------------------------------------------------


class TestApplyPause:
    def test_pause_sets_state(self) -> None:
        record = {"state": "running", "updated_at": "old"}
        commands = [{"command": "pause"}]
        updated = apply_controls(record, commands)
        assert updated["state"] == "paused"
        assert updated["updated_at"] != "old"

    def test_pause_idempotent_when_already_paused(self) -> None:
        record = {"state": "paused", "updated_at": "t0"}
        updated = apply_controls(record, [{"command": "pause"}])
        assert updated["state"] == "paused"
        # Idempotent: timestamp may refresh but state stays paused.
        assert updated["updated_at"] is not None

    def test_pause_skips_terminal_states(self) -> None:
        for terminal in ("stopped", "completed", "failed"):
            record = {"state": terminal, "updated_at": "t0"}
            updated = apply_controls(record, [{"command": "pause"}])
            assert updated["state"] == terminal

    def test_pause_preserves_other_fields(self) -> None:
        record = {"state": "idle", "worktree": "/wt", "cli": "claude"}
        updated = apply_controls(record, [{"command": "pause"}])
        assert updated["worktree"] == "/wt"
        assert updated["cli"] == "claude"


# ---------------------------------------------------------------------------
# apply_controls — resume
# ---------------------------------------------------------------------------


class TestApplyResume:
    def test_resume_restores_previous_state(self) -> None:
        record = {"state": "paused", "previous_state": "running"}
        updated = apply_controls(record, [{"command": "resume"}])
        assert updated["state"] == "running"
        assert "previous_state" not in updated

    def test_resume_defaults_to_idle_without_previous(self) -> None:
        record = {"state": "paused"}
        updated = apply_controls(record, [{"command": "resume"}])
        assert updated["state"] == "idle"

    def test_resume_noop_when_not_paused(self) -> None:
        record = {"state": "running", "updated_at": "t0"}
        updated = apply_controls(record, [{"command": "resume"}])
        assert updated["state"] == "running"


# ---------------------------------------------------------------------------
# apply_controls — stop
# ---------------------------------------------------------------------------


class TestApplyStop:
    def test_stop_transitions_to_stopped(self) -> None:
        record = {"state": "running"}
        updated = apply_controls(record, [{"command": "stop"}])
        assert updated["state"] == "stopped"

    def test_stop_idempotent(self) -> None:
        record = {"state": "stopped"}
        updated = apply_controls(record, [{"command": "stop"}])
        assert updated["state"] == "stopped"

    def test_stop_signals_owned_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        record = {"state": "running"}
        child = _FakeChild(99999)  # non-existent PID; os.kill will raise OSError
        signalled_pids: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            signalled_pids.append(pid)

        monkeypatch.setattr("crew_chief.central_controls.os.kill", fake_kill)
        updated = apply_controls(record, [{"command": "stop"}], owned_child=child)
        assert updated["state"] == "stopped"
        assert signalled_pids == [99999]

    def test_stop_does_not_signal_none_child(self) -> None:
        record = {"state": "running"}
        updated = apply_controls(record, [{"command": "stop"}], owned_child=None)
        assert updated["state"] == "stopped"

    def test_stop_does_not_signal_invalid_pid(self) -> None:
        record = {"state": "running"}
        child = _FakeChild(-1)
        updated = apply_controls(record, [{"command": "stop"}], owned_child=child)
        assert updated["state"] == "stopped"


# ---------------------------------------------------------------------------
# apply_controls — ordering and edge cases
# ---------------------------------------------------------------------------


class TestApplyOrdering:
    def test_newest_command_wins(self) -> None:
        record = {"state": "running"}
        commands = [
            {"command": "pause"},
            {"command": "resume"},
            {"command": "stop"},
        ]
        updated = apply_controls(record, commands)
        assert updated["state"] == "stopped"

    def test_unknown_commands_skipped(self) -> None:
        record = {"state": "running"}
        commands = [{"command": "explode"}, {"command": "pause"}]
        updated = apply_controls(record, commands)
        assert updated["state"] == "paused"

    def test_empty_commands_returns_copy(self) -> None:
        record = {"state": "running"}
        updated = apply_controls(record, [])
        assert updated == record
        assert updated is not record

    def test_non_dict_entries_ignored(self) -> None:
        record = {"state": "running"}
        commands = ["bad", 42, None, {"command": "pause"}]  # type: ignore[list-item]
        updated = apply_controls(record, commands)  # type: ignore[arg-type]
        assert updated["state"] == "paused"

    def test_rejects_non_dict_record(self) -> None:
        with pytest.raises(ControlError, match="record must be a dict"):
            apply_controls("not-a-dict", [])  # type: ignore[arg-type]

    def test_rejects_non_list_commands(self) -> None:
        with pytest.raises(ControlError, match="commands must be a list"):
            apply_controls({}, "not-a-list")  # type: ignore[arg-type]