"""Tests for the standard crew terminal names (T02), written first."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crew_chief.terminals import ROLES, log_file, terminal_name


def test_first_attempt_has_no_suffix() -> None:
    assert terminal_name("terminais", "code", "greet", "driver", 1) == "acc-terminais-code-greet-driver"


def test_second_attempt_appends_number() -> None:
    assert terminal_name("terminais", "lane1", "cli", "driver", 2) == "acc-terminais-lane1-cli-driver-2"


def test_third_attempt_appends_number() -> None:
    assert terminal_name("terminais", "code", "auth", "engineer", 3) == "acc-terminais-code-auth-engineer-3"


def test_default_attempt_is_one() -> None:
    assert terminal_name("terminais", "code", "greet", "driver") == "acc-terminais-code-greet-driver"


@pytest.mark.parametrize("role", ROLES)
def test_every_role_is_accepted(role: str) -> None:
    assert terminal_name("terminais", "code", "greet", role) == f"acc-terminais-code-greet-{role}"


def test_strips_and_lowercases_inputs() -> None:
    assert terminal_name(" Terminais ", " CODE ", " Greet ", "Driver") == "acc-terminais-code-greet-driver"


@pytest.mark.parametrize(
    "slug, lane, job, role, attempt",
    [
        ("terminais", "code", "greet", "engineer", 1),
        ("terminais", "lane1", "cli", "driver", 2),
        ("shop", "code", "auth", "scrutineer", 10),
        ("a1", "lane2", "data", "engineer", 7),
    ],
)
def test_names_are_single_hyphen_lowercase(slug: str, lane: str, job: str, role: str, attempt: int) -> None:
    name = terminal_name(slug, lane, job, role, attempt)
    assert re.fullmatch(r"acc-[a-z0-9-]+", name), name
    assert "--" not in name
    assert not name.endswith("-")


@pytest.mark.parametrize("slug", ["", "   "])
def test_empty_slug_is_rejected(slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        terminal_name(slug, "code", "greet", "driver")


@pytest.mark.parametrize("lane", ["", "   "])
def test_empty_lane_is_rejected(lane: str) -> None:
    with pytest.raises(ValueError, match="lane"):
        terminal_name("terminais", lane, "greet", "driver")


@pytest.mark.parametrize("job", ["", "   "])
def test_empty_job_is_rejected(job: str) -> None:
    with pytest.raises(ValueError, match="job"):
        terminal_name("terminais", "code", job, "driver")


@pytest.mark.parametrize("role", ["chief", "", "ENGINEERS"])
def test_unknown_role_is_rejected(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        terminal_name("terminais", "code", "greet", role)


@pytest.mark.parametrize("attempt", [0, -1, True, "2", 1.5])
def test_bad_attempt_is_rejected(attempt: object) -> None:
    with pytest.raises(ValueError, match="attempt"):
        terminal_name("terminais", "code", "greet", "driver", attempt)  # type: ignore[arg-type]


@pytest.mark.parametrize("argument", ["slug", "lane", "job"])
@pytest.mark.parametrize("bad", ["my job", "a/b", "a_b", "a.b", "a:b", "job!", "../x"])
def test_outside_charset_is_rejected(argument: str, bad: str) -> None:
    kwargs = {"slug": "terminais", "lane": "code", "job": "greet", "role": "driver"}
    kwargs[argument] = bad
    with pytest.raises(ValueError, match=argument):
        terminal_name(**kwargs)  # type: ignore[arg-type]


def test_role_outside_charset_is_rejected() -> None:
    with pytest.raises(ValueError, match="role"):
        terminal_name("terminais", "code", "greet", "dri ver")


def test_log_file_docstring_says_background_mode_only() -> None:
    assert log_file.__doc__ is not None
    assert "background mode" in log_file.__doc__
    assert "Orca-mode workers produce no `.log` files" in log_file.__doc__


def test_log_file_is_a_path_with_the_name() -> None:
    path = log_file("../shop-acc-logs", "acc-terminais-code-greet-driver")
    assert isinstance(path, Path)
    assert path == Path("../shop-acc-logs/acc-terminais-code-greet-driver.log")


def test_terminal_name_flows_into_log_file() -> None:
    name = terminal_name("terminais", "code", "greet", "driver", 2)
    assert name == "acc-terminais-code-greet-driver-2"
    assert log_file("../shop-acc-logs", name).name == "acc-terminais-code-greet-driver-2.log"


def test_parallel_jobs_get_unique_names() -> None:
    name1 = terminal_name("terminais", "code", "greet", "driver")
    name2 = terminal_name("terminais", "code", "cli", "driver")
    assert name1 != name2
    assert name1 == "acc-terminais-code-greet-driver"
    assert name2 == "acc-terminais-code-cli-driver"


def test_parallel_scrutineers_get_unique_names() -> None:
    s1 = terminal_name("terminais", "code", "auth", "scrutineer", 1)
    s2 = terminal_name("terminais", "code", "auth", "scrutineer", 2)
    assert s1 != s2
    assert s1 == "acc-terminais-code-auth-scrutineer"
    assert s2 == "acc-terminais-code-auth-scrutineer-2"


def test_module_only_builds_strings() -> None:
    source = Path(__file__).resolve().parent.parent / "skill/crew-chief/scripts/crew_chief/terminals.py"
    text = source.read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "os.system" not in text
    assert "os.popen" not in text
    assert "tmux" not in text
    assert "shlex" not in text