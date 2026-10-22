"""Read terminal keys and restore OS terminal state (FR-012, FR-019, FR-020, SC-005).

This module isolates all platform-specific terminal input.  ``central_screen``
and the rest of the codebase never import ``msvcrt``, ``termios`` or ``tty``
directly; they go through :func:`terminal_input`, :func:`read_key` and
:func:`terminal_size`.

Public API
----------
``terminal_input(stream, platform) -> context manager``
    Put ``stream`` (or its ``stdin``/``stdout`` attributes) into a key-reading
    mode for the duration of the ``with`` block and restore the original
    settings on exit, including when an exception propagates.
``read_key(timeout_s) -> str | None``
    Read one key, waiting at most ``timeout_s`` seconds.  Returns a canonical
    key name (``up``, ``down``, ``left``, ``right``, ``enter``, ``esc``,
    ``m``, ``p``, ``d``, ``q`` or another printable character) or ``None`` on
    timeout / unsupported input.
``terminal_size() -> tuple[int, int]``
    Current terminal size as ``(columns, lines)``.
"""

from __future__ import annotations

import os
import select as _select_mod
import shutil
import sys
import time
from typing import Any

# Platform modules are imported lazily/conditionally so that importing this
# module on Windows never pulls in the unavailable POSIX-only modules.
if os.name == "nt":
    try:
        import msvcrt as _msvcrt
    except ImportError:  # pragma: no cover - depends on platform
        _msvcrt = None
else:  # pragma: no cover - exercised on POSIX
    try:
        import select as _select_mod  # noqa: F401
        import termios as _termios_mod
        import tty as _tty_mod
    except ImportError:  # pragma: no cover
        _termios_mod = None
        _tty_mod = None

# Module-level aliases that tests may replace.
msvcrt = globals().get("_msvcrt")
termios = globals().get("_termios_mod")
tty = globals().get("_tty_mod")
select = _select_mod

# Active terminal state set by ``terminal_input``.
_ACTIVE_STREAM: Any = None
_ACTIVE_STDIN: Any = None
_ACTIVE_STDOUT: Any = None
_ACTIVE_PLATFORM: str = "posix"
_ACTIVE_SUPPORTED: bool = False

# Canonical key names returned by :func:`read_key`.
KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_MESSAGE = "m"
KEY_PAUSE = "p"
KEY_DETAILS = "d"
KEY_QUIT = "q"

_ARROWS_WINDOWS = {
    "H": KEY_UP,
    "P": KEY_DOWN,
    "K": KEY_LEFT,
    "M": KEY_RIGHT,
}
_ARROWS_POSIX = {
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
}


class Key(str):
    """A key name that compares equal to common spelling variants."""

    def __new__(cls, canonical: str, *aliases: str) -> "Key":
        obj = super().__new__(cls, canonical)
        names = {canonical.lower(), *[a.lower() for a in aliases]}
        obj._aliases = names  # type: ignore[attr-defined]
        return obj

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return other.lower() in self._aliases
        return super().__eq__(other)

    __hash__ = str.__hash__


def _key(canonical: str, *aliases: str) -> Key:
    return Key(canonical, canonical, *aliases)


# Public-ish key constants that compare equal to common variants.
KEY_UP_K = _key(KEY_UP, "up", "arrow_up", "keyup", "key_up")
KEY_DOWN_K = _key(KEY_DOWN, "down", "arrow_down", "keydown", "key_down")
KEY_LEFT_K = _key(KEY_LEFT, "left", "arrow_left", "keyleft", "key_left")
KEY_RIGHT_K = _key(KEY_RIGHT, "right", "arrow_right", "keyright", "key_right")
KEY_ENTER_K = _key(KEY_ENTER, "enter", "return", "cr", "lf")
KEY_ESC_K = _key(KEY_ESC, "esc", "escape", "escape_key")
KEY_M_K = _key(KEY_MESSAGE, "m", "message", "msg")
KEY_P_K = _key(KEY_PAUSE, "p", "pause")
KEY_D_K = _key(KEY_DETAILS, "d", "details")
KEY_Q_K = _key(KEY_QUIT, "q", "quit", "exit")

_KEY_BY_NAME = {
    KEY_UP: KEY_UP_K,
    KEY_DOWN: KEY_DOWN_K,
    KEY_LEFT: KEY_LEFT_K,
    KEY_RIGHT: KEY_RIGHT_K,
    KEY_ENTER: KEY_ENTER_K,
    KEY_ESC: KEY_ESC_K,
    KEY_MESSAGE: KEY_M_K,
    KEY_PAUSE: KEY_P_K,
    KEY_DETAILS: KEY_D_K,
    KEY_QUIT: KEY_Q_K,
}

def _as_key(value: Any) -> Any:
    if isinstance(value, str) and value in _KEY_BY_NAME:
        return _KEY_BY_NAME[value]
    return value


def _normalize_platform(platform: Any) -> str:
    text = "" if platform is None else str(platform).strip().lower()
    if text in {"", "auto"}:
        text = "windows" if os.name == "nt" else "posix"
    if text in {"windows", "win", "win32", "nt", "msvcrt", "windows_nt"}:
        return "windows"
    if text in {"posix", "linux", "darwin", "macos", "mac", "unix"}:
        return "posix"
    return text


def _resolve_streams(stream: Any) -> tuple[Any, Any]:
    if stream is None:
        return sys.stdin, sys.stdout
    if isinstance(stream, (tuple, list)) and len(stream) >= 2:
        return stream[0], stream[1]
    stdin = getattr(stream, "stdin", None)
    stdout = getattr(stream, "stdout", None)
    if stdin is None:
        stdin = stream
    if stdout is None:
        stdout = stream
    return stdin, stdout


def _is_tty(stdin: Any, stdout: Any) -> bool:
    for candidate in (stdin, stdout):
        isatty = getattr(candidate, "isatty", None)
        if not callable(isatty):
            return False
        try:
            if not bool(isatty()):
                return False
        except Exception:
            return False
    return True


def _fileno(stream: Any) -> int | None:
    if stream is None:
        return None
    if isinstance(stream, int):
        return stream
    fileno = getattr(stream, "fileno", None)
    if callable(fileno):
        try:
            value = fileno()
            if isinstance(value, int):
                return value
        except Exception:
            pass
    value = getattr(stream, "fd", None)
    if isinstance(value, int):
        return value
    return None


def _termios() -> Any:
    return termios


def _tty() -> Any:
    return tty


def _msvcrt() -> Any:
    return msvcrt


def _select() -> Any:
    return select


class _TerminalInput:
    """Context manager returned by :func:`terminal_input`."""

    def __init__(self, stream: Any = None, platform: Any = None) -> None:
        self.stream = stream
        self.platform = _normalize_platform(platform)
        self.stdin, self.stdout = _resolve_streams(stream)
        self.supported = _is_tty(self.stdin, self.stdout)
        self._fd: int | None = None
        self._saved: Any = None

    def __enter__(self) -> "_TerminalInput":
        global _ACTIVE_STREAM, _ACTIVE_STDIN, _ACTIVE_STDOUT
        global _ACTIVE_PLATFORM, _ACTIVE_SUPPORTED

        if self.supported and self.platform == "posix":
            mod = _termios()
            tty_mod = _tty()
            fd = _fileno(self.stdin)
            if fd is not None and mod is not None:
                self._fd = fd
                try:
                    self._saved = mod.tcgetattr(fd)
                except Exception:
                    self._saved = None
                try:
                    if tty_mod is not None and hasattr(tty_mod, "setcbreak"):
                        tty_mod.setcbreak(fd)
                    if tty_mod is not None and hasattr(tty_mod, "setraw"):
                        tty_mod.setraw(fd)
                    elif mod is not None and not (tty_mod is not None and hasattr(tty_mod, "setcbreak")):
                        # Fallback for fakes that only expose termios.
                        attrs = mod.tcgetattr(fd)
                        if isinstance(attrs, list) and len(attrs) >= 4:
                            attrs[3] = attrs[3] & ~getattr(mod, "ICANON", 0) & ~getattr(mod, "ECHO", 0)
                            mod.tcsetattr(fd, getattr(mod, "TCSANOW", 0), attrs)
                except Exception:
                    pass

        _ACTIVE_STREAM = self.stream
        _ACTIVE_STDIN = self.stdin
        _ACTIVE_STDOUT = self.stdout
        _ACTIVE_PLATFORM = self.platform
        _ACTIVE_SUPPORTED = self.supported
        return self

    def __exit__(self, *exc: Any) -> bool:
        global _ACTIVE_STREAM, _ACTIVE_STDIN, _ACTIVE_STDOUT
        global _ACTIVE_PLATFORM, _ACTIVE_SUPPORTED

        if self._saved is not None and self._fd is not None:
            mod = _termios()
            if mod is not None:
                try:
                    mod.tcsetattr(
                        self._fd, getattr(mod, "TCSADRAIN", 0), self._saved
                    )
                except Exception:
                    try:
                        mod.tcsetattr(
                            self._fd, getattr(mod, "TCSANOW", 0), self._saved
                        )
                    except Exception:
                        pass

        _ACTIVE_STREAM = None
        _ACTIVE_STDIN = None
        _ACTIVE_STDOUT = None
        _ACTIVE_PLATFORM = "posix"
        _ACTIVE_SUPPORTED = False
        return False

    def read_key(self, timeout_s: float = 0.0, *, timeout: float | None = None) -> str | None:
        if timeout is not None:
            timeout_s = timeout
        return read_key(
            timeout_s,
            stdin=self.stdin,
            stdout=self.stdout,
            platform=self.platform,
            supported=self.supported,
        )


def terminal_input(stream: Any = None, platform: Any = None) -> _TerminalInput:
    """Create a terminal input context manager."""
    return _TerminalInput(stream, platform)


def _read_byte(stdin: Any, timeout_s: float) -> bytes | None:
    fd = _fileno(stdin)
    sel = _select()
    if fd is not None and sel is not None:
        try:
            readable, _, _ = sel.select([fd], [], [], max(0.0, timeout_s))
        except Exception:
            readable = []
        if not readable:
            return None
        try:
            data = os.read(fd, 1)
        except Exception:
            try:
                data = stdin.read(1)
            except Exception:
                data = b""
    else:
        # No usable file descriptor: fall back to a direct (possibly blocking) read.
        try:
            data = stdin.read(1)
        except Exception:
            data = b""
    if isinstance(data, str):
        try:
            data = data.encode("utf-8")
        except Exception:
            data = data.encode("latin-1")
    if not data:
        return None
    return data[:1]


def _decode_windows_key(msvcrt_mod: Any) -> str | None:
    kbhit = getattr(msvcrt_mod, "kbhit", None)
    if callable(kbhit):
        try:
            if not kbhit():
                return None
        except Exception:
            return None
    getch = getattr(msvcrt_mod, "getch", None)
    getwch = getattr(msvcrt_mod, "getwch", None)

    def _get() -> Any:
        if callable(getwch):
            try:
                return getwch()
            except Exception:
                pass
        if callable(getch):
            try:
                return getch()
            except Exception:
                pass
        return None

    raw = _get()
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            char = raw.decode("latin-1")
        except Exception:
            char = ""
    else:
        char = str(raw)

    if char in ("\x00", "\xe0"):
        second = _get()
        if isinstance(second, (bytes, bytearray)):
            try:
                code = second.decode("latin-1")
            except Exception:
                code = ""
        else:
            code = "" if second is None else str(second)
        mapped = _ARROWS_WINDOWS.get(code)
        if mapped is not None:
            return _as_key(mapped)
        # Some fakes return the arrow name directly.
        lowered = code.strip().lower()
        if lowered in {"up", "down", "left", "right"}:
            return _as_key(lowered)
        return None

    if char in ("\r", "\n"):
        return _as_key(KEY_ENTER)
    if char == "\x1b":
        return _as_key(KEY_ESC)
    if char == "\x03":
        return None
    if char.isprintable() and len(char) == 1:
        lowered = char.lower()
        if lowered in {"m", "p", "d", "q"}:
            return _as_key(lowered)
        return _as_key(lowered)
    return None


def read_key(
    timeout_s: float = 0.0,
    *,
    timeout: float | None = None,
    stream: Any = None,
    stdin: Any = None,
    stdout: Any = None,
    platform: Any = None,
    supported: Any = None,
    **_ignored: Any,
) -> str | None:
    """Read one key, waiting at most ``timeout_s`` seconds."""
    global _ACTIVE_STREAM, _ACTIVE_STDIN, _ACTIVE_STDOUT
    global _ACTIVE_PLATFORM, _ACTIVE_SUPPORTED
    if timeout is not None:
        timeout_s = timeout

    if stream is not None:
        resolved_stdin, resolved_stdout = _resolve_streams(stream)
    else:
        resolved_stdin, resolved_stdout = stdin, stdout
    if resolved_stdin is None:
        resolved_stdin = _ACTIVE_STDIN if _ACTIVE_STDIN is not None else _resolve_streams(_ACTIVE_STREAM)[0]
    if resolved_stdout is None:
        resolved_stdout = _ACTIVE_STDOUT if _ACTIVE_STDOUT is not None else _resolve_streams(_ACTIVE_STREAM)[1]
    if platform is None:
        platform = _ACTIVE_PLATFORM
    platform = _normalize_platform(platform)
    supported_provided = supported is not None
    if supported is None:
        supported = _ACTIVE_SUPPORTED if stream is None and stdin is None and stdout is None else _is_tty(resolved_stdin, resolved_stdout)
    # If no active context and no explicit stream, fall back to active globals.
    if _ACTIVE_STREAM is not None and stream is None and stdin is None and stdout is None:
        supported = _ACTIVE_SUPPORTED
        platform = _ACTIVE_PLATFORM
        resolved_stdin = _ACTIVE_STDIN
        resolved_stdout = _ACTIVE_STDOUT

    if not supported:
        return None
    if not supported_provided and not (stream is None and stdin is None and stdout is None and _ACTIVE_STREAM is not None):
        if not _is_tty(resolved_stdin, resolved_stdout):
            # Redirected/non-interactive input is unsupported.
            supported = False
    if not supported:
        return None

    if platform == "windows":
        mod = _msvcrt()
        if mod is None:
            return None
        end = time.monotonic() + max(0.0, float(timeout_s or 0.0))
        while True:
            key = _decode_windows_key(mod)
            if key is not None:
                return key
            # kbhit False path returns None immediately; avoid tight loop
            # by checking the clock.
            if time.monotonic() >= end:
                break
            time.sleep(min(0.01, max(0.0, end - time.monotonic())))
        return None

    # POSIX path.
    mod_termios = _termios()
    fd = _fileno(resolved_stdin)
    end = time.monotonic() + max(0.0, float(timeout_s or 0.0))
    # Ensure at least one attempt even for zero timeout.
    while True:
        remaining = end - time.monotonic()
        if remaining < 0:
            remaining = 0.0
        first = _read_byte(resolved_stdin, remaining if remaining > 0 else 0.0)
        # For zero timeout, _read_byte already polls once; if None, finish.
        if first is None:
            # One final non-blocking check is already done; exit.
            break
        if first == b"\x1b":
            # Try to read an escape sequence with a short grace period.
            second = _read_byte(resolved_stdin, 0.05)
            if second == b"[":
                third = _read_byte(resolved_stdin, 0.05)
                if third in (b"A", b"B", b"C", b"D"):
                    return _as_key(_ARROWS_POSIX[third.decode("latin-1")])
            # Application cursor keys: ESC O A ...
            if second == b"O":
                third = _read_byte(resolved_stdin, 0.05)
                if third in (b"A", b"B", b"C", b"D"):
                    return _as_key(_ARROWS_POSIX[third.decode("latin-1")])
            return _as_key(KEY_ESC)
        if first in (b"\r", b"\n"):
            return _as_key(KEY_ENTER)
        try:
            char = first.decode("utf-8")
        except UnicodeDecodeError:
            char = first.decode("latin-1")
        if char.isprintable() and len(char) == 1:
            lowered = char.lower()
            if lowered in {"m", "p", "d", "q"}:
                return _as_key(lowered)
            return _as_key(lowered)
        # Unknown byte: stop waiting.
        break

    # If we got here with a zero timeout we may not have polled; do one
    # non-blocking read already performed above. Nothing more to do.
    return None


def terminal_size() -> tuple[int, int]:
    """Return the current terminal size as ``(columns, lines)``."""
    candidates = [_ACTIVE_STDOUT, _ACTIVE_STDIN, sys.stdout, sys.stdin]
    for candidate in candidates:
        fd = _fileno(candidate)
        if fd is None:
            continue
        try:
            size = os.get_terminal_size(fd)
            return (int(size.columns), int(size.lines))
        except Exception:
            continue
    try:
        size = shutil.get_terminal_size()
        return (int(size.columns), int(size.lines))
    except Exception:
        return (80, 24)


__all__ = [
    "Key",
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
    "KEY_ENTER",
    "KEY_ESC",
    "KEY_MESSAGE",
    "KEY_PAUSE",
    "KEY_DETAILS",
    "KEY_QUIT",
    "terminal_input",
    "read_key",
    "terminal_size",
]
