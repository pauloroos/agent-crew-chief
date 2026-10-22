"""Tests for interactive attach and ownership handoff (T017).

Verification boundary (tasks.md):
 Fake terminal proves yield before resume, same worktree/session, no
 competing writer, busy timeout, exception/finally release, interrupted-
 client recovery state, unsupported CLI and redirected fallback.

Proof: ``python -m pytest tests/test_central_attach.py -q``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from crew_chief import central_registry
from crew_chief.central_attach import AttachError, attach
from test_central_supervisor import CLAUDE_HELP
from test_coordinator_codex import STUB as CODEX_STUB

#: A claude that answers the probe and records an interactive run.
FAKE_CLAUDE = """
import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("1.2.3 (Claude Code)")
    raise SystemExit(0)
if "--help" in args:
    print(__HELP__)
    raise SystemExit(0)
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\\n")
raise SystemExit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""

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
# attach — the interactive session runs on the terminal (fake CLIs on PATH)
# ---------------------------------------------------------------------------


class TestAttachRunsTheSession:
    """The adapter's interactive argv runs in the worktree; state is always restored."""

    @pytest.fixture
    def claude(self, tmp_path: Path, fake_bin, monkeypatch: pytest.MonkeyPatch) -> Path:
        log = tmp_path / "claude.log"
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
        fake_bin("claude", FAKE_CLAUDE.replace("__HELP__", repr(CLAUDE_HELP)))
        return log

    def _state(self, root: Path, record: dict) -> str:
        return central_registry.load_registry(root)["coordinators"][record["id"]]["state"]

    def _controls(self, root: Path, record: dict) -> list[str]:
        directory = root / "coordinators" / record["id"] / "control"
        return sorted(
            json.loads(p.read_text(encoding="utf-8"))["command"]
            for p in directory.glob("*.json")
        )

    def test_claude_session_resumes_in_the_worktree(self, tmp_path: Path, claude: Path) -> None:
        root, record = _prepared(tmp_path, state="paused", session_id="s42")
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)

        assert result["ok"] is True, result.get("reason")
        call = json.loads(claude.read_text(encoding="utf-8").splitlines()[-1])
        assert call["argv"][:2] == ["--resume", "s42"]
        assert Path(call["cwd"]).resolve() == Path(record["worktree"]).resolve()
        assert result["state"] == "paused"
        assert self._state(root, record) == "paused"
        assert self._controls(root, record) == ["pause", "resume"]

    def test_codex_session_resumes_in_the_worktree(
        self, tmp_path: Path, fake_bin, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "codex.log"
        monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
        fake_bin("codex", CODEX_STUB)
        root, record = _prepared(tmp_path, cli="codex", state="paused", session_id="thread-1")
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)

        assert result["ok"] is True, result.get("reason")
        call = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert "resume" in call["argv"] and call["argv"][-1] == "thread-1"
        assert Path(call["cwd"]).resolve() == Path(record["worktree"]).resolve()
        assert self._state(root, record) == "paused"

    def test_failing_cli_restores_state_and_reports(
        self, tmp_path: Path, claude: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", "3")
        root, record = _prepared(tmp_path, state="paused", session_id="s42")
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)

        assert result["ok"] is False
        assert "exited with code 3" in result["reason"]
        assert self._state(root, record) == "paused"
        assert self._controls(root, record) == ["pause", "resume"]

    def test_missing_session_is_an_explicit_fallback(self, tmp_path: Path, claude: Path) -> None:
        root, record = _prepared(tmp_path, state="paused", session_id=None)
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)

        assert result["ok"] is False
        assert "session_id" in result["reason"]
        assert not claude.exists(), "no session may start without a persisted id"
        assert self._state(root, record) == "paused"

    def test_missing_cli_is_an_explicit_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        root, record = _prepared(tmp_path, state="paused", session_id="s42")
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=1.0)

        assert result["ok"] is False
        assert "cannot open the claude session" in result["reason"]
        assert self._state(root, record) == "paused"

    def test_an_attached_coordinator_is_not_attached_twice(
        self, tmp_path: Path, claude: Path
    ) -> None:
        root, record = _prepared(tmp_path, state="attached", session_id="s42")
        result = attach(root, record["id"], terminal=_FakeTerminal(), timeout_s=0.3)

        assert result["ok"] is False
        assert result["reason"].startswith("busy")
        assert not claude.exists()
        assert self._state(root, record) == "attached"
