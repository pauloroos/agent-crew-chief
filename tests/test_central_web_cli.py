"""Tests for ``acc central --web`` startup and shutdown (T034)."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
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
        self.ready = threading.Event()

    def serve_forever(self, poll_interval=0.5):
        self.served = True
        self.ready.set()

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
         patch("crew_chief.central_screen.central_attach.attach", fake_attach), \
         patch("crew_chief.central_screen.read_key", return_value="q"), \
         patch("crew_chief.central_screen.terminal_input", return_value=nullcontext()):
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


class IO:
    def __init__(self, tty):
        import io
        class Stream(io.StringIO):
            def isatty(self):
                return tty
        self.stdin = Stream()
        self.stdout = Stream()
        self.stderr = Stream()


def test_server_runs_before_browser_opens(tmp_path, monkeypatch):
    fake = FakeServer()
    seen = []
    monkeypatch.setattr(central_screen, "run_screen", lambda *a, **kw: 0)
    def opener(url):
        seen.append(fake.ready.wait(1))
        return True
    assert central_screen.run_web(tmp_path, io=IO(True), assets=tmp_path,
        server_factory=lambda *a, **k: fake, browser_open=opener) == 0
    assert seen == [True]
    assert fake.closed and not fake.attach_bridge_active


def test_redirected_screen_never_consumes_attach_intents(tmp_path, monkeypatch):
    fake = FakeServer()
    fake.attach_intents.put("11111111-1111-4111-8111-111111111111")
    calls = []
    monkeypatch.setattr(central_screen.central_attach, "attach", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(central_screen, "_snapshot", lambda *a: {})
    central_screen.run_screen(tmp_path, io=IO(False), web_server=fake)
    assert calls == [] and fake.attach_intents.qsize() == 1
    assert not fake.attach_bridge_active


@pytest.mark.parametrize("mode", ["normal", "screen_error", "ctrl_c", "browser_error"])
def test_real_http_ready_and_all_threads_closed(tmp_path, monkeypatch, mode):
    from http.client import HTTPConnection
    import socket
    owned, servers = [], []
    assets = Path(__file__).resolve().parents[1] / "skill/crew-chief/web"
    def factory(root, directory, port):
        assert root == tmp_path and directory == assets and port == 0
        server = central_web.create_server(root, directory, port)
        servers.append(server)
        real = server.serve_forever
        def serve(**kwargs):
            owned.append(threading.current_thread())
            return real(**kwargs)
        server.serve_forever = serve
        return server
    fetched = []
    def opener(url):
        server = servers[0]
        assert not server.attach_bridge_active
        conn = HTTPConnection(*server.server_address, timeout=3)
        try:
            conn.request("GET", "/")
            response = conn.getresponse()
            fetched.append(response.status)
            assert b"ACC Central" in response.read()
        finally:
            conn.close()
        if mode == "ctrl_c":
            original = owned[0].join
            calls = []
            def join(timeout=None):
                if not calls:
                    calls.append(1)
                    raise KeyboardInterrupt
                return original(timeout)
            owned[0].join = join
        if mode == "browser_error":
            raise OSError("browser unavailable")
        return True
    def screen(*a, **kw):
        if mode == "screen_error":
            raise RuntimeError("synthetic screen failure")
        return 0
    monkeypatch.setattr(central_screen, "run_screen", screen)
    result = central_screen.run_web(tmp_path, io=IO(mode != "ctrl_c"), assets=assets,
        server_factory=factory, browser_open=opener)
    assert result == (1 if mode == "screen_error" else 0)
    assert fetched == [200]
    assert all(not t.is_alive() for t in owned)
    assert not servers[0].attach_bridge_active and servers[0].socket.fileno() == -1
    with socket.socket() as sock:
        sock.settimeout(.3)
        assert sock.connect_ex(servers[0].server_address) != 0


@pytest.mark.parametrize("poll", ["nan", "inf", "-1"])
def test_nonfinite_or_negative_poll_refused(tmp_path, poll):
    args = _parse(["central", "--web", "--poll", poll, "--central-dir", str(tmp_path)])
    assert central_cli.handle_central(args) == 2


def test_public_cli_rejects_web_json_before_start(tmp_path):
    import subprocess
    import sys
    entry = Path(__file__).resolve().parents[1] / "skill/crew-chief/scripts/crew-chief.py"
    result = subprocess.run([sys.executable, "-I", str(entry), "central", "--web", "--json",
        "--central-dir", str(tmp_path / "central")], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "incompatible" in result.stderr
    assert not (tmp_path / "central").exists()


def test_thread_start_failure_closes_without_shutdown_deadlock(tmp_path, monkeypatch):
    fake = FakeServer()
    def forbidden():
        raise AssertionError("shutdown cannot run before serve_forever")
    fake.shutdown = forbidden
    class BrokenThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("synthetic thread start failure")
    monkeypatch.setattr(central_screen.threading, "Thread", BrokenThread)
    assert central_screen.run_web(tmp_path, io=IO(False), assets=tmp_path,
        server_factory=lambda *a, **k: fake, browser_open=lambda url: True) == 1
    assert fake.closed and not fake.attach_bridge_active
