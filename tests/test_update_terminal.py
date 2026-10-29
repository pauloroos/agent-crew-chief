"""T006 — cached update notice in the terminal central view.

Verification boundary: exact string/version twice, narrow/plain terminal,
no notice when disabled/not newer, no ANSI from cache, existing terminal
keys/output behavior; no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace
import io
import json
from pathlib import Path

import pytest

from crew_chief import __version__, central_screen, central_terminal, config, update_cache

NOTICE = ("ACC v99.0.0 available. Update manually in your ACC source clone: "
          "git fetch --tags; git checkout v99.0.0; python -I install.py --command; "
          "python -I install.py <project>")


def _make_prefs(tmp_path: Path, enabled: bool = True) -> Path:
    prefs_file = tmp_path / "preferences.toml"
    prefs_file.write_text(
        f"check_updates = {'true' if enabled else 'false'}\n", encoding="utf-8"
    )
    return prefs_file


def _seed_cache(config_dir: Path, version: str, now: datetime) -> None:
    assert update_cache.reserve_check(config_dir, now)
    update_cache.store_result(config_dir, now.isoformat(), version)


def test_notice_rendered_in_plain_output(tmp_path, monkeypatch):
    """Notice text appears verbatim in plain terminal output when newer."""
    prefs = _make_prefs(tmp_path)
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))
    now = datetime.now(timezone.utc)
    _seed_cache(prefs.parent, "v99.0.0", now)

    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=prefs.parent)
    snap = {"coordinators": [], "questions": []}
    snap["update_notice"] = provider()
    text = central_terminal.render(snap, plain=True, width=120)
    assert NOTICE in text.splitlines()
    assert text.count("v99.0.0") == 2
    assert provider() is not None
    assert "v99.0.0" in provider()


def test_disabled_preference_suppresses_notice(tmp_path, monkeypatch):
    """No notice text when check_updates is false."""
    prefs = _make_prefs(tmp_path, enabled=False)
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))

    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=prefs.parent)
    assert provider() is None


def test_stale_cache_no_notice(tmp_path, monkeypatch):
    """Stale or missing cache produces no notice."""
    prefs = _make_prefs(tmp_path)
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))

    provider = central_screen.make_notice_provider(project=tmp_path, config_dir=prefs.parent)
    assert provider() is None


def test_plain_render_contains_no_ansi(tmp_path):
    """Plain mode never emits ANSI escapes regardless of snapshot content."""
    snap = {
        "coordinators": [
            {
                "id": "c1",
                "repo_label": "demo",
                "cli": "acc",
                "state": "running",
                "accepted": 1,
                "total": 2,
                "metrics": {},
            }
        ],
        "questions": [],
    }
    text = central_terminal.render(snap, plain=True, width=80)
    assert "\x1b[" not in text


def test_narrow_width_truncates_without_crash(tmp_path):
    """Narrow terminals truncate gracefully without raising."""
    snap = {
        "coordinators": [
            {
                "id": "c1",
                "repo_label": "a-very-long-repo-label-that-exceeds-narrow-width",
                "cli": "acc",
                "state": "running",
                "accepted": 1,
                "total": 2,
                "metrics": {},
            }
        ],
        "questions": [],
    }
    text = central_terminal.render(snap, plain=True, width=20)
    for line in text.splitlines():
        assert len(line) <= 20


def test_existing_render_signature_preserved():
    """central_terminal.render keeps its public signature and defaults."""
    import inspect

    sig = inspect.signature(central_terminal.render)
    params = list(sig.parameters.keys())
    assert params == ["snapshot", "selected", "width", "details", "plain"]

@pytest.mark.parametrize("mode", ["once", "redirected", "interactive"])
@pytest.mark.parametrize("state", ["newer", "disabled", "equal", "older", "stale", "hostile", "missing"])
def test_real_screen_output(tmp_path, monkeypatch, mode, state):
    prefs = _make_prefs(tmp_path, enabled=state != "disabled")
    before = prefs.read_bytes()
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))
    now = datetime.now(timezone.utc)
    if state != "missing":
        stamp = now - timedelta(days=2) if state == "stale" else now
        version = __version__ if state == "equal" else "v0.0.0" if state == "older" else "v99.0.0"
        _seed_cache(prefs.parent, version, stamp)
    if state == "hostile":
        cache = prefs.parent / "update-check.json"
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["latest_version"] = "v99.0.0\x1b[31m"
        cache.write_text(json.dumps(data), encoding="utf-8")

    class Stream(io.StringIO):
        def isatty(self):
            return mode != "redirected"
    streams = SimpleNamespace(stdin=Stream(), stdout=Stream(), stderr=Stream())
    monkeypatch.setattr(central_screen, "terminal_size", lambda: (20, 24))
    monkeypatch.setattr(central_screen, "terminal_input", lambda **kwargs: nullcontext())
    monkeypatch.setattr(central_screen, "read_key", lambda **kwargs: central_screen.KEY_QUIT)
    assert central_screen.run_screen(tmp_path / "central", {
        "once": mode == "once", "plain": True, "project": tmp_path,
    }, streams) == 0
    text = streams.stdout.getvalue()
    if state == "newer":
        assert NOTICE in text.splitlines()
        assert text.count("v99.0.0") == 2
    else:
        assert "ACC " not in text
        assert "v99.0.0" not in text
    assert "\x1b" not in text
    assert prefs.read_bytes() == before


@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("width", [20, 80, 200])
def test_exact_logical_line_in_all_terminal_widths(plain, width):
    text = central_terminal.render({"update_notice": NOTICE}, width=width, plain=plain)
    assert text.splitlines().count(NOTICE) == 1
    assert text.count("v99.0.0") == 2


@pytest.mark.parametrize("notice", [
    None, 123, {}, [], "", "arbitrary notice", "\x1b[31m" + NOTICE,
    NOTICE + "\n", NOTICE + "\x07", NOTICE + "; malicious command",
    NOTICE.replace("checkout v99.0.0", "checkout v99.0.1"),
    NOTICE.replace("v99.0.0", "v099.0.0"),
    NOTICE.replace("v99.0.0", "v99.0.0-rc.1"),
    NOTICE.replace("v99.0.0", "v0.0.0"),
    NOTICE.replace("v99.0.0", "v" + __version__.removeprefix("v")),
])
def test_untrusted_snapshot_notice_is_silent(notice):
    assert central_terminal.render({"update_notice": notice}, plain=True) == central_terminal.render({}, plain=True)
