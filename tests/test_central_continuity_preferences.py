"""Tests for the coordinator continuity preferences (FR-027, FR-028, US7/AC5).

``stall_minutes`` is a finite positive number of minutes (default 40) and
``on_coordinator_quota`` is ``ask`` (default), ``wait`` or ``handoff:<cli>``
with a target from the supported coordinator CLIs.
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path

import pytest

from crew_chief import central_config
from crew_chief import config
from crew_chief.central_config import CentralConfigError, resolve_settings
from crew_chief.config import PrefsError, load_prefs

DEFAULTS = {"stall_minutes": 40, "on_coordinator_quota": "ask"}


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


# --- defaults and precedence -------------------------------------------------


def test_continuity_defaults_are_40_minutes_and_ask():
    assert central_config.resolve_continuity_settings({}, {}, {}) == DEFAULTS


def test_continuity_defaults_are_not_shared_mutable_state():
    first = central_config.resolve_continuity_settings({}, {}, {})
    first["on_coordinator_quota"] = "wait"
    assert central_config.resolve_continuity_settings({}, {}, {}) == DEFAULTS


def test_launch_overrides_win_over_project_and_global():
    result = central_config.resolve_continuity_settings(
        {"stall_minutes": 10, "on_coordinator_quota": "wait"},
        {"stall_minutes": 20, "on_coordinator_quota": "handoff:claude"},
        {"stall_minutes": 30, "on_coordinator_quota": "handoff:codex"},
    )
    assert result == {"stall_minutes": 30, "on_coordinator_quota": "handoff:codex"}


def test_project_wins_over_global():
    result = central_config.resolve_continuity_settings(
        {"stall_minutes": 10, "on_coordinator_quota": "wait"},
        {"stall_minutes": 20},
        {},
    )
    assert result == {"stall_minutes": 20, "on_coordinator_quota": "wait"}


def test_launch_none_values_do_not_mask_project_settings():
    result = central_config.resolve_continuity_settings(
        {"stall_minutes": 10},
        {"stall_minutes": 20, "on_coordinator_quota": "wait"},
        {"stall_minutes": None, "on_coordinator_quota": None},
    )
    assert result == {"stall_minutes": 20, "on_coordinator_quota": "wait"}


def test_inputs_are_not_mutated():
    global_data = {"stall_minutes": 10, "on_coordinator_quota": "wait"}
    project_data = {"on_coordinator_quota": "handoff:claude"}
    overrides = {"stall_minutes": None}
    before = copy.deepcopy((global_data, project_data, overrides))
    central_config.resolve_continuity_settings(global_data, project_data, overrides)
    assert (global_data, project_data, overrides) == before


def test_established_settings_output_is_unchanged():
    # Existing callers compare resolve_settings to its five established keys.
    result = resolve_settings(
        {"stall_minutes": 5, "on_coordinator_quota": "wait", "central_poll_s": 2.0},
        {},
        {},
        "codex",
    )
    assert result == {
        "coordinator_default": "codex",
        "coordinator_unrestricted": True,
        "central_poll_s": 2.0,
        "central_round_timeout_s": 900,
        "central_backend": "default",
    }


# --- policy values -----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param("ask", "ask", id="ask"),
        pytest.param("wait", "wait", id="wait"),
        pytest.param(" WAIT ", "wait", id="surrounding-space"),
        pytest.param("handoff:claude", "handoff:claude", id="handoff-claude"),
        pytest.param("handoff:codex", "handoff:codex", id="handoff-codex"),
        pytest.param("handoff:Claude", "handoff:claude", id="handoff-case"),
        pytest.param("handoff:claude-code", "handoff:claude", id="handoff-alias"),
    ],
)
def test_supported_policies_are_accepted(value, expected):
    result = central_config.resolve_continuity_settings(
        {"on_coordinator_quota": value}, {}, {}
    )
    assert result["on_coordinator_quota"] == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("stop", id="unsupported-word"),
        pytest.param("handoff", id="handoff-without-target"),
        pytest.param("handoff:", id="handoff-empty-target"),
        pytest.param("handoff:gemini", id="handoff-unsupported-target"),
        pytest.param("handoff:claude:codex", id="handoff-extra-parts"),
        pytest.param("handoff: opencode", id="handoff-opencode"),
    ],
)
def test_unsupported_policies_are_rejected(value):
    with pytest.raises(CentralConfigError, match="on_coordinator_quota"):
        central_config.resolve_continuity_settings({"on_coordinator_quota": value}, {}, {})


@pytest.mark.parametrize("bad", [42, 1.5, True, False, ["ask"], {"policy": "ask"}])
def test_non_string_policies_are_rejected(bad):
    with pytest.raises(CentralConfigError, match="on_coordinator_quota must be a string"):
        central_config.resolve_continuity_settings({"on_coordinator_quota": bad}, {}, {})


def test_unsupported_handoff_target_names_the_supported_ones():
    with pytest.raises(CentralConfigError, match="claude, codex"):
        central_config.resolve_continuity_settings(
            {"on_coordinator_quota": "handoff:gemini"}, {}, {}
        )


# --- stall timing ------------------------------------------------------------

#: Shared bad timing values, as (value, expected message fragment) pairs.
BAD_TIMINGS = [
    pytest.param(math.inf, "must be finite", id="plus-inf"),
    pytest.param(-math.inf, "must be finite", id="minus-inf"),
    pytest.param(math.nan, "must be finite", id="nan"),
    pytest.param(0, "must be positive", id="zero-int"),
    pytest.param(0.0, "must be positive", id="zero-float"),
    pytest.param(-1, "must be positive", id="negative-int"),
    pytest.param(-0.5, "must be positive", id="negative-float"),
    pytest.param(True, "must be a number", id="bool-true"),
    pytest.param(False, "must be a number", id="bool-false"),
    pytest.param("40", "must be a number", id="string-digits"),
]


@pytest.mark.parametrize("value,fragment", BAD_TIMINGS)
def test_launch_stall_minutes_rejects_bad_timing(value, fragment):
    with pytest.raises(CentralConfigError, match=f"stall_minutes {fragment}"):
        central_config.resolve_continuity_settings(
            {}, {}, {"stall_minutes": value}
        )


@pytest.mark.parametrize("value,fragment", BAD_TIMINGS)
def test_project_stall_minutes_rejects_bad_timing(value, fragment):
    with pytest.raises(CentralConfigError, match=f"stall_minutes {fragment}"):
        central_config.resolve_continuity_settings({}, {"stall_minutes": value}, {})


@pytest.mark.parametrize("value,fragment", BAD_TIMINGS)
def test_global_stall_minutes_rejects_bad_timing(value, fragment):
    with pytest.raises(CentralConfigError, match=f"stall_minutes {fragment}"):
        central_config.resolve_continuity_settings({"stall_minutes": value}, {}, {})


@pytest.mark.parametrize("value", [1, 0.5, 40, 90.25])
def test_finite_positive_stall_minutes_are_kept(value):
    result = central_config.resolve_continuity_settings(
        {"stall_minutes": value}, {}, {}
    )
    assert result["stall_minutes"] == value


# --- prefs loading -----------------------------------------------------------


def test_prefs_default_to_40_and_ask(tmp_path):
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.stall_minutes == 40
    assert p.on_coordinator_quota == "ask"


def test_prefs_project_continuity_keys_win_over_global(tmp_path):
    _global(tmp_path, 'stall_minutes = 10\non_coordinator_quota = "wait"\n')
    _project(tmp_path, 'stall_minutes = 20\non_coordinator_quota = "handoff:codex"\n')
    p = load_prefs(tmp_path, global_path=tmp_path / "global.toml")
    assert p.stall_minutes == 20
    assert p.on_coordinator_quota == "handoff:codex"


def test_prefs_global_continuity_keys_apply_without_a_project_file(tmp_path):
    _global(tmp_path, 'stall_minutes = 15\non_coordinator_quota = "handoff:claude"\n')
    p = load_prefs(tmp_path, global_path=tmp_path / "global.toml")
    assert p.stall_minutes == 15
    assert p.on_coordinator_quota == "handoff:claude"


def test_prefs_read_continuity_keys_with_bom_and_crlf(tmp_path):
    _project(
        tmp_path,
        'stall_minutes = 5\r\non_coordinator_quota = "wait"\r\n',
        encoding="utf-8-sig",
        newline="",
    )
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.stall_minutes == 5
    assert p.on_coordinator_quota == "wait"


def test_prefs_continuity_keys_leave_established_settings_intact(tmp_path):
    _project(
        tmp_path,
        'central_poll_s = 2.5\n'
        "central_round_timeout_s = 600\n"
        'central_backend = "orca"\n'
        'coordinator_default = "codex"\n'
        "stall_minutes = 5\n"
        'on_coordinator_quota = "wait"\n',
    )
    p = load_prefs(tmp_path, global_path=tmp_path / "none.toml")
    assert p.central_poll_s == 2.5
    assert p.central_round_timeout_s == 600
    assert p.central_backend == "orca"
    assert p.coordinator_default == "codex"
    assert p.stall_minutes == 5
    assert p.on_coordinator_quota == "wait"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("stall_minutes = 0\n", id="zero"),
        pytest.param("stall_minutes = -1\n", id="negative"),
        pytest.param("stall_minutes = nan\n", id="nan"),
        pytest.param("stall_minutes = inf\n", id="inf"),
        pytest.param("stall_minutes = true\n", id="bool"),
        pytest.param('stall_minutes = "40"\n', id="string"),
        pytest.param('on_coordinator_quota = "handoff:gemini"\n', id="unsupported-target"),
        pytest.param('on_coordinator_quota = "stop"\n', id="unsupported-word"),
        pytest.param("on_coordinator_quota = 7\n", id="policy-number"),
    ],
)
def test_bad_continuity_key_is_rejected_in_the_project_file(tmp_path, line):
    _project(tmp_path, line)
    key = line.split("=", 1)[0].strip()
    with pytest.raises(PrefsError, match=key):
        load_prefs(tmp_path, global_path=tmp_path / "none.toml")


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("stall_minutes = 0\n", id="zero"),
        pytest.param("stall_minutes = inf\n", id="inf"),
        pytest.param('on_coordinator_quota = "handoff:opencode"\n', id="unsupported-target"),
        pytest.param('on_coordinator_quota = ""\n', id="empty"),
    ],
)
def test_bad_continuity_key_is_rejected_in_the_global_file(tmp_path, line):
    _global(tmp_path, line)
    key = line.split("=", 1)[0].strip()
    with pytest.raises(PrefsError, match=key):
        load_prefs(tmp_path, global_path=tmp_path / "global.toml")


def test_prefs_error_names_the_offending_file(tmp_path):
    path = _project(tmp_path, 'on_coordinator_quota = "handoff:gemini"\n')
    with pytest.raises(PrefsError, match=re.escape(str(path))):
        load_prefs(tmp_path, global_path=tmp_path / "none.toml")
