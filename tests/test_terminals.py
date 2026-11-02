"""Tests for the standard crew terminal names (T02), written first."""

from __future__ import annotations

import re

import pytest

from crew_chief.terminals import ROLES, terminal_name


def test_first_attempt_has_no_suffix() -> None:
    assert terminal_name("terminais", "code", "driver", 1) == "acc-terminais-code-driver"


def test_second_attempt_appends_number() -> None:
    assert terminal_name("terminais", "lane1", "driver", 2) == "acc-terminais-lane1-driver-2"


def test_third_attempt_appends_number() -> None:
    assert terminal_name("terminais", "code", "engineer", 3) == "acc-terminais-code-engineer-3"


def test_default_attempt_is_one() -> None:
    assert terminal_name("terminais", "code", "driver") == "acc-terminais-code-driver"


@pytest.mark.parametrize("role", ROLES)
def test_every_role_is_accepted(role: str) -> None:
    assert terminal_name("terminais", "code", role) == f"acc-terminais-code-{role}"


def test_strips_and_lowercases_inputs() -> None:
    assert terminal_name(" Terminais ", " CODE ", "Driver") == "acc-terminais-code-driver"


@pytest.mark.parametrize(
    "slug, lane, role, attempt",
    [
        ("terminais", "code", "engineer", 1),
        ("terminais", "lane1", "driver", 2),
        ("shop", "code", "scrutineer", 10),
        ("a1", "lane2", "engineer", 7),
    ],
)
def test_names_are_single_hyphen_lowercase(slug: str, lane: str, role: str, attempt: int) -> None:
    name = terminal_name(slug, lane, role, attempt)
    assert re.fullmatch(r"acc-[a-z0-9-]+", name), name
    assert "--" not in name
    assert not name.endswith("-")


@pytest.mark.parametrize("slug", ["", "   "])
def test_empty_slug_is_rejected(slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        terminal_name(slug, "code", "driver")


@pytest.mark.parametrize("lane", ["", "   "])
def test_empty_lane_is_rejected(lane: str) -> None:
    with pytest.raises(ValueError, match="lane"):
        terminal_name("terminais", lane, "driver")


@pytest.mark.parametrize("role", ["chief", "", "ENGINEERS"])
def test_unknown_role_is_rejected(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        terminal_name("terminais", "code", role)


@pytest.mark.parametrize("attempt", [0, -1, True, "2", 1.5])
def test_bad_attempt_is_rejected(attempt: object) -> None:
    with pytest.raises(ValueError, match="attempt"):
        terminal_name("terminais", "code", "driver", attempt)  # type: ignore[arg-type]


# --- T03: tmux command builders -------------------------------------------------

from pathlib import Path

from crew_chief.terminals import (
    log_file,
    tmux_attach,
    tmux_kill_session,
    tmux_new_session,
    tmux_pipe_pane,
    tmux_session_pid,
    tmux_start,
)


def test_log_file_is_a_path_with_the_name() -> None:
    path = log_file("../shop-acc-logs", "acc-terminais-code-driver")
    assert isinstance(path, Path)
    assert path == Path("../shop-acc-logs/acc-terminais-code-driver.log")


def test_new_session_command() -> None:
    assert (
        tmux_new_session("acc-x", "opencode run --standalone")
        == "tmux new-session -d -s acc-x 'opencode run --standalone'"
    )


def test_pipe_pane_command() -> None:
    assert tmux_pipe_pane("acc-x", "/tmp/acc-x.log") == "tmux pipe-pane -o -t acc-x 'cat >> /tmp/acc-x.log'"


def test_pipe_pane_keeps_a_path_with_spaces_quoted_inside() -> None:
    assert (
        tmux_pipe_pane("acc-x", "/tmp/my logs/acc-x.log")
        == "tmux pipe-pane -o -t acc-x 'cat >> \"/tmp/my logs/acc-x.log\"'"
    )


def test_session_pid_command() -> None:
    assert tmux_session_pid("acc-x") == "tmux display-message -p -t acc-x '#{session_pid}'"


def test_attach_command() -> None:
    assert tmux_attach("acc-x") == "tmux attach -t acc-x"


def test_kill_session_command() -> None:
    assert tmux_kill_session("acc-x") == "tmux kill-session -t acc-x"


def test_start_returns_the_three_commands_in_order() -> None:
    commands = tmux_start("acc-x", Path("/tmp/acc-x.log"), "opencode run")
    assert isinstance(commands, list)
    assert len(commands) == 3
    assert commands == [
        tmux_new_session("acc-x", "opencode run"),
        tmux_pipe_pane("acc-x", "/tmp/acc-x.log"),
        tmux_session_pid("acc-x"),
    ]


def test_terminal_name_flows_into_the_commands() -> None:
    name = terminal_name("terminais", "code", "driver", 2)
    assert name == "acc-terminais-code-driver-2"
    assert log_file("../shop-acc-logs", name).name == "acc-terminais-code-driver-2.log"
    assert tmux_kill_session(name) == "tmux kill-session -t acc-terminais-code-driver-2"
    assert tmux_attach(name) == "tmux attach -t acc-terminais-code-driver-2"


def test_module_only_builds_strings() -> None:
    source = Path(__file__).resolve().parent.parent / "skill/crew-chief/scripts/crew_chief/terminals.py"
    text = source.read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "os.system" not in text
    assert "os.popen" not in text
