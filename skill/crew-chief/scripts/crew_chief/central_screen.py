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

import math
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Protocol

from . import (
    central_attach,
    central_controls,
    central_messages,
    central_snapshot,
    central_terminal,
    central_web,
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


def _drain_attach_intents(root: Path, server: Any, io_obj: _IO) -> None:
    """Consume queued web attach intents on the terminal thread."""
    intents = getattr(server, "attach_intents", None)
    if intents is None:
        return
    while True:
        try:
            cid = intents.get_nowait()
        except queue.Empty:
            break
        except Exception:
            break
        try:
            central_attach.attach(root, str(cid), io_obj, timeout_s=5.0)
        except Exception:
            pass


def run_screen(
    root: Path,
    options: dict | None = None,
    io: _IO | None = None,
    clock: _Clock | None = None,
    server: Any = None,
    web_server: Any = None,
    attach_server: Any = None,
    **kwargs: Any,
) -> int:
    """Run the central terminal screen.  Returns 0 on clean exit."""
    srv = web_server or server or attach_server
    if srv is None:
        srv = kwargs.get("web_server") or kwargs.get("server") or kwargs.get("attach_server")

    opts = options or {}
    once = bool(opts.get("once"))
    plain = bool(opts.get("plain"))
    poll_s = float(opts.get("poll", _DEFAULT_POLL_S) or _DEFAULT_POLL_S)
    if poll_s <= 0:
        poll_s = _DEFAULT_POLL_S

    io_obj: _IO = io if io is not None else _StdIO()
    clk: _Clock = clock if clock is not None else _RealClock()

    if srv is not None:
        try:
            srv.attach_bridge_active = True
        except Exception:
            pass

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
        _drain_attach_intents(root, srv, io_obj)
        snap = _snapshot(root, None)
        cols = None
        try:
            cols = terminal_size()[0]
        except Exception:
            cols = 80
        text = _render(snap, selected=0, details=False, plain=True, width=cols)
        _write(getattr(io_obj, "stdout", sys.stdout), text)
        if srv is not None:
            try:
                srv.attach_bridge_active = False
            except Exception:
                pass
        return 0

    selected = 0
    show_details = False
    paused = False

    try:
        with terminal_input(stream=io_obj):
            while True:
                _drain_attach_intents(root, srv, io_obj)
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
    except KeyboardInterrupt:
        return 0
    finally:
        if srv is not None:
            try:
                srv.attach_bridge_active = False
            except Exception:
                pass
    return 0

def _is_tty(io_obj: Any) -> bool:
    stdin = getattr(io_obj, "stdin", None)
    stdout = getattr(io_obj, "stdout", None)
    supported = True
    for stream in (stdin, stdout):
        if stream is not None and hasattr(stream, "isatty"):
            try:
                if not stream.isatty():
                    supported = False
            except Exception:
                supported = False
    return supported


def _make_server(factory: Any, root: Path, assets: Path, port: int, interactive: bool) -> Any:
    try:
        return factory(root, assets, port, attach_bridge_active=interactive)
    except TypeError:
        try:
            server = factory(root, assets, port)
        except TypeError:
            server = factory(root, assets)
        try:
            server.attach_bridge_active = interactive
        except Exception:
            pass
        return server


def run_web(
    root: Path,
    options: dict | None = None,
    io: _IO | None = None,
    clock: _Clock | None = None,
    *,
    assets: Any = None,
    port: int = 0,
    browser_open: Any = None,
    server_factory: Any = None,
    **kwargs: Any,
) -> int:
    """Serve the loopback web view and (when interactive) the terminal bridge."""
    if assets is None and "assets_dir" in kwargs:
        assets = kwargs["assets_dir"]
    if port is None:
        port = 0
    if not isinstance(port, int) or isinstance(port, bool):
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0

    opts = dict(options or {})
    poll_raw = opts.get("poll", 1.0)
    try:
        poll_s = float(poll_raw if poll_raw is not None else 1.0)
    except (TypeError, ValueError):
        poll_s = 1.0
    if not math.isfinite(poll_s) or poll_s <= 0:
        poll_s = 1.0

    io_obj: _IO = io if io is not None else _StdIO()
    clk: _Clock = clock if clock is not None else _RealClock()
    root = Path(root)
    assets_path = Path(assets) if assets is not None else (Path(__file__).resolve().parents[2] / "web")

    factory = server_factory
    if factory is None:
        factory = central_web.create_server

    interactive = _is_tty(io_obj)

    try:
        server = _make_server(factory, root, assets_path, port, interactive)
    except Exception as exc:
        print(f"error: could not start web server: {exc}", file=sys.stderr)
        return 1

    try:
        server.attach_bridge_active = interactive
    except Exception:
        pass

    try:
        url = f"http://127.0.0.1:{server.server_port}/"
    except Exception:
        url = f"http://127.0.0.1:{port}/"

    out = getattr(io_obj, "stdout", sys.stdout)
    _write(out, url + "\n")

    opener = browser_open
    if opener is None:
        opener = webbrowser.open
    opened = True
    try:
        result = opener(url)
        if result is False:
            opened = False
    except Exception:
        opened = False
    if not opened:
        fallback = f"fallback: could not open browser; open {url} manually"
        _write(getattr(io_obj, "stderr", sys.stderr), fallback + "\n")
        _write(out, fallback + "\n")

    server_thread = None
    try:
        if interactive:
            server_thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}
            )
            server_thread.start()
            run_screen(
                root,
                {"once": False, "plain": False, "poll": poll_s},
                io=io_obj,
                clock=clk,
                web_server=server,
            )
        else:
            try:
                server.serve_forever(poll_interval=0.01)
            except KeyboardInterrupt:
                return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"error: web server failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        if server_thread is not None:
            try:
                server_thread.join(timeout=5)
            except Exception:
                pass
        try:
            server.server_close()
        except Exception:
            pass
        try:
            server.attach_bridge_active = False
        except Exception:
            pass
    return 0


run_web_screen = run_web
run_web_view = run_web
serve_web = run_web
run_web_server = run_web
