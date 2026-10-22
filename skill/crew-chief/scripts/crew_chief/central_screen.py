"""Refreshing terminal screen for the central coordinator view (FR-010, FR-012, FR-020, FR-026, SC-005).

This module drives the interactive loop: it polls the shared snapshot via
:func:`central_snapshot.snapshot`, renders it through
:func:`central_terminal.render`, reads keys via :mod:`central_keys` and
routes actions (attach, message, pause/resume, details, quit).  When the
terminal is redirected or ``--plain`` / ``--once`` is requested it prints
one finite snapshot instead of entering the refresh loop.

Public API
----------
``run_screen(root, options, io, clock) -> int``
    Entry point used by ``acc central`` CLI handlers.  Returns an exit code.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Protocol

from . import (
    central_attach,
    central_controls,
    central_messages,
    central_snapshot,
    central_terminal,
)
from .central_keys import (
    KEY_DETAILS,
    KEY_DOWN,
    KEY_ENTER,
    KEY_ESC,
    KEY_MESSAGE,
    KEY_PAUSE,
    KEY_QUIT,
    KEY_UP,
    read_key,
    terminal_input,
    terminal_size,
)

__all__ = ["run_screen"]

_DEFAULT_POLL_S = 1.0


class _Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class _RealClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


class _IO(Protocol):
    stdin: Any
    stdout: Any


class _StdIO:
    # Read at use time: the streams may be replaced after import.
    @property
    def stdin(self) -> Any:
        return sys.stdin

    @property
    def stdout(self) -> Any:
        return sys.stdout


def _snapshot(root: Path, now: Any) -> dict:
    try:
        return central_snapshot.snapshot(root=root, now=now)
    except Exception:
        return {"rows": [], "questions": [], "generated_at": str(now), "error": "snapshot failed"}


def _render(snap: dict, selected: int, details: bool, plain: bool, width: int | None) -> str:
    try:
        return central_terminal.render(
            snap,
            selected=selected,
            details=details,
            plain=plain,
            width=width,
        )
    except Exception:
        return "render error\n"


def _write(out: Any, text: str) -> None:
    try:
        out.write(text)
        out.flush()
    except Exception:
        pass


def _clear_and_draw(out: Any, text: str, plain: bool) -> None:
    if plain:
        _write(out, text)
        return
    # Move cursor home + clear screen before each frame.
    _write(out, "\x1b[H\x1b[2J")
    _write(out, text)


def _row_count(snap: dict) -> int:
    rows = snap.get("rows") if isinstance(snap, dict) else None
    if isinstance(rows, list):
        return len(rows)
    return 0


def _clamp_selected(selected: int, count: int) -> int:
    if count <= 0:
        return 0
    if selected < 0:
        return 0
    if selected >= count:
        return count - 1
    return selected


def _handle_message(root: Path, snap: dict, selected: int, io: _IO) -> None:
    """Send a canned message/reply to the selected coordinator's inbox."""
    rows = snap.get("rows") if isinstance(snap, dict) else None
    if not isinstance(rows, list) or selected < 0 or selected >= len(rows):
        return
    row = rows[selected]
    if not isinstance(row, dict):
        return
    cid = row.get("coordinator_id") or row.get("id")
    if not cid:
        return
    questions = snap.get("questions") if isinstance(snap, dict) else []
    reply_to = None
    if isinstance(questions, list):
        for q in questions:
            if isinstance(q, dict) and q.get("coordinator_id") == cid:
                reply_to = q.get("id")
                break
    try:
        central_messages.send(root, str(cid), central_messages.owner_message(str(cid), "ack", reply_to))
    except Exception:
        pass


def _handle_pause_resume(root: Path, snap: dict, selected: int) -> None:
    rows = snap.get("rows") if isinstance(snap, dict) else None
    if not isinstance(rows, list) or selected < 0 or selected >= len(rows):
        return
    row = rows[selected]
    if not isinstance(row, dict):
        return
    cid = row.get("coordinator_id") or row.get("id")
    if not cid:
        return
    state = str(row.get("state", "")).lower()
    action = "resume" if state in ("paused", "suspended") else "pause"
    try:
        central_controls.request_control(root, str(cid), action)
    except Exception:
        pass


def _handle_attach(root: Path, snap: dict, selected: int, io: _IO) -> None:
    rows = snap.get("rows") if isinstance(snap, dict) else None
    if not isinstance(rows, list) or selected < 0 or selected >= len(rows):
        return
    row = rows[selected]
    if not isinstance(row, dict):
        return
    cid = row.get("coordinator_id") or row.get("id")
    if not cid:
        return
    try:
        central_attach.attach(root, str(cid), io, timeout_s=5.0)
    except Exception:
        pass


def run_screen(
    root: Path,
    options: dict | None = None,
    io: _IO | None = None,
    clock: _Clock | None = None,
) -> int:
    """Run the central terminal screen.  Returns 0 on clean exit."""
    opts = options or {}
    once = bool(opts.get("once"))
    plain = bool(opts.get("plain"))
    poll_s = float(opts.get("poll", _DEFAULT_POLL_S) or _DEFAULT_POLL_S)
    if poll_s <= 0:
        poll_s = _DEFAULT_POLL_S

    io_obj: _IO = io if io is not None else _StdIO()
    clk: _Clock = clock if clock is not None else _RealClock()

    # Redirected / non-TTY → one finite snapshot, no input loop.
    supported = True
    stdin = getattr(io_obj, "stdin", None)
    stdout = getattr(io_obj, "stdout", None)
    if stdin is not None and hasattr(stdin, "isatty"):
        try:
            if not stdin.isatty():
                supported = False
        except Exception:
            supported = False
    if stdout is not None and hasattr(stdout, "isatty"):
        try:
            if not stdout.isatty():
                supported = False
        except Exception:
            supported = False

    if once or not supported:
        snap = _snapshot(root, None)
        cols = None
        try:
            cols = terminal_size()[0]
        except Exception:
            cols = 80
        text = _render(snap, selected=0, details=False, plain=True, width=cols)
        _write(getattr(io_obj, "stdout", sys.stdout), text)
        return 0

    selected = 0
    show_details = False
    paused = False

    with terminal_input(stream=io_obj):
        while True:
            snap = _snapshot(root, None)
            count = _row_count(snap)
            selected = _clamp_selected(selected, count)

            cols = None
            try:
                cols = terminal_size()[0]
            except Exception:
                cols = 80

            frame = _render(snap, selected=selected, details=show_details, plain=plain, width=cols)
            _clear_and_draw(getattr(io_obj, "stdout", sys.stdout), frame, plain)

            key = read_key(timeout_s=poll_s)
            if key is None:
                continue
            if key == KEY_QUIT:
                return 0
            if key == KEY_UP:
                selected = max(0, selected - 1)
                continue
            if key == KEY_DOWN:
                selected = min(max(count - 1, 0), selected + 1)
                continue
            if key == KEY_DETAILS:
                show_details = not show_details
                continue
            if key == KEY_ESC:
                show_details = False
                continue
            if key == KEY_MESSAGE:
                _handle_message(root, snap, selected, io_obj)
                continue
            if key == KEY_PAUSE:
                _handle_pause_resume(root, snap, selected)
                continue
            if key == KEY_ENTER:
                _handle_attach(root, snap, selected, io_obj)
                continue