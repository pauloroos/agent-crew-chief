"""Tests for the central preference keys in config.py (FR-003, FR-022)."""

from __future__ import annotations

from pathlib import Path

import pytest

from crew_chief.config import PrefsError, load_prefs

GLOBAL_TEXT = (
    'coordinator_default = "claude"\n'
    "coordinator_unrestricted = false\n"
    "central_poll_s = 2.5\n"
    "central_round_timeout_s = 600\n"
    'central_backend = "orca"\n'
)

PROJECT_TEXT = (
    'coordinator_default = "codex"\n'
    "coordinator_unrestricted = true\n"
    "central_poll_s = 3.5\n"
    "central_round_timeout_s = 1200\n"
    'central_backend = "other"\n'
)

# (key, TOML source for a value the key must not take).
BAD_SCALARS = [
    ("coordinator_default", "5"),
    ("coordinator_unrestricted", '"yes"'),
    ("central_poll_s", "0"),
    ("central_poll_s", "-1"),
    ("central_poll_s", "nan"),
    ("central_poll_s", "inf"),
    ("central_poll_s", "true"),
    ("central_round_timeout_s", '"900"'),
    ("central_backend", '""'),
]


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


def test_central_keys_default_to_the_plan_values(tmp_path):
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.coordinator_default == ""
    assert p.coordinator_unrestricted is True
    assert p.central_poll_s == 1.0
    assert p.central_round_timeout_s == 900
    assert p.central_backend == "default"


def test_project_central_keys_win_over_global(tmp_path):
    g = _global(tmp_path, GLOBAL_TEXT)
    proj = tmp_path / "proj"
    _project(proj, PROJECT_TEXT)
    p = load_prefs(proj, global_path=g)
    assert p.coordinator_default == "codex"
    assert p.coordinator_unrestricted is True
    assert p.central_poll_s == 3.5
    assert p.central_round_timeout_s == 1200
    assert p.central_backend == "other"


def test_global_central_keys_apply_without_a_project_file(tmp_path):
    g = _global(tmp_path, GLOBAL_TEXT)
    p = load_prefs(tmp_path, global_path=g)
    assert p.coordinator_default == "claude"
    assert p.coordinator_unrestricted is False
    assert p.central_poll_s == 2.5
    assert p.central_round_timeout_s == 600
    assert p.central_backend == "orca"


def test_empty_coordinator_default_is_accepted(tmp_path):
    _project(tmp_path, 'coordinator_default = ""\n')
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.coordinator_default == ""


def test_unknown_keys_are_ignored_and_input_bytes_unchanged(tmp_path):
    path = _project(
        tmp_path, 'mystery_setting = "keep"\ncoordinator_unrestricted = false\n'
    )
    raw = path.read_bytes()
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.coordinator_unrestricted is False
    assert path.read_bytes() == raw


def test_bom_and_crlf_are_parsed_and_left_unchanged(tmp_path):
    text = 'coordinator_default = "codex"\r\ncentral_poll_s = 2.0\r\n'
    path = _project(tmp_path, text, encoding="utf-8-sig", newline="")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.coordinator_default == "codex" and p.central_poll_s == 2.0
    assert path.read_bytes() == raw


def test_crlf_global_file_is_parsed_and_left_unchanged(tmp_path):
    g = _global(tmp_path, "central_backend = \"orca\"\r\n", newline="")
    raw = g.read_bytes()
    assert b"\xef\xbb\xbf" not in raw
    p = load_prefs(tmp_path, global_path=g)
    assert p.central_backend == "orca"
    assert g.read_bytes() == raw


@pytest.mark.parametrize(("key", "value"), BAD_SCALARS)
def test_bad_central_scalar_is_rejected_in_the_project_file(
    tmp_path, key, value
):
    _project(tmp_path, f"{key} = {value}\n")
    with pytest.raises(PrefsError, match=key):
        load_prefs(tmp_path, global_path=tmp_path / "none.toml")


def test_bad_central_scalar_is_rejected_in_the_global_file(tmp_path):
    g = _global(tmp_path, "central_poll_s = 0\n")
    with pytest.raises(PrefsError, match="central_poll_s"):
        load_prefs(tmp_path, global_path=g)
