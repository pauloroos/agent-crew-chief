"""Tests for ``acc central --web`` startup and shutdown (T034)."""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from crew_chief import central_cli, central_screen, central_web


def _parse(argv):
    parser = argparse.ArgumentParser(prog="acc")
    parser.add_argument("--project", default=".")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    central_cli.add_central_subcommands(sub)
    return parser.parse_args(argv)


class FakeServer:
    def __init__(self, port=12345):
        self.server_port = port
        self.server_address = ("127.0.0.1", port)
        self.attach_bridge_active = False
        self.attach_intents = __import__("queue").Queue(maxsize=16)
        self.closed = False
        self.served = False

    def serve_forever(self, poll_interval=0.5):
        self.served = True

    def shutdown(self):
        pass

    def server_close(self):
        self.closed = True


def test_parser_accepts_web_and_port():
    args = _parse(["central", "--web", "--port", "8080"])
    assert args.web is True
    assert args.port == 8080


def test_web_rejects_json(tmp_path, capsys):
    args = _parse(["central", "--web", "--central-dir", str(tmp_path)])
    args.json = True
    assert central_cli.handle_central(args) == 2
    assert "incompatible" in capsys.readouterr().err


def test_web_rejects_plain_and_once(tmp_path, capsys):
    for flag in ("plain", "once"):
        args = _parse(["central", "--web", "--central-dir", str(tmp_path)])
        setattr(args, flag, True)
        assert central_cli.handle_central(args) == 2


def test_port_requires_web(tmp_path, capsys):
    args = _parse(["central", "--port", "8080", "--central-dir", str(tmp_path)])
    assert central_cli.handle_central(args) == 2
    assert "--port requires --web" in capsys.readouterr().err


def test_invalid_port(tmp_path, capsys):
    args = _parse(["central", "--web", "--port", "70000", "--central-dir", str(tmp_path)])
    assert central_cli.handle_central(args) == 2
    assert "invalid port" in capsys.readouterr().err


def test_invalid_poll(tmp_path, capsys):
    args = _parse(["central", "--web", "--poll", "0", "--central-dir", str(tmp_path)])
    assert central_cli.handle_central(args) == 2
    assert "invalid poll" in capsys.readouterr().err


def test_startup_url_port_fallback_and_cleanup(tmp_path, capsys):
    fake = FakeServer(port=54321)
    opened = {}

    def fake_open(url):
        opened["url"] = url
        return False

    with patch("crew_chief.central_web.create_server", return_value=fake):
        args = _parse(["central", "--web", "--central-dir", str(tmp_path)])
        args.browser_open = fake_open
        rc = central_cli.handle_central(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "http://127.0.0.1:54321/" in out.out
    assert opened["url"] == "http://127.0.0.1:54321/"
    assert "fallback" in out.out or "fallback" in out.err
    assert fake.closed is True
    assert fake.served is True


def test_terminal_bridge_handoff(tmp_path):
    fake = FakeServer(port=54322)
    import queue
    fake.attach_intents.put("11111111-1111-4111-8111-111111111111")
    calls = []

    def fake_attach(root, cid, io, timeout_s=5.0):
        calls.append((str(root), cid))
        return {"ok": True}

    with patch("crew_chief.central_web.create_server", return_value=fake), \
         patch("crew_chief.central_screen.central_attach.attach", fake_attach):
        # Force interactive io
        class TTY:
            def isatty(self):
                return True
        io = type("IO", (), {})()
        io.stdin = TTY()
        io.stdout = TTY()
        io.stderr = TTY()
        args = _parse(["central", "--web", "--central-dir", str(tmp_path)])
        args.browser_open = lambda url: True
        # monkeypatch create_server already; run_web directly
        rc = central_screen.run_web(tmp_path / "central", {"poll": 0.01}, io=io, assets=tmp_path, port=0, server_factory=lambda *a, **k: fake, browser_open=lambda url: True)
    assert rc == 0
    assert calls and calls[0][1] == "11111111-1111-4111-8111-111111111111"
    assert fake.attach_bridge_active is False