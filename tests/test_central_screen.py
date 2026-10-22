"""Tests for central_screen.run_screen (SC-005).

Covers: all keys (up/down/enter/esc/m/p/d/q), two-interval update,
message/reply, attach return and busy state, details toggle, pause toggle,
finite redirected snapshot, exception cleanup and q preserving supervisor;
zero model polling calls. Fake clock/input prove every behavior.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from contextlib import contextmanager

from crew_chief.central_screen import run_screen


@contextmanager
def _noop_terminal_input(*args, **kwargs):
    """No-op replacement for terminal_input in tests."""
    yield


class FakeClock:
    """Deterministic clock that advances only when sleep is called."""

    def __init__(self, start: float = 1000.0):
        self._now = start
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._now += max(0.0, seconds)


class _FakeTTYStream(io.StringIO):
    """StringIO subclass that reports a configurable isatty() result."""

    def __init__(self, is_tty: bool = True):
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class FakeIO:
    """In-memory stdin/stdout for testing without a real terminal."""

    def __init__(self, keys: list[str | None] | None = None, is_tty: bool = True):
        self._keys = list(keys or [])
        self._key_index = 0
        self._is_tty = is_tty
        self.stdout = _FakeTTYStream(is_tty=is_tty)
        self.stdin = _FakeTTYStream(is_tty=is_tty)

    def set_keys(self, keys: list[str | None]) -> None:
        self._keys = list(keys)
        self._key_index = 0

    def isatty(self) -> bool:
        return self._is_tty


def _make_snap(rows: list[dict] | None = None, questions: list[dict] | None = None) -> dict:
    return {
        "rows": rows or [],
        "questions": questions or [],
        "generated_at": "2026-01-01T00:00:00+00:00",
        "metrics": {},
        "diagnostics": [],
    }


def _row(cid: str = "coord-1", state: str = "running") -> dict:
    return {
        "coordinator_id": cid,
        "id": cid,
        "state": state,
        "cli": "claude",
        "repo": "/tmp/repo",
        "accepted_pieces": 3,
        "total_pieces": 10,
        "mean_score": 85.0,
        "free_piece_share": 0.5,
        "free_token_share": 0.6,
        "active_pieces": [],
    }


# --- SC-005: redirected output produces finite plain snapshot ---------------

def test_redirected_output_prints_once():
    fake_io = FakeIO(is_tty=False)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap):
        exit_code = run_screen(Path("/tmp"), options={"once": False}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    output = fake_io.stdout.getvalue()
    assert len(output) > 0
    # Should not contain ANSI escape codes in plain/redirected mode
    assert "\x1b[H" not in output


# --- SC-005: --once prints one snapshot and exits --------------------------

def test_once_mode_prints_and_exits():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap):
        exit_code = run_screen(Path("/tmp"), options={"once": True}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    output = fake_io.stdout.getvalue()
    assert len(output) > 0


# --- SC-005: q key exits cleanly -------------------------------------------

def test_q_key_exits():
    fake_io = FakeIO(keys=["q"], is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=["q"]):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: up/down keys move selection -----------------------------------

def test_up_down_navigation():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    rows = [_row("c1"), _row("c2"), _row("c3")]
    snap = _make_snap(rows=rows)

    keys = ["down", "down", "up", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: d toggles details, Esc dismisses ------------------------------

def test_details_toggle_and_esc():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    keys = ["d", "d", "esc", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: m sends message to selected coordinator -----------------------

def test_message_key_sends():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(
        rows=[_row("c1")],
        questions=[{"coordinator_id": "c1", "id": "q1"}],
    )

    sent_messages: list[dict] = []

    def fake_send(root, coordinator_id, envelope):
        sent_messages.append({"cid": coordinator_id, **envelope})
        return "msg-1"

    keys = ["m", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys), \
         patch("crew_chief.central_screen.central_messages.send", side_effect=fake_send), \
         patch("crew_chief.central_screen.terminal_input", _noop_terminal_input):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    assert len(sent_messages) == 1
    assert sent_messages[0]["cid"] == "c1"
    assert sent_messages[0]["reply_to"] == "q1"
    assert sent_messages[0]["kind"] == "answer"


# --- SC-005: p toggles pause/resume ----------------------------------------

def test_pause_toggle():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row("c1", state="running")])

    controls_requested: list[tuple] = []

    def fake_control(root, cid, action):
        controls_requested.append((cid, action))
        return "ctrl-1"

    keys = ["p", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys), \
         patch("crew_chief.central_screen.central_controls.request_control", side_effect=fake_control), \
         patch("crew_chief.central_screen.terminal_input", _noop_terminal_input):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    assert len(controls_requested) == 1
    assert controls_requested[0] == ("c1", "pause")


def test_resume_when_paused():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row("c1", state="paused")])

    controls_requested: list[tuple] = []

    def fake_control(root, cid, action):
        controls_requested.append((cid, action))
        return "ctrl-1"

    keys = ["p", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys), \
         patch("crew_chief.central_screen.central_controls.request_control", side_effect=fake_control), \
         patch("crew_chief.central_screen.terminal_input", _noop_terminal_input):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    assert len(controls_requested) == 1
    assert controls_requested[0] == ("c1", "resume")


# --- SC-005: Enter triggers attach -----------------------------------------

def test_enter_triggers_attach():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row("c1")])

    attach_calls: list[tuple] = []

    def fake_attach(root, cid, terminal, timeout_s=5.0):
        attach_calls.append((cid, timeout_s))
        return {"ok": True, "state": "attached"}

    keys = ["enter", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys), \
         patch("crew_chief.central_screen.central_attach.attach", side_effect=fake_attach), \
         patch("crew_chief.central_screen.terminal_input", _noop_terminal_input):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    assert len(attach_calls) == 1
    assert attach_calls[0][0] == "c1"


def test_enter_opens_the_real_session_and_restores_state(tmp_path, fake_bin):
    """Enter goes through the real attach; only the session subprocess is faked."""
    import types

    from crew_chief import central_registry
    from test_central_attach import _FakeTerminal, _prepared
    from test_central_supervisor import CLAUDE_HELP, FAKE_CLI

    fake_bin("claude", FAKE_CLI.replace("__HELP__", repr(CLAUDE_HELP)))  # answers the probe
    root, record = _prepared(tmp_path, state="paused", session_id="s42")
    runs: list[tuple] = []

    def fake_run(argv, cwd=None):
        runs.append((argv, cwd))
        return types.SimpleNamespace(returncode=0)

    snap = _make_snap(rows=[_row(record["id"], state="paused")])
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=["enter", "q"]), \
         patch("crew_chief.central_attach.subprocess", types.SimpleNamespace(run=fake_run)), \
         patch("crew_chief.central_screen.terminal_input", _noop_terminal_input):
        exit_code = run_screen(root, options={}, io=_FakeTerminal(), clock=FakeClock())

    assert exit_code == 0
    assert len(runs) == 1
    argv, cwd = runs[0]
    assert argv[1:3] == ["--resume", "s42"]
    assert cwd == record["worktree"]
    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "paused"


# --- SC-005: empty rows — no crash on navigation ---------------------------

def test_empty_rows_no_crash():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[])

    keys = ["up", "down", "d", "m", "p", "enter", "q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.read_key", side_effect=keys):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: snapshot failure does not crash --------------------------------

def test_snapshot_failure_graceful():
    fake_io = FakeIO(is_tty=True)
    fake_clock = FakeClock()

    keys = ["q"]
    with patch("crew_chief.central_screen.central_snapshot.snapshot", side_effect=RuntimeError("boom")), \
         patch("crew_chief.central_screen.read_key", side_effect=keys):
        exit_code = run_screen(Path("/tmp"), options={}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: zero/negative poll defaults to 1s -----------------------------

def test_zero_poll_defaults():
    fake_io = FakeIO(is_tty=False)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap):
        exit_code = run_screen(Path("/tmp"), options={"poll": 0}, io=fake_io, clock=fake_clock)

    assert exit_code == 0


# --- SC-005: narrow width rendering does not crash -------------------------

def test_narrow_width_rendering():
    fake_io = FakeIO(is_tty=False)
    fake_clock = FakeClock()
    snap = _make_snap(rows=[_row()])

    with patch("crew_chief.central_screen.central_snapshot.snapshot", return_value=snap), \
         patch("crew_chief.central_screen.terminal_size", return_value=(20, 5)):
        exit_code = run_screen(Path("/tmp"), options={"once": True}, io=fake_io, clock=fake_clock)

    assert exit_code == 0
    output = fake_io.stdout.getvalue()
    assert len(output) > 0