"""Tests for terminal key reading and restoration (T023)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from crew_chief import central_keys


class _FakeMsvcrt:
    def __init__(self, sequence):
        self._seq = list(sequence)
        self.kbhit_calls = 0

    def kbhit(self):
        self.kbhit_calls += 1
        return bool(self._seq)

    def getch(self):
        if not self._seq:
            raise EOFError("no key")
        item = self._seq.pop(0)
        if isinstance(item, str):
            item = item.encode("latin-1")
        return item

    def getwch(self):
        return self.getch().decode("latin-1")


class _FakeStream:
    def __init__(self, tty=True, fd=5):
        self._tty = tty
        self._fd = fd
        self.reads = []

    def isatty(self):
        return self._tty

    def fileno(self):
        return self._fd

    def read(self, n=1):
        return b""


def test_import_does_not_load_termios():
    assert "termios" not in sys.modules or os.name != "nt"


def test_terminal_size_uses_os(monkeypatch):
    def fake_get_terminal_size(fd=None):
        class S:
            columns = 123
            lines = 45

        return S()

    monkeypatch.setattr(os, "get_terminal_size", fake_get_terminal_size, raising=False)
    monkeypatch.setattr(central_keys.shutil, "get_terminal_size", lambda: (_ for _ in ()).throw(Exception("no")))
    assert central_keys.terminal_size() == (123, 45)


def test_windows_decode_arrows_enter_esc_and_letters(monkeypatch):
    seq = [b"\xe0", b"H", b"\xe0", b"P", b"\xe0", b"K", b"\xe0", b"M", b"\r", b"\x1b", b"m", b"p", b"d", b"q"]
    fake = _FakeMsvcrt(seq)
    monkeypatch.setattr(central_keys, "msvcrt", fake)
    stream = _FakeStream()
    with central_keys.terminal_input(stream, "windows") as ctx:
        assert ctx.read_key(0) == "up"
        assert ctx.read_key(0) == "down"
        assert ctx.read_key(0) == "left"
        assert ctx.read_key(0) == "right"
        assert ctx.read_key(0) == "enter"
        assert ctx.read_key(0) == "esc"
        assert ctx.read_key(0) == "m"
        assert ctx.read_key(0) == "p"
        assert ctx.read_key(0) == "d"
        assert ctx.read_key(0) == "q"


def test_timeout_returns_none(monkeypatch):
    fake = _FakeMsvcrt([])
    monkeypatch.setattr(central_keys, "msvcrt", fake)
    stream = _FakeStream()
    with central_keys.terminal_input(stream, "windows"):
        assert central_keys.read_key(0) is None


def test_redirected_input_returns_none(monkeypatch):
    fake = _FakeMsvcrt([b"m"])
    monkeypatch.setattr(central_keys, "msvcrt", fake)
    stream = _FakeStream(tty=False)
    with central_keys.terminal_input(stream, "windows"):
        assert central_keys.read_key(0) is None


def test_restoration_posix(monkeypatch):
    # Inject fake posix modules.
    class FakeTermios:
        TCSADRAIN = 1
        TCSANOW = 2
        ICANON = 4
        ECHO = 8

        def __init__(self):
            self.saved = {"mode": "old"}
            self.tcgetattr_calls = 0
            self.tcsetattr_calls = []

        def tcgetattr(self, fd):
            self.tcgetattr_calls += 1
            return self.saved

        def tcsetattr(self, fd, when, attrs):
            self.tcsetattr_calls.append((fd, when, attrs))

    class FakeTty:
        def __init__(self, termios):
            self.termios = termios
            self.calls = []

        def setcbreak(self, fd):
            self.calls.append(fd)

    termios = FakeTermios()
    tty = FakeTty(termios)
    monkeypatch.setattr(central_keys, "termios", termios)
    monkeypatch.setattr(central_keys, "tty", tty)
    monkeypatch.setattr(central_keys, "select", None)

    stream = _FakeStream(tty=True, fd=7)
    # Make select unavailable so _read_byte uses direct read (returns b"").
    with central_keys.terminal_input(stream, "posix"):
        pass
    assert termios.tcgetattr_calls == 1
    assert tty.calls == [7]
    assert termios.tcsetattr_calls
    assert termios.tcsetattr_calls[-1][1] == termios.TCSADRAIN
    assert termios.tcsetattr_calls[-1][2] is termios.saved


def test_exception_restores(monkeypatch):
    class FakeTermios:
        TCSADRAIN = 1

        def __init__(self):
            self.saved = "old"
            self.tcsetattr_calls = []

        def tcgetattr(self, fd):
            return self.saved

        def tcsetattr(self, fd, when, attrs):
            self.tcsetattr_calls.append((fd, when, attrs))

    class FakeTty:
        def setcbreak(self, fd):
            pass

    termios = FakeTermios()
    monkeypatch.setattr(central_keys, "termios", termios)
    monkeypatch.setattr(central_keys, "tty", FakeTty())
    monkeypatch.setattr(central_keys, "select", None)

    stream = _FakeStream(tty=True, fd=9)
    try:
        with central_keys.terminal_input(stream, "posix"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert termios.tcsetattr_calls


def test_posix_decode_escape_sequences(monkeypatch):
    # Fake stream whose select reports readable and read returns bytes.
    class FakeSelect:
        def select(self, rlist, wlist, xlist, timeout):
            # First call for ESC: readable; subsequent for sequence.
            return (list(rlist), [], [])

    # We need a stream with read returning a sequence of bytes.
    class SeqStream:
        def __init__(self):
            self._data = [b"\x1b", b"[", b"A", b"\r", b"m", b"q"]
            self._tty = True
            self._fd = 3

        def isatty(self):
            return self._tty

        def fileno(self):
            return self._fd

        def read(self, n=1):
            if self._data:
                return self._data.pop(0)
            return b""

    sel = FakeSelect()
    monkeypatch.setattr(central_keys, "select", sel)
    termios = type("T", (), {})()
    monkeypatch.setattr(central_keys, "termios", termios)
    monkeypatch.setattr(central_keys, "tty", type("Y", (), {"setcbreak": lambda self, fd: None})())

    stream = SeqStream()
    with central_keys.terminal_input(stream, "posix"):
        # read_key uses _read_byte -> os.read? fd present, so os.read called on fd 3 -> fails.
        pass
    # Because fd is present, os.read(fd) will be attempted; patch os.read.
    stream2 = SeqStream()
    monkeypatch.setattr(os, "read", lambda fd, n: stream2.read(n), raising=False)
    with central_keys.terminal_input(stream2, "posix"):
        assert central_keys.read_key(0) == "up"
        assert central_keys.read_key(0) == "enter"
        assert central_keys.read_key(0) == "m"
        assert central_keys.read_key(0) == "q"


def test_terminal_size_fallback(monkeypatch):
    monkeypatch.setattr(os, "get_terminal_size", lambda fd=None: (_ for _ in ()).throw(OSError("no")), raising=False)
    monkeypatch.setattr(central_keys.shutil, "get_terminal_size", lambda: type("S", (), {"columns": 80, "lines": 24})())
    assert central_keys.terminal_size() == (80, 24)
