"""Tests for cached update notice provided to central view startup (T005)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from crew_chief import central_screen, config, update_cache


def test_disabled_preference_provider_is_silent(tmp_path, monkeypatch):
    prefs_file = tmp_path / "preferences.toml"
    prefs_file.write_text("check_updates = false\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs_file))

    provider = central_screen.make_notice_provider(project=tmp_path)
    assert provider() is None


def test_enabled_provider_reads_cache_only(tmp_path, monkeypatch):
    prefs_file = tmp_path / "preferences.toml"
    prefs_file.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs_file))

    config_dir = prefs_file.parent
    now = datetime.now(timezone.utc)
    assert update_cache.reserve_check(config_dir, now)
    update_cache.store_result(config_dir, now.isoformat(), "v9.9.9")

    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=config_dir)
    assert provider() is not None
    assert "v9.9.9" in provider()


def test_stale_cache_returns_none(tmp_path, monkeypatch):
    prefs_file = tmp_path / "preferences.toml"
    prefs_file.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs_file))

    config_dir = prefs_file.parent
    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=config_dir)
    assert provider() is None


def test_malformed_cache_returns_none(tmp_path, monkeypatch):
    prefs_file = tmp_path / "preferences.toml"
    prefs_file.write_text("check_updates = true\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs_file))

    config_dir = prefs_file.parent
    (config_dir / "update-check.json").write_text("{not json", encoding="utf-8")
    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=config_dir)
    assert provider() is None


def test_run_screen_accepts_injected_provider(tmp_path):
    io = type("IO", (), {})()
    import io as _io
    class Stream(_io.StringIO):
        def isatty(self):
            return False
    io.stdin = Stream()
    io.stdout = Stream()
    io.stderr = Stream()
    provider = lambda: "notice"
    rc = central_screen.run_screen(tmp_path, options={}, io=io, notice_provider=provider)
    assert rc == 0

@pytest.mark.parametrize("once", [True, False])
def test_screen_consumes_provider_at_each_render_boundary(tmp_path, monkeypatch, once):
    import io
    from contextlib import nullcontext

    class Stream(io.StringIO):
        def isatty(self):
            return True
    streams = type("IO", (), {"stdin": Stream(), "stdout": Stream(), "stderr": Stream()})()
    original = {"coordinators": []}
    captured, calls = [], []
    notices = iter(["first notice", None])
    def provider():
        calls.append(True)
        return next(notices)
    def renderer(snapshot, **kwargs):
        captured.append(snapshot)
        return "rendered frame"
    monkeypatch.setattr(central_screen, "_snapshot", lambda *args: original)
    monkeypatch.setattr(central_screen, "_render", renderer)
    monkeypatch.setattr(central_screen, "terminal_input", lambda **kwargs: nullcontext())
    keys = iter([None, central_screen.KEY_QUIT])
    monkeypatch.setattr(central_screen, "read_key", lambda **kwargs: next(keys))
    assert central_screen.run_screen(tmp_path, {"once": once, "plain": True}, streams,
                                     notice_provider=provider) == 0
    assert captured[0] == {"coordinators": [], "update_notice": "first notice"}
    assert captured[0] is not original
    assert original == {"coordinators": []}
    assert streams.stdout.getvalue().count("rendered frame") == (1 if once else 2)
    assert len(calls) == (1 if once else 2)
    if not once:
        assert captured[1] == original


def test_cli_forwards_exact_explicit_project(tmp_path, monkeypatch):
    from crew_chief import cli
    seen = {}
    project = tmp_path / "project"
    project.mkdir()
    def run(root, options, **kwargs):
        seen.update(root=root, options=options)
        return 0
    monkeypatch.setattr(central_screen, "run_screen", run)
    args = cli.build_parser().parse_args([
        "central", "--plain", "--project", str(project),
        "--central-dir", str(tmp_path / "central")])
    assert args.handler(args) == 0
    assert seen["root"] == tmp_path / "central"
    assert seen["options"]["project"] == project.resolve()
