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
        {"cli": "codex", "central_poll_s": 4.0},
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
    with pytest.raises(CentralConfigError, match="unknown coordinator"):
        resolve_settings({}, {}, {}, "not-a-cli")


def test_resolve_settings_unknown_override_errors():
    with pytest.raises(CentralConfigError, match="unknown coordinator"):
        resolve_settings({}, {}, {"cli": "mystery"}, None)


def test_resolve_settings_non_finite_poll():
    with pytest.raises(CentralConfigError, match="central_poll_s must be finite"):
        resolve_settings({}, {}, {"central_poll_s": float("nan")}, "codex")


def test_resolve_settings_negative_timeout():
    with pytest.raises(CentralConfigError, match="central_round_timeout_s must be positive"):
        resolve_settings({}, {}, {"central_round_timeout_s": -1}, "codex")


def test_resolve_settings_zero_poll():
    with pytest.raises(CentralConfigError, match="central_poll_s must be positive"):
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


def test_launch_none_overrides_do_not_mask_project_settings():
    result = resolve_settings(
        {"coordinator_default": "claude", "central_poll_s": 2.0, "central_round_timeout_s": 200},
        {"coordinator_default": "codex", "central_poll_s": 3.0},
        {"cli": None, "poll": None, "round_timeout_s": None, "backend": None},
        "caller",
    )
    assert result["coordinator_default"] == "codex"
    assert result["central_poll_s"] == 3.0
    assert result["central_round_timeout_s"] == 200
    assert result["central_backend"] == "default"


def test_launch_none_overrides_do_not_mask_global_settings():
    result = resolve_settings(
        {"coordinator_default": "claude", "central_poll_s": 2.0, "central_round_timeout_s": 200,
         "central_backend": "orca"},
        {},
        {"cli": None, "poll": None, "round_timeout_s": None, "backend": None},
        "codex",
    )
    assert result["coordinator_default"] == "claude"
    assert result["central_poll_s"] == 2.0
    assert result["central_round_timeout_s"] == 200
    assert result["central_backend"] == "orca"


def test_launch_none_unrestricted_does_not_mask_project_setting():
    result = resolve_settings({}, {"coordinator_unrestricted": False}, {"unrestricted": None}, "codex")
    assert result["coordinator_unrestricted"] is False


def test_launch_none_restricted_does_not_mask_global_setting():
    result = resolve_settings(
        {"coordinator_unrestricted": False}, {}, {"restricted": None, "unrestricted": None}, "codex"
    )
    assert result["coordinator_unrestricted"] is False


def test_launch_none_unrestricted_keeps_project_true_over_global_false():
    result = resolve_settings(
        {"coordinator_unrestricted": False},
        {"coordinator_unrestricted": True},
        {"unrestricted": None, "restricted": None},
        "codex",
    )
    assert result["coordinator_unrestricted"] is True


def test_launch_none_overrides_leave_defaults_intact():
    result = resolve_settings({}, {}, {"cli": None, "unrestricted": None, "restricted": None,
                                      "poll": None, "backend": None}, "codex")
    assert result["coordinator_default"] == "codex"
    assert result["coordinator_unrestricted"] is True
    assert result["central_poll_s"] == 1.0


def test_launch_explicit_false_is_preserved():
    result = resolve_settings({"coordinator_unrestricted": True}, {}, {"unrestricted": False}, "codex")
    assert result["coordinator_unrestricted"] is False

    result = resolve_settings({"coordinator_unrestricted": True}, {}, {"restricted": False}, "codex")
    assert result["coordinator_unrestricted"] is True


@pytest.mark.parametrize("bad", ["false", "true", "no", "0", 0, 1, 0.0, [], {}, (), ["false"]])
def test_preference_unrestricted_rejects_non_bool(bad):
    with pytest.raises(CentralConfigError, match="coordinator_unrestricted must be a boolean"):
        resolve_settings({"coordinator_unrestricted": bad}, {}, {}, "codex")


@pytest.mark.parametrize("bad", ["false", "true", "no", "0", 0, 1, 0.0, [], {}, ()])
def test_launch_unrestricted_rejects_non_bool(bad):
    with pytest.raises(CentralConfigError, match="unrestricted must be a boolean"):
        resolve_settings({}, {}, {"unrestricted": bad}, "codex")


@pytest.mark.parametrize("bad", ["false", "true", "no", "0", 0, 1, 0.0, [], {}, ()])
def test_launch_restricted_rejects_non_bool(bad):
    with pytest.raises(CentralConfigError, match="restricted must be a boolean"):
        resolve_settings({}, {}, {"restricted": bad}, "codex")


def test_launch_string_false_does_not_silently_grant_unrestricted():
    with pytest.raises(CentralConfigError):
        resolve_settings({"coordinator_unrestricted": False}, {}, {"restricted": "false"}, "codex")


@pytest.mark.parametrize("bad", [42, 1.5, True, False, ["claude"], {"cli": "claude"}])
def test_preference_coordinator_rejects_non_string(bad):
    with pytest.raises(CentralConfigError, match="coordinator_default must be a string"):
        resolve_settings({"coordinator_default": bad}, {}, {}, "codex")


@pytest.mark.parametrize("bad", [42, 1.5, True, False, ["claude"], {"cli": "claude"}])
def test_launch_coordinator_rejects_non_string(bad):
    with pytest.raises(CentralConfigError, match="cli must be a string"):
        resolve_settings({}, {}, {"cli": bad}, "codex")


def test_speculative_aliases_are_not_honoured():
    result = resolve_settings(
        {"coordinator_default": "codex"},
        {},
        {"coordinator": "claude", "launch_cli": "claude", "coordinator_override": "claude",
         "cli_override": "claude", "coordinator_cli": "claude"},
        "codex",
    )
    assert result["coordinator_default"] == "codex"

    result = resolve_settings({}, {}, {"central_poll_seconds": 9.0, "poll_s": 9.0, "poll_seconds": 9.0}, "codex")
    assert result["central_poll_s"] == 1.0

    result = resolve_settings({}, {}, {"central_round_timeout": 9, "round_timeout": 9}, "codex")
    assert result["central_round_timeout_s"] == 900


def test_restricted_is_a_launch_flag_not_a_preference():
    assert resolve_settings({"restricted": True}, {}, {}, "codex")["coordinator_unrestricted"] is True
    assert resolve_settings({}, {"restricted": True}, {}, "codex")["coordinator_unrestricted"] is True
    assert resolve_settings({"restricted": False}, {}, {}, "codex")["coordinator_unrestricted"] is True


def test_project_empty_default_falls_back_to_caller_not_global():
    result = resolve_settings({"coordinator_default": "claude"}, {"coordinator_default": ""}, {}, "codex")
    assert result["coordinator_default"] == "codex"


def test_launch_none_coordinator_falls_back_to_caller():
    result = resolve_settings({}, {}, {"cli": None}, "codex")
    assert result["coordinator_default"] == "codex"


def test_no_filesystem_writes(tmp_path, monkeypatch):
    # ensure resolve functions do not touch cwd/home
    before = set(tmp_path.iterdir()) if tmp_path.exists() else set()
    resolve_root(None, {}, tmp_path)
    resolve_settings({}, {}, {}, "codex")
    after = set(tmp_path.iterdir()) if tmp_path.exists() else set()
    assert before == after
