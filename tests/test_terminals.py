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
