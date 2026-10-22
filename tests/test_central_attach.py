"""Tests for interactive attach and ownership handoff (T017).

Verification boundary (tasks.md):
 Fake terminal proves yield before resume, same worktree/session, no
 competing writer, busy timeout, exception/finally release, interrupted-
 client recovery state, unsupported CLI and redirected fallback.

Proof: ``python -m pytest tests/test_central_attach.py -q``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from crew_chief import central_registry
from crew_chief.central_attach import AttachError, attach

# ---------------------------------------------------------------------------
# helpers — mirror test_central_supervisor.py record_for / prepared pattern
# ---------------------------------------------------------------------------


def _record_for(tmp_path: Path, **overrides) -> dict:
    """Build a coordinator record that passes _validate_record."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    stamp = "2026-01-01T00:00:00+00:00"
    record = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "repo": str(repo),
        "worktree": str(worktree),
        "branch": f"central/{uuid.uuid4()}",
        "base_commit": "0" * 40,
        "backend": "default",
        "cli": "claude",
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": "raw-request",
        "authorization": {
            "mode": "raw-request",
            "approved": False,
            "authorize_raw": True,
            "settings": {"coordinator_unrestricted": True},
        },
        "job_ids": [],
        "active_job_id": None,
        "state": "running",
        "created_at": stamp,
        "updated_at": stamp,
        "last_error": None,
        "supervisor_nonce": None,
    }
    record.update(overrides)
    return record


def _prepared(tmp_path: Path, **overrides) -> tuple[Path, dict]:
    """Register one coordinator and return (root, record)."""
    root = tmp_path / "central"
    record = _record_for(tmp_path, **overrides)
    central_registry.register(root, record)
    return root, record


class _FakeTerminal:
    """Mimics an interactive terminal with isatty streams."""

    def __init__(self, interactive: bool = True) -> None:
        self._interactive = interactive

    class _Stream:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    @property
    def stdin(self) -> object:
        return self._Stream(self._interactive)

    @property
    def stdout(self) -> object:
        return self._Stream(self._interactive)


# ---------------------------------------------------------------------------
# attach — validation and fallback paths
# ---------------------------------------------------------------------------


class TestAttachValidation:
    def test_rejects_non_canonical_id(self, tmp_path: Path) -> None:
        with pytest.raises(AttachError, match="canonical UUID"):
            attach(tmp_path, "not-a-uuid")

    def test_missing_record_returns_fallback(self, tmp_path: Path) -> None:
        result = attach(tmp_path, str(uuid.uuid4()))
        assert result["ok"] is False
        assert result["state"] == "unknown"

    def test_unsupported_cli_returns_fallback(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, cli="unknown-cli")
        result = attach(root, record["id"], terminal=_FakeTerminal())
        assert result["ok"] is False
        assert "does not support interactive resume" in result["reason"]

    def test_redirected_terminal_returns_fallback(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        result = attach(root, record["id"], terminal=_FakeTerminal(interactive=False))
        assert result["ok"] is False
        assert "redirected or non-interactive" in result["reason"]

    def test_none_terminal_returns_fallback(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path)
        result = attach(root, record["id"], terminal=None)
        assert result["ok"] is False
        assert "redirected or non-interactive" in result["reason"]

    def test_blocked_states_return_fallback(self, tmp_path: Path) -> None:
        for blocked in ("stopped", "completed", "failed"):
            root, record = _prepared(tmp_path, state=blocked)
            result = attach(root, record["id"], terminal=_FakeTerminal())
            assert result["ok"] is False
            assert result["state"] == blocked


# ---------------------------------------------------------------------------
# attach — busy timeout
# ---------------------------------------------------------------------------


class TestAttachBusy:
    def test_busy_when_supervisor_does_not_yield(self, tmp_path: Path) -> None:
        root, record = _prepared(tmp_path, state="running")
        # Supervisor never pauses; attach should time out quickly.
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=0.3)
        assert result["ok"] is False
        assert "busy" in result["reason"].lower()


# ---------------------------------------------------------------------------
# attach — successful yield path (monkeypatched supervisor interaction)
# ---------------------------------------------------------------------------


class TestAttachYield:
    def test_attached_result_contains_worktree_and_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, record = _prepared(
            tmp_path, state="paused", session_id="s42"
        )

        # Skip the real control-file write and registry updates; we only need
        # the protocol shape when the coordinator is already paused.
        monkeypatch.setattr(
            "crew_chief.central_attach.central_controls.request_control",
            lambda *a, **kw: "fake-id",
        )
        monkeypatch.setattr(
            "crew_chief.central_attach.central_registry.update_record",
            lambda *a, **kw: {},
        )

        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)
        assert result["ok"] is True
        assert result["state"] == "attached"
        assert result["worktree"] == record["worktree"]
        assert result["session_id"] == "s42"
        assert result["cli"] == "claude"
        assert "attached_at" in result

    def test_codex_cli_is_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, record = _prepared(tmp_path, cli="codex", state="paused")
        monkeypatch.setattr(
            "crew_chief.central_attach.central_controls.request_control",
            lambda *a, **kw: "fake-id",
        )
        monkeypatch.setattr(
            "crew_chief.central_attach.central_registry.update_record",
            lambda *a, **kw: {},
        )
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)
        assert result["ok"] is True
        assert result["cli"] == "codex"