"""T033 follow-up tests for phase 1 review findings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from crew_chief import central_screen, update_cache, update_check


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_schedule_check_respects_reservation(tmp_path):
    now = _now()
    # valid reservation -> spawn not called
    update_cache.reserve_check(tmp_path, now)
    calls = []

    def spawn(config_dir):
        calls.append(config_dir)

    update_check.schedule_check(tmp_path, True, spawn=spawn, now=now)
    assert calls == []

    # future timestamp reservation -> not due
    future_root = tmp_path / "future"
    future_root.mkdir()
    (future_root / "update-check.json").write_text(
        '{"schema_version":1,"checked_at":"2026-01-02T00:00:00+00:00",'
        '"latest_version":null,"successful_at":null}'
    )
    calls2 = []
    update_check.schedule_check(future_root, True, spawn=lambda d: calls2.append(d), now=now)
    assert calls2 == []

    # absent cache -> spawn called once
    absent_root = tmp_path / "absent"
    absent_root.mkdir()
    calls3 = []
    update_check.schedule_check(absent_root, True, spawn=lambda d: calls3.append(d), now=now)
    assert len(calls3) == 1

    # corrupt cache -> spawn called once
    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    (corrupt_root / "update-check.json").write_text("broken")
    calls4 = []
    update_check.schedule_check(corrupt_root, True, spawn=lambda d: calls4.append(d), now=now)
    assert len(calls4) == 1

    # no cache write by schedule_check on a valid reservation
    before = (tmp_path / "update-check.json").read_bytes()
    update_check.schedule_check(tmp_path, True, spawn=lambda d: None, now=now)
    assert (tmp_path / "update-check.json").read_bytes() == before


def test_readme_update_sections_use_python_I():
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "README.pt-BR.md"):
        text = (root / name).read_text(encoding="utf-8")
        # Extract the ## Update / ## Atualização code block region.
        start = text.index("## Update" if name == "README.md" else "## Atualização")
        end_marker = "### " if "### " in text[start:] else "## "
        # find next '## ' after start (excluding the header itself)
        idx = text.find("\n## ", start + 1)
        section = text[start:] if idx == -1 else text[start:idx]
        assert "python -I install.py --command" in section
        assert "python -I install.py" in section
        assert "python install.py" not in section


def test_dead_notice_provider_names_are_gone():
    assert "notice_provider" not in central_screen.__all__
    assert not hasattr(central_screen, "notice_provider")
    assert central_screen._NOTICE_PROVIDER_KEYS == ("notice_provider",)


def test_invalid_check_updates_provider_is_silent(tmp_path, monkeypatch):
    now = _now()
    project = tmp_path / "project"
    project.mkdir()
    prefs_dir = project / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True)
    prefs_file = prefs_dir / "preferences.toml"

    global_prefs = tmp_path / "global.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(global_prefs))

    config_dir = tmp_path / "cache"
    config_dir.mkdir()
    update_cache.reserve_check(config_dir, now)
    update_cache.store_result(config_dir, now.isoformat(), "v9.9.9")

    for bad in ('"yes"', "1"):
        prefs_file.write_text(f"check_updates = {bad}\n", encoding="utf-8")
        provider = central_screen.make_notice_provider(
            project=project, config_dir=config_dir, now=now
        )
        assert provider() is None, bad

    prefs_file.write_text("check_updates = true\n", encoding="utf-8")
    provider = central_screen.make_notice_provider(
        project=project, config_dir=config_dir, now=now
    )
    assert provider() is not None
    assert "v9.9.9" in provider()