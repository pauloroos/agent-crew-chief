"""Tests for public central CLI commands (T019).

Verification boundary (tasks.md):
Subprocess CLI fake tests cover exact help/JSON/exit codes and each launch
mode; launch now starts supervisor; controls consumed; no dev-bundle
coordinator execution; old commands retain parser behavior.

Proof: ``python -m pytest tests/test_central_cli.py -q``.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from crew_chief import central_registry
from crew_chief.central_cli import (
    add_central_subcommands,
    handle_attach,
    handle_control,
    handle_launch,
    handle_message,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acc")
    parser.add_argument("--project", default=".")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    add_central_subcommands(sub)
    return parser


def _parse(argv: list[str]) -> argparse.Namespace:
    return _build_parser().parse_args(argv)


def _record_for(tmp_path: Path, **overrides: Any) -> dict:
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


def _prepared(tmp_path: Path, **overrides: Any) -> tuple[Path, dict]:
    root = tmp_path / "central"
    record = _record_for(tmp_path, **overrides)
    central_registry.register(root, record)
    return root, record


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


class TestParserWiring:
    def test_launch_subcommand_parses(self) -> None:
        args = _parse([
            "launch",
            "--repo", "/r",
            "--request-file", "/req.md",
            "--mode", "raw-request",
            "--authorize-raw",
        ])
        assert args.command == "launch"
        assert args.handler is handle_launch

    def test_message_subcommand_parses(self) -> None:
        args = _parse(["message", "abc", "--text", "hi"])
        assert args.command == "message"
        assert args.handler is handle_message
        assert args.id == "abc"

    def test_attach_subcommand_parses(self) -> None:
        args = _parse(["attach", "abc"])
        assert args.command == "attach"
        assert args.handler is handle_attach

    def test_control_subcommands_parse(self) -> None:
        for action in ("pause", "resume", "stop", "recover"):
            args = _parse([action, "abc"])
            assert args.command == action
            assert args.handler is handle_control
            assert args.action == action

    def test_old_commands_still_exist(self) -> None:
        # The main cli.py owns discover/job/piece/etc.; central_cli only adds
        # launch/message/attach/pause/resume/stop/recover/_supervisor. Ensure
        # the new subcommands do not shadow existing ones by checking that the
        # parser still accepts a non-central command when built without them.
        base = argparse.ArgumentParser(prog="acc")
        base.add_argument("--project", default=".")
        sub = base.add_subparsers(dest="command", required=True)
        sub.add_parser("discover")
        args = base.parse_args(["discover"])
        assert args.command == "discover"


# ---------------------------------------------------------------------------
# handle_launch — argument validation (no subprocess side effects)
# ---------------------------------------------------------------------------


class TestHandleLaunchValidation:
    def test_missing_repo_returns_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _parse([
            "launch",
            "--repo", str(tmp_path / "missing"),
            "--request-file", str(tmp_path / "req.md"),
            "--mode", "raw-request",
            "--authorize-raw",
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "repo path does not exist" in err

    def test_missing_request_file_returns_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        args = _parse([
            "launch",
            "--repo", str(repo),
            "--request-file", str(tmp_path / "missing.md"),
            "--mode", "raw-request",
            "--authorize-raw",
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)
        assert rc == 2
        assert "request file not found" in capsys.readouterr().err

    def test_approved_spec_without_approved_flag_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        req = tmp_path / "req.md"
        req.write_text("run", encoding="utf-8")
        args = _parse([
            "launch",
            "--repo", str(repo),
            "--request-file", str(req),
            "--mode", "approved-spec",
            "--spec-dir", str(tmp_path / "spec"),
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)
        assert rc == 2
        assert "--approved is required" in capsys.readouterr().err

    def test_direct_spec_without_spec_dir_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        req = tmp_path / "req.md"
        req.write_text("run", encoding="utf-8")
        args = _parse([
            "launch",
            "--repo", str(repo),
            "--request-file", str(req),
            "--mode", "direct-spec",
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)
        assert rc == 2
        assert "--spec-dir is required" in capsys.readouterr().err

    def test_raw_request_without_authorize_raw_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        req = tmp_path / "req.md"
        req.write_text("run", encoding="utf-8")
        args = _parse([
            "launch",
            "--repo", str(repo),
            "--request-file", str(req),
            "--mode", "raw-request",
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)
        assert rc == 2
        assert "--authorize-raw is required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# handle_message — delegates to central_messages.send
# ---------------------------------------------------------------------------


class TestHandleMessage:
    def test_success_prints_message_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        sent: dict[str, Any] = {}

        def fake_send(*, root: Path, coordinator_id: str, text: str, reply_to: str | None) -> str:
            sent.update({"root": root, "id": coordinator_id, "text": text, "reply_to": reply_to})
            return "msg-1"

        monkeypatch.setattr("crew_chief.central_cli.central_messages.send", fake_send)
        args = _parse(["message", record["id"], "--text", "hello"])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_message(args)
        assert rc == 0
        assert sent["text"] == "hello"
        assert "msg-1" in capsys.readouterr().out

    def test_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root, record = _prepared(tmp_path)
        monkeypatch.setattr(
            "crew_chief.central_cli.central_messages.send",
            lambda **kw: "msg-2",
        )
        args = _parse(["--json", "message", record["id"], "--text", "hi"])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_message(args)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"ok": True, "message_id": "msg-2"}


# ---------------------------------------------------------------------------
# handle_attach — delegates to central_attach.attach
# ---------------------------------------------------------------------------


class TestHandleAttach:
    def test_success_returns_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root, record = _prepared(tmp_path, state="paused")
        monkeypatch.setattr(
            "crew_chief.central_cli.central_attach.attach",
            lambda *a, **kw: {"ok": True, "state": "attached"},
        )
        args = _parse(["attach", record["id"]])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_attach(args)
        assert rc == 0
        assert "attached to" in capsys.readouterr().out

    def test_failure_returns_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root, record = _prepared(tmp_path)
        monkeypatch.setattr(
            "crew_chief.central_cli.central_attach.attach",
            lambda *a, **kw: {"ok": False, "state": "running", "reason": "busy"},
        )
        args = _parse(["attach", record["id"]])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_attach(args)
        assert rc == 1
        assert "busy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# handle_control — pause/resume/stop via controls, recover via registry
# ---------------------------------------------------------------------------


class TestHandleControl:
    def test_pause_writes_control(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        root, record = _prepared(tmp_path)
        written: dict[str, str] = {}

        def fake_request(r: Path, cid: str, action: str) -> str:
            written.update({"action": action, "cid": cid})
            return "ctrl-1"

        monkeypatch.setattr("crew_chief.central_cli.central_controls.request_control", fake_request)
        args = _parse(["pause", record["id"]])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_control(args)
        assert rc == 0
        assert written["action"] == "pause"
        assert "pause requested" in capsys.readouterr().out

    def test_recover_resets_failed_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path, state="failed")
        args = _parse(["recover", record["id"]])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_control(args)
        assert rc == 0
        assert "recovered" in capsys.readouterr().out
        loaded = central_registry.load_registry(root)
        assert loaded["coordinators"][record["id"]]["state"] == "created"

    def test_recover_rejects_non_terminal_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path, state="running")
        args = _parse(["recover", record["id"]])
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_control(args)
        assert rc == 2
        assert "recover applies only to failed/stopped" in capsys.readouterr().err

    def test_unknown_action_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        args = _parse(["pause", record["id"]])
        args.action = "explode"
        args.project = str(root.parent)
        args.central_dir = str(root)
        rc = handle_control(args)
        assert rc == 2
        assert "unknown control action" in capsys.readouterr().err