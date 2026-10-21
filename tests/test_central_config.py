"""Tests for central root and coordinator settings resolution."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from crew_chief.central_config import (
    CentralConfigError,
    resolve_root,
    resolve_settings,
)


def test_resolve_root_explicit_wins():
    home = Path("/home/test")
    assert resolve_root("/explicit/root", {"AGENT_CREW_CHIEF_CENTRAL_DIR": "/env/root"}, home) == Path("/explicit/root")


def test_resolve_root_env_wins_over_home():
    home = Path("/home/test")
    assert resolve_root(None, {"AGENT_CREW_CHIEF_CENTRAL_DIR": "/env/root"}, home) == Path("/env/root")


def test_resolve_root_home_default():
    home = Path("/home/test")
    assert resolve_root(None, {}, home) == home / ".config" / "agent-crew-chief" / "central"


def test_resolve_root_none_env():
    home = Path("/home/test")
    assert resolve_root(None, None, home) == home / ".config" / "agent-crew-chief" / "central"


def test_resolve_root_empty_explicit_falls_through():
    home = Path("/home/test")
    assert resolve_root("", {"AGENT_CREW_CHIEF_CENTRAL_DIR": "/env/root"}, home) == Path("/env/root")


def test_resolve_settings_defaults():
    result = resolve_settings({}, {}, {}, "codex")
    assert result == {
        "coordinator_default": "codex",
        "coordinator_unrestricted": True,
        "central_poll_s": 1.0,
        "central_round_timeout_s": 900,
        "central_backend": "default",
    }


def test_resolve_settings_precedence():
    result = resolve_settings(
        {"coordinator_default": "codex", "central_poll_s": 2.0, "central_round_timeout_s": 200},
        {"coordinator_default": "claude", "central_poll_s": 3.0},
        {"coordinator": "codex", "central_poll_s": 4.0},
        "caller",
    )
    assert result["coordinator_default"] == "codex"
    assert result["central_poll_s"] == 4.0
    assert result["central_round_timeout_s"] == 200


def test_resolve_settings_project_over_global():
    result = resolve_settings(
        {"coordinator_default": "codex"},
        {"coordinator_default": "claude"},
        {},
        "caller",
    )
    assert result["coordinator_default"] == "claude"


def test_resolve_settings_global_over_caller():
    result = resolve_settings(
        {"coordinator_default": "claude"},
        {},
        {},
        "codex",
    )
    assert result["coordinator_default"] == "claude"


def test_resolve_settings_caller_fallback():
    result = resolve_settings({}, {}, {}, "claude")
    assert result["coordinator_default"] == "claude"


def test_resolve_settings_empty_default_falls_back_to_caller():
    result = resolve_settings({"coordinator_default": ""}, {}, {}, "codex")
    assert result["coordinator_default"] == "codex"


def test_resolve_settings_unknown_caller_errors():
    with pytest.raises((ValueError, CentralConfigError)):
        resolve_settings({}, {}, {}, "not-a-cli")


def test_resolve_settings_unknown_override_errors():
    with pytest.raises((ValueError, CentralConfigError)):
        resolve_settings({}, {}, {"cli": "mystery"}, None)


def test_resolve_settings_non_finite_poll():
    with pytest.raises((ValueError, CentralConfigError)):
        resolve_settings({}, {}, {"central_poll_s": float("nan")}, "codex")


def test_resolve_settings_negative_timeout():
    with pytest.raises((ValueError, CentralConfigError)):
        resolve_settings({}, {}, {"central_round_timeout_s": -1}, "codex")


def test_resolve_settings_zero_poll():
    with pytest.raises((ValueError, CentralConfigError)):
        resolve_settings({}, {}, {"central_poll_s": 0}, "codex")


def test_resolve_settings_restricted_override():
    result = resolve_settings({}, {}, {"restricted": True}, "codex")
    assert result["coordinator_unrestricted"] is False

    result = resolve_settings({}, {}, {"unrestricted": False}, "codex")
    assert result["coordinator_unrestricted"] is False

    result = resolve_settings({"coordinator_unrestricted": False}, {}, {}, "codex")
    assert result["coordinator_unrestricted"] is False


def test_resolve_settings_backend():
    result = resolve_settings({}, {}, {"backend": "orca"}, "codex")
    assert result["central_backend"] == "orca"


def test_no_filesystem_writes(tmp_path, monkeypatch):
    # ensure resolve functions do not touch cwd/home
    before = set(tmp_path.iterdir()) if tmp_path.exists() else set()
    resolve_root(None, {}, tmp_path)
    resolve_settings({}, {}, {}, "codex")
    after = set(tmp_path.iterdir()) if tmp_path.exists() else set()
    assert before == after
