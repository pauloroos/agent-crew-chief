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
    central_controls,
    central_messages,
    central_snapshot,
    central_terminal,
)
from .central_backend import ATTACH_TIMEOUT_KEY, ON_OPEN_KEY, TERMINAL_KEY
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

__all__ = ["run_screen", "make_notice_provider"]

_DEFAULT_POLL_S = 1.0

_NOTICE_PROVIDER_KEYS = ("notice_provider",)


def _current_version() -> str:
    try:
        from . import __version__ as version

        return str(version)
    except Exception:
        return "0.4.2"


# Sentinel returned when loading preferences fails (invalid value/file).
_PREFS_LOAD_FAILED = object()


def _load_prefs_quietly(project):
    if project is None:
        return None
    try:
        from . import config as _config

        return _config.load_prefs(Path(project))
    except Exception:
        return _PREFS_LOAD_FAILED


def make_notice_provider(
    project=None,
    *,
    prefs=None,
    config_dir=None,
    current=None,
    now=None,
    enabled=None,
):
    """Build a read-only cached-update-notice provider (T005).

    The returned callable reads only the validated cache; it never performs
    network I/O, scheduling or preference writes. ``enabled`` follows the
    effective project preference (strict boolean, default true).
    """
    if prefs is None:
        prefs = _load_prefs_quietly(project)
    if enabled is None:
        if prefs is _PREFS_LOAD_FAILED:
            enabled = False
        elif prefs is None:
            enabled = True
        else:
            enabled = getattr(prefs, "check_updates", None) is True
    fixed_config_dir = None if config_dir is None else Path(config_dir)
    # Resolve the current version lazily when not explicitly supplied so a
    # monkeypatched package version is honoured by the cache read.
    explicit_current = current

    def _config_dir():
        if fixed_config_dir is not None:
            return fixed_config_dir
        try:
            from . import config as _config

            return _config.default_global_path().parent
        except Exception:
            return None

    def provider():
        if enabled is not True:
            return None
        root_dir = _config_dir()
        if root_dir is None:
            return None
        try:
            from . import update_cache as _cache
            from datetime import datetime, timezone

            if callable(now):
                when = now()
            elif now is not None:
                when = now
            else:
                when = datetime.now(timezone.utc)
            version = explicit_current if explicit_current is not None else _current_version()
            return _cache.cached_notice(Path(root_dir), version, True, when)
        except Exception:
            return None

    # Preserve the effective preference even when it is false: the provider
    # must stay silent and the preference must not be rewritten.
    if enabled is not True:
        def disabled_provider():
            return None

        disabled_provider.__wrapped__ = provider  # type: ignore[attr-defined]
        return disabled_provider
    return provider


def _resolve_notice_provider(options, notice_provider=None, project=None):
    opts = options if isinstance(options, dict) else ({} if options is None else {})
    try:
        opts = dict(opts)
    except Exception:
        opts = {}
    provider = notice_provider
    if provider is None:
        for key in _NOTICE_PROVIDER_KEYS:
            if key in opts:
                provider = opts[key]
                break
    if provider is None:
        proj = project if project is not None else opts.get("project")
        if proj is not None or "check_updates" in opts or "prefs" in opts or "config_dir" in opts:
            provider = make_notice_provider(
                project=proj,
                prefs=opts.get("prefs"),
                config_dir=opts.get("config_dir"),
                current=opts.get("current_version", opts.get("current")),
                enabled=opts.get("check_updates") if "check_updates" in opts else None,
                now=opts.get("now"),
            )
    if provider is not None and "notice_provider" not in opts:
        opts["notice_provider"] = provider
    return opts, provider


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


def _registered(root: Path, cid: str):
    """The stored row and backend of ``cid``, routed exactly as ``acc`` routes it (FR-017)."""
    row, backend = central_controls.registered_backend(root, cid)
    if backend is None:
        raise central_controls.ControlError(f"no coordinator {cid!r} registered")
    return row, backend


def _attach(root: Path, cid: str, io: Any, on_open: Any = None) -> dict:
    row, backend = _registered(root, cid)
    return backend.attach({**row, TERMINAL_KEY: io, ATTACH_TIMEOUT_KEY: 5.0, ON_OPEN_KEY: on_open})


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
        row, backend = _registered(root, str(cid))
        backend.send(row, central_messages.owner_message(str(cid), "ack", reply_to))
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
        _registered(root, str(cid))
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
        _attach(root, str(cid), io)
    except Exception:
        pass


def run_screen(
    root: Path,
    options: dict | None = None,
    io: _IO | None = None,
    clock: _Clock | None = None,
    *,
    notice_provider=None,
    project=None,
) -> int:
    """Run the central terminal screen.  Returns 0 on clean exit."""
    opts, resolved_provider = _resolve_notice_provider(options, notice_provider, project)
    def view_snapshot():
        # View-only data: never alter the shared snapshot or perform a check.
        snap = dict(_snapshot(root, None))
        if callable(resolved_provider):
            try:
                notice = resolved_provider()
            except Exception:
                notice = None
            if notice:
                snap["update_notice"] = notice
        return snap

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
        snap = view_snapshot()
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
            snap = view_snapshot()
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
