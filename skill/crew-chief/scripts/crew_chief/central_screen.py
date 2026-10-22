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

from contextlib import contextmanager
import queue
import threading
import webbrowser
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from . import (
    central_web,
    central_controls,
    central_messages,
    central_snapshot,
    central_terminal,
)
from .central_backend import ATTACH_TIMEOUT_KEY, TERMINAL_KEY
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

__all__ = ["run_screen", "run_web"]

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


def _registered(root: Path, cid: str):
    """The stored row and backend of ``cid``, routed exactly as ``acc`` routes it (FR-017)."""
    row, backend = central_controls.registered_backend(root, cid)
    if backend is None:
        raise central_controls.ControlError(f"no coordinator {cid!r} registered")
    return row, backend


def _attach(root: Path, cid: str, io: Any) -> dict:
    row, backend = _registered(root, cid)
    return backend.attach({**row, TERMINAL_KEY: io, ATTACH_TIMEOUT_KEY: 5.0})


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


@contextmanager
def _web_bridge(server):
    if server is not None:
        server.attach_bridge_active = True
    try:
        yield
    finally:
        if server is not None:
            server.attach_bridge_active = False


def _drain_attach_intents(root, server, io):
    if server is None:
        return
    for _ in range(16):
        try:
            cid = server.attach_intents.get_nowait()
        except queue.Empty:
            return
        _attach(root, cid, io)


def run_screen(
    root: Path,
    options: dict | None = None,
    io: _IO | None = None,
    clock: _Clock | None = None,
    *,
    web_server=None,
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

    if web_server is not None:
        web_server.attach_bridge_active = False
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

    with terminal_input(stream=io_obj), _web_bridge(web_server):
        while True:
            _drain_attach_intents(root, web_server, io_obj)
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

def _is_tty(io):
    try:
        return bool(io.stdin.isatty() and io.stdout.isatty())
    except (AttributeError, OSError):
        return False


def run_web(root, options=None, io=None, clock=None, *, assets=None, port=0,
            browser_open=None, server_factory=None):
    """Own one loopback server; only the terminal input loop may attach."""
    io_obj = io if io is not None else _StdIO()
    assets = Path(assets) if assets is not None else Path(__file__).resolve().parents[2] / "web"
    factory = server_factory if server_factory is not None else central_web.create_server
    server = None
    thread = None
    started = False
    try:
        server = factory(Path(root), assets, port)
        # Serve before invoking the browser: an opener may immediately fetch.
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        started = True
        url = f"http://127.0.0.1:{server.server_port}/"
        _write(io_obj.stdout, url + "\n")
        opener = browser_open if browser_open is not None else webbrowser.open
        try:
            opened = opener(url)
        except Exception:
            opened = False
        if opened is False:
            _write(getattr(io_obj, "stderr", io_obj.stdout), "fallback: open " + url + " manually\n")
            _write(io_obj.stdout, "fallback: open " + url + " manually\n")
        if _is_tty(io_obj):
            return run_screen(Path(root), options, io=io_obj, clock=clock, web_server=server)
        # Redirected input cannot own a terminal bridge. Keep HTTP alive until
        # Ctrl-C, rather than the terminal screen's finite plain snapshot.
        while thread.is_alive():
            thread.join(.2)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        _write(getattr(io_obj, "stderr", sys.stderr), "error: web view failed\n")
        return 1
    finally:
        if server is not None:
            server.attach_bridge_active = False
            if started and thread.is_alive():
                server.shutdown()
            if started:
                thread.join(5)
            server.server_close()
