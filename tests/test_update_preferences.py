"""Tests for the update-check preference (FR-013, US3/AC2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_chief.config import PrefsError, load_prefs


def _project(
    tmp_path: Path, text: str, *, encoding: str = "utf-8", newline: str = "\n"
) -> Path:
    path = tmp_path / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, newline=newline)
    return path


def _global(
    tmp_path: Path, text: str, *, encoding: str = "utf-8", newline: str = "\n"
) -> Path:
    path = tmp_path / "global.toml"
    path.write_text(text, encoding=encoding, newline=newline)
    return path


SETUP_GLOBAL = (
    "check_updates = false\n"
    "# >>> acc setup >>>\n"
    "check_updates = true\n"
    "# <<< acc setup <<<\n"
)


def test_check_updates_defaults_to_true(tmp_path):
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.check_updates is True


def test_project_check_updates_wins_over_global(tmp_path):
    g = _global(tmp_path, "check_updates = true\n")
    proj = tmp_path / "proj"
    _project(proj, "check_updates = false\n")
    p = load_prefs(proj, global_path=g)
    assert p.check_updates is False


def test_global_check_updates_applies_without_project_file(tmp_path):
    g = _global(tmp_path, "check_updates = false\n")
    p = load_prefs(tmp_path, global_path=g)
    assert p.check_updates is False


def test_setup_block_fills_check_updates_when_global_unset(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(
        "# >>> acc setup >>>\ncheck_updates = false\n# <<< acc setup <<<\n",
        encoding="utf-8",
    )
    p = load_prefs(tmp_path, global_path=g)
    assert p.check_updates is False


def test_global_check_updates_beats_setup_block(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(SETUP_GLOBAL, encoding="utf-8")
    p = load_prefs(tmp_path, global_path=g)
    # top-level (user) wins over the setup block
    assert p.check_updates is False


def test_project_check_updates_beats_setup_block(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(
        "# >>> acc setup >>>\ncheck_updates = true\n# <<< acc setup <<<\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "crew-chief").mkdir(parents=True)
    (tmp_path / "docs" / "crew-chief" / "preferences.toml").write_text(
        "check_updates = false\n", encoding="utf-8"
    )
    p = load_prefs(tmp_path, global_path=g)
    assert p.check_updates is False


@pytest.mark.parametrize("value", ['"true"', '"false"', "1", "0", "1.0", "null"])
def test_check_updates_rejects_non_boolean(tmp_path, value):
    _project(tmp_path, f"check_updates = {value}\n")
    with pytest.raises(PrefsError, match="check_updates"):
        load_prefs(tmp_path, global_path=tmp_path / "none.toml")


def test_check_updates_rejects_non_boolean_in_global(tmp_path):
    g = _global(tmp_path, "check_updates = 1\n")
    with pytest.raises(PrefsError, match="check_updates"):
        load_prefs(tmp_path, global_path=g)


def test_check_updates_rejects_non_boolean_in_setup_block(tmp_path):
    g = tmp_path / "g.toml"
    g.write_text(
        "# >>> acc setup >>>\ncheck_updates = 0\n# <<< acc setup <<<\n",
        encoding="utf-8",
    )
    with pytest.raises(PrefsError, match="check_updates"):
        load_prefs(tmp_path, global_path=g)


def test_bom_and_crlf_are_parsed_and_left_unchanged(tmp_path):
    text = "check_updates = false\r\n"
    path = _project(tmp_path, text, encoding="utf-8-sig", newline="")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.check_updates is False
    assert path.read_bytes() == raw


def test_crlf_global_file_is_parsed_and_left_unchanged(tmp_path):
    g = _global(tmp_path, "check_updates = true\r\n", newline="")
    raw = g.read_bytes()
    p = load_prefs(tmp_path, global_path=g)
    assert p.check_updates is True
    assert g.read_bytes() == raw


def test_loading_does_not_rewrite_settings(tmp_path):
    path = _project(tmp_path, "check_updates = false\nmystery = 1\n")
    raw = path.read_bytes()
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.check_updates is False
    assert path.read_bytes() == raw