"""Tests for shared central quota cooldowns (T020).

Verification boundary (tasks.md):
Two coordinator fake jobs share same-family expiry, unrelated families
proceed, latest expiry wins; supervisor and executor quota both observed;
no change to budgets/free-first; absent central context preserves baseline.

Proof: ``python -m pytest tests/test_central_quota.py -q``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crew_chief.central_quota import blocked, cooling_families, observe


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _future(minutes: float = 30.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


def _past(minutes: float = 5.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(
        timespec="seconds"
    )


@pytest.fixture(autouse=True)
def _central_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test runs inside a central coordinator context."""
    monkeypatch.setenv("CREW_CHIEF_CENTRAL_ID", "test-central")


# ---------------------------------------------------------------------------
# observe + blocked — basic contract
# ---------------------------------------------------------------------------

class TestObserveBlocked:
    def test_observe_creates_family_file(self, tmp_path: Path) -> None:
        observe(tmp_path, "opencode:beam", "coord-1", _future())
        assert blocked(tmp_path, "opencode:beam")

    def test_blocked_false_when_no_record(self, tmp_path: Path) -> None:
        assert not blocked(tmp_path, "missing-family")

    def test_expired_cooldown_is_not_blocking(self, tmp_path: Path) -> None:
        observe(tmp_path, "expired", "c", _past())
        assert not blocked(tmp_path, "expired")

    def test_latest_expiry_wins(self, tmp_path: Path) -> None:
        short = _future(5)
        long = _future(60)
        observe(tmp_path, "fam", "a", short)
        observe(tmp_path, "fam", "b", long)
        # Still blocked after the short one would have expired.
        now = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert blocked(tmp_path, "fam", now=now)

    def test_earlier_expiry_does_not_shorten(self, tmp_path: Path) -> None:
        long = _future(60)
        short = _future(5)
        observe(tmp_path, "fam", "a", long)
        observe(tmp_path, "fam", "b", short)
        now = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert blocked(tmp_path, "fam", now=now)

    def test_invalid_until_is_ignored(self, tmp_path: Path) -> None:
        observe(tmp_path, "bad", "c", "not-a-date")
        assert not blocked(tmp_path, "bad")

    def test_empty_family_is_ignored(self, tmp_path: Path) -> None:
        observe(tmp_path, "", "c", _future())
        assert not blocked(tmp_path, "")


# ---------------------------------------------------------------------------
# cooling_families
# ---------------------------------------------------------------------------

class TestCoolingFamilies:
    def test_returns_active_families(self, tmp_path: Path) -> None:
        observe(tmp_path, "alpha", "c1", _future())
        observe(tmp_path, "beta", "c2", _future())
        observe(tmp_path, "gamma", "c3", _past())
        active = cooling_families(tmp_path)
        assert active == {"alpha", "beta"}

    def test_empty_when_no_quota_dir(self, tmp_path: Path) -> None:
        assert cooling_families(tmp_path) == set()


# ---------------------------------------------------------------------------
# context gate — no-op outside central context
# ---------------------------------------------------------------------------

class TestContextGate:
    def test_observe_noop_without_central_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CREW_CHIEF_CENTRAL_ID", raising=False)
        observe(tmp_path, "fam", "c", _future())
        # Re-enable to check that nothing was written.
        monkeypatch.setenv("CREW_CHIEF_CENTRAL_ID", "x")
        assert not blocked(tmp_path, "fam")

    def test_blocked_false_without_central_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe(tmp_path, "fam", "c", _future())
        monkeypatch.delenv("CREW_CHIEF_CENTRAL_ID", raising=False)
        assert not blocked(tmp_path, "fam")

    def test_cooling_families_empty_without_central_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe(tmp_path, "fam", "c", _future())
        monkeypatch.delenv("CREW_CHIEF_CENTRAL_ID", raising=False)
        assert cooling_families(tmp_path) == set()


# ---------------------------------------------------------------------------
# cross-coordinator sharing
# ---------------------------------------------------------------------------

class TestCrossCoordinator:
    def test_two_coordinators_share_same_family_expiry(self, tmp_path: Path) -> None:
        observe(tmp_path, "shared", "coord-A", _future(10))
        observe(tmp_path, "shared", "coord-B", _future(30))
        # Both coordinators see the family as blocked.
        assert blocked(tmp_path, "shared")

    def test_unrelated_families_proceed_independently(self, tmp_path: Path) -> None:
        observe(tmp_path, "blocked-fam", "c1", _future())
        assert blocked(tmp_path, "blocked-fam")
        assert not blocked(tmp_path, "other-fam")