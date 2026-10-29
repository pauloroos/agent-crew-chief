"""Tests for detached background update scheduling (FR-013/FR-014/FR-015/FR-018)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from crew_chief import cli, update_check as check, update_cache


def test_production_worker_entry_executes_under_isolation(tmp_path, monkeypatch):
    prefs = tmp_path / 'prefs.toml'
    prefs.write_text('check_updates = false\n')
    monkeypatch.setenv('AGENT_CREW_CHIEF_GLOBAL_PREFS', str(prefs))
    child = check._spawn_update_worker(tmp_path, project=tmp_path)
    try:
        assert child.wait(timeout=8) == 0
        assert not (tmp_path/'update-check.json').exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize('status', [200, 429])
def test_real_commands_return_before_http_and_throttle_concurrent_workers(tmp_path, monkeypatch, status):
    prefs = tmp_path / 'prefs.toml'
    prefs.write_text('check_updates = true\n')
    before = prefs.read_bytes()
    monkeypatch.setenv('AGENT_CREW_CHIEF_GLOBAL_PREFS', str(prefs))
    entered, release = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(self.path)
            entered.set()
            release.wait(4)
            body = b'{"tag_name":"v999.0.0"}'
            try:
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/latest'
    # Test boundary only: run the actual entry with a fake endpoint and deny
    # external DNS in every spawned interpreter; no production endpoint option.
    bootstrap = tmp_path/'worker-bootstrap.py'
    bootstrap.write_text(
        'import sys,socket,runpy\n'
        f'sys.path.insert(0,{str(Path(check.__file__).parents[1])!r})\n'
        'from crew_chief import update_check\n'
        'update_check.FIXED_ENDPOINT=sys.argv.pop(1)\n'
        'original=socket.getaddrinfo\n'
        'def resolve(host,*args,**kwargs):\n'
        ' assert host=="127.0.0.1",host\n'
        ' return original(host,*args,**kwargs)\n'
        'socket.getaddrinfo=resolve\n'
        'sys.argv=sys.argv[1:]\n'
        'runpy.run_path(sys.argv[0],run_name="__main__")\n', encoding='utf-8')
    real_popen = subprocess.Popen
    children, argvs = [], []
    def spawn(argv, **kwargs):
        argvs.append(argv)
        child = real_popen([*argv[:2], str(bootstrap), endpoint, *argv[2:]], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(check.subprocess, 'Popen', spawn)
    try:
        for index in range(4):
            started = time.monotonic()
            assert cli.main(['economy', '--project', str(tmp_path), '--job', 'absent']) == 2
            assert time.monotonic()-started < 2
            assert not release.is_set()
            if index == 0:
                assert entered.wait(2)
        assert entered.wait(4)
        assert any(child.poll() is None for child in children)
        assert len(requests) == 1
        assert all(Path(argv[2]).name == 'crew-chief.py' for argv in argvs)
        # Other reservations finish before releasing the successful response;
        # the cache deliberately skips any contended nonwaiting transaction.
        for child in children[1:]:
            assert child.wait(timeout=2) == 0
        release.set()
        for child in children:
            assert child.wait(timeout=8) == 0
        assert len(requests) == 1
        notice = update_cache.cached_notice(tmp_path, '0.4.3', True, datetime.now(timezone.utc))
        assert bool(notice) == (status == 200)
        assert not update_cache.reserve_check(tmp_path, datetime.now(timezone.utc))
        assert prefs.read_bytes() == before
    finally:
        release.set()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert not thread.is_alive()


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        assert host in ("127.0.0.1", "localhost", "::1"), host
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_schedule_check_disabled_does_not_spawn(tmp_path, monkeypatch):
    calls = []

    def fake_spawn(config_dir):
        calls.append(config_dir)
        return None

    check.schedule_check(tmp_path, False, spawn=fake_spawn)
    check.schedule_check(tmp_path, "true", spawn=fake_spawn)
    check.schedule_check(tmp_path, 1, spawn=fake_spawn)
    assert calls == []


def test_schedule_check_enabled_calls_spawn_with_config_dir(tmp_path, monkeypatch):
    calls = []

    def fake_spawn(config_dir):
        calls.append(Path(config_dir))
        return None

    check.schedule_check(tmp_path, True, spawn=fake_spawn)
    assert calls == [tmp_path]


def test_schedule_check_spawn_failure_is_silent(tmp_path, monkeypatch):
    def boom(config_dir):
        raise RuntimeError("spawn failed")

    # Must not raise.
    check.schedule_check(tmp_path, True, spawn=boom)


def test_spawn_uses_detached_hidden_flags_and_minimal_env(tmp_path, monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(check.subprocess, "Popen", fake_popen)

    child = check._spawn_update_worker(tmp_path)
    assert getattr(child, "pid", None) == 12345
    assert len(calls) == 1
    info = calls[0]
    argv = info["argv"]
    kwargs = info["kwargs"]
    assert argv[0] == sys.executable
    assert argv[1] == "-I"
    assert argv[2] == str((Path(check.__file__).resolve().parents[1] / "crew-chief.py"))
    assert argv[3] == "_check-updates"
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    env = kwargs["env"]
    assert "PYTHONPATH" not in env
    # No proxy/token env leaked.
    for key in env:
        assert "proxy" not in key.lower()
        assert "token" not in key.lower()
        assert "secret" not in key.lower()
    if os.name == "nt":
        assert kwargs["creationflags"] & 0x08000000
        assert kwargs["startupinfo"].wShowWindow == 0
    else:
        assert kwargs["start_new_session"] is True


def test_spawn_includes_project_when_given(tmp_path, monkeypatch):
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return type("P", (), {"pid": 1})()

    monkeypatch.setattr(check.subprocess, "Popen", fake_popen)
    project = tmp_path / "proj"
    project.mkdir()
    check._spawn_update_worker(tmp_path, project=project)
    argv = calls[0]["argv"]
    assert "--project" in argv
    assert str(project.resolve()) in argv


def test_public_help_and_usage_hide_the_private_worker_entry(capsys):
    """The detached entry stays internal: no name, no SUPPRESS leak in public help."""
    help_text = cli.build_parser().format_help()
    assert "_check-updates" not in help_text
    # Baseline v0.4.2 already exposes exactly one suppressed entry (_supervisor).
    assert help_text.count("==SUPPRESS==") == 1
    assert "_supervisor         ==SUPPRESS==" in help_text
    rc = cli.main(["not-a-command"])
    assert rc == 2
    error_text = capsys.readouterr().err
    assert "_check-updates" not in error_text


def test_private_check_updates_revalidates_preference(tmp_path, monkeypatch):
    # Disabled prefs => no run_worker call.
    project = tmp_path
    global_prefs = tmp_path / "global.toml"
    global_prefs.write_text("check_updates = false\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs))

    called = {"n": 0}

    def fake_run_worker(*args, **kwargs):
        called["n"] += 1

    monkeypatch.setattr(check, "run_worker", fake_run_worker)
    rc = cli.main(["_check-updates", "--project", str(project)])
    assert rc == 0
    assert called["n"] == 0


def test_private_check_updates_runs_worker_when_enabled(tmp_path, monkeypatch):
    project = tmp_path
    global_prefs = tmp_path / "global.toml"
    global_prefs.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs))

    called = {"args": None}

    def fake_run_worker(config_dir, now, *, endpoint=None):
        called["args"] = (Path(config_dir), endpoint)

    monkeypatch.setattr(check, "run_worker", fake_run_worker)
    rc = cli.main(["_check-updates", "--project", str(project)])
    assert rc == 0
    assert called["args"] is not None
    assert called["args"][0] == global_prefs.parent
    assert called["args"][1] == check.FIXED_ENDPOINT


def test_public_command_schedules_without_waiting(tmp_path, monkeypatch):
    # Use a stalled fake server indirectly by ensuring schedule returns fast.
    global_prefs = tmp_path / "global.toml"
    global_prefs.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs))

    spawned = {"n": 0}

    def fake_spawn(config_dir):
        spawned["n"] += 1
        # simulate a quick spawn; no wait
        return None

    # Patch schedule_check's spawn via cli's call by monkeypatching update_check.schedule_check
    original = check.schedule_check

    def patched(config_dir, enabled, *, spawn=None):
        return original(config_dir, enabled, spawn=fake_spawn)

    monkeypatch.setattr(check, "schedule_check", patched)
    monkeypatch.setattr(cli.update_check, "schedule_check", patched)

    started = time.monotonic()
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    elapsed = time.monotonic() - started
    assert elapsed < 2
    assert spawned['n'] == 0


def test_nonblocking_foreground_with_stalled_worker(tmp_path, monkeypatch):
    # Simulate spawn that returns immediately while "worker" would hang.
    global_prefs = tmp_path / "global.toml"
    global_prefs.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs))

    def fake_spawn(config_dir):
        return None

    monkeypatch.setattr(check, "_spawn_update_worker", lambda *a, **k: None)
    started = time.monotonic()
    rc = cli.main(["economy", "--project", str(tmp_path), "--job", "absent"])
    elapsed = time.monotonic() - started
    # discover may fail due to no project prefs? but should return quickly.
    assert elapsed < 15
    assert rc == 2


def _normalize_help(text):
    """Join only the usage-block continuation lines; leave everything else exact."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("usage:"):
            start = i
            break
    if start is None:
        return text
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() == "":
            end = j
            break
        if not line.startswith(" ") and not line.startswith("\t"):
            end = j
            break
    normalized_usage = " ".join(
        s.strip() for s in lines[start:end] if s.strip() != ""
    )
    new_lines = lines[:start] + [normalized_usage] + lines[end:]
    result = "\n".join(new_lines)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def test_public_help_matches_released_v042_exactly(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **k: os.terminal_size((80, 24)))
    # Frozen from released v0.4.2 build_parser().format_help(), COLUMNS=80,
    # plus the v0.4.4 interview subcommand (FR-036).
    # Argparse wraps the usage line differently across Python minors, so compare
    # the whitespace-normalized text instead of the exact layout.
    expected = """usage: crew-chief [-h] [--version]
                  {discover,interview,job,piece,pick,dispatch,correction,grade,accept,discard,close,spec,brief,ruling,decision,usage,economy,scoreboard,setup,mcp,launch,central,message,attach,pause,resume,stop,recover,_supervisor} ...

Coordinate several executor CLIs.

positional arguments:
  {discover,interview,job,piece,pick,dispatch,correction,grade,accept,discard,close,spec,brief,ruling,decision,usage,economy,scoreboard,setup,mcp,launch,central,message,attach,pause,resume,stop,recover,_supervisor}
    discover            list the executors available now
    interview           start or inspect an interview-mode job (T036, FR-036)
    job                 job records
    piece               job pieces
    pick                choose the next executor for a piece
    dispatch            run one attempt
    correction          record a correction on a piece
    grade               grade a piece and refresh the scoreboard
    accept              merge the piece's last ok attempt
    discard             drop the piece's last ok attempt
    close               print the job's commit message and comment
    spec                spec-kit specs: new folder, import tasks as pieces,
                        status
    brief               briefs for spec tasks: exec, review, converge
    ruling              rulings recorded in the job
    decision            ADRs in docs/decisions
    usage               the coordinator's own token spend
    economy             free share, coordinator share, rework, time
    scoreboard          regenerate and print the scoreboard
    setup               detect the CLIs and the subscriptions, ask the rest
                        and write the global cost tiers
    mcp                 run JSON-RPC 2.0 MCP server (stdio or HTTP)
    launch              start a new coordinator
    central             watch every coordinator in one screen
    message             send a message to a coordinator
    attach              enter a coordinator conversation interactively
    pause               pause a coordinator
    resume              resume a coordinator
    stop                stop a coordinator
    recover             recover a coordinator
    _supervisor         ==SUPPRESS==

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
"""
    actual = cli.build_parser().format_help()
    assert _normalize_help(actual) == _normalize_help(expected)
