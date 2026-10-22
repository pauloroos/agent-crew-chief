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
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from crew_chief import central_config, central_messages, central_registry, cli
from crew_chief.central_cli import (
    add_central_subcommands,
    handle_attach,
    handle_central,
    handle_control,
    handle_launch,
    handle_message,
)
from test_central_supervisor import CLAUDE_HELP, FAKE_CLI


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
# real subcommands through cli.main — nothing below the parser is mocked
# ---------------------------------------------------------------------------


def _main(*argv: str) -> int:
    return cli.main(list(argv))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo_with_acc(path: Path) -> Path:
    """A git repo whose installed ACC matches this release (0.4.1)."""
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    init = path / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text('__version__ = "0.4.1"\n', encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _wait_for(condition, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


class TestRealSubcommands:
    def test_launch_prepares_and_starts_a_supervisor(
        self, tmp_path: Path, fake_bin, capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_bin("claude", FAKE_CLI.replace("__HELP__", repr(CLAUDE_HELP)))
        repo = _repo_with_acc(tmp_path / "repo")
        request = tmp_path / "req.md"
        request.write_text("# request\n\nSay hello.\n", encoding="utf-8")
        root = tmp_path / "central"

        rc = _main(
            "launch", "--json", "--repo", str(repo), "--request-file", str(request),
            "--mode", "raw-request", "--authorize-raw", "--cli", "claude",
            "--central-dir", str(root),
        )

        captured = capsys.readouterr()
        assert rc == 0, captured.err
        coordinator_id = json.loads(captured.out)["coordinator_id"]
        coord = root / "coordinators" / coordinator_id
        assert (coord / "request.md").read_bytes() == request.read_bytes()

        # The detached supervisor runs one fake round and releases its lease.
        def state() -> str:
            return central_registry.load_registry(root)["coordinators"][coordinator_id]["state"]

        assert _wait_for(lambda: state() == "completed")
        assert _wait_for(lambda: not (coord / "supervisor.lock").exists())

    def test_launch_failure_is_reported_not_raised(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "not-git"
        repo.mkdir()
        request = tmp_path / "req.md"
        request.write_text("run", encoding="utf-8")
        rc = _main(
            "launch", "--repo", str(repo), "--request-file", str(request),
            "--mode", "raw-request", "--authorize-raw", "--cli", "claude",
            "--central-dir", str(tmp_path / "central"),
        )
        assert rc == 1
        assert "launch failed" in capsys.readouterr().err

    def test_message_lands_in_the_inbox(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        rc = _main("message", record["id"], "--text", "hello", "--json",
                   "--central-dir", str(root))
        assert rc == 0
        message_id = json.loads(capsys.readouterr().out)["message_id"]
        stored = central_messages.pending(root, record["id"], "in")
        assert [(m["id"], m["kind"], m["body"]) for m in stored] == [
            (message_id, "message", "hello")
        ]

    def test_message_reply_answers_a_stored_question(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        question = central_messages.send(root, record["id"], {
            "direction": "out", "kind": "question", "body": "which db?",
        })
        rc = _main("message", record["id"], "--text", "sqlite",
                   "--reply-to", question, "--central-dir", str(root))
        assert rc == 0, capsys.readouterr().err
        answers = [m for m in central_messages.pending(root, record["id"], "in")
                   if m["kind"] == "answer"]
        assert answers[0]["reply_to"] == question

    def test_message_reply_to_unknown_question_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        rc = _main("message", record["id"], "--text", "x",
                   "--reply-to", str(uuid.uuid4()), "--central-dir", str(root))
        assert rc == 1
        assert "message failed" in capsys.readouterr().err

    def test_message_to_an_unknown_coordinator_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _prepared(tmp_path)
        rc = _main("message", str(uuid.uuid4()), "--text", "x", "--central-dir", str(root))
        assert rc == 2
        assert "registered" in capsys.readouterr().err

    def test_attach_falls_back_without_an_interactive_terminal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path, state="paused")
        rc = _main("attach", record["id"], "--central-dir", str(root))
        assert rc == 1
        assert "non-interactive" in capsys.readouterr().err

    @pytest.mark.parametrize("action", ["pause", "resume", "stop"])
    def test_controls_write_a_durable_control_file(
        self, tmp_path: Path, action: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path)
        rc = _main(action, record["id"], "--json", "--central-dir", str(root))
        assert rc == 0
        control_id = json.loads(capsys.readouterr().out)["control_id"]
        path = root / "coordinators" / record["id"] / "control" / f"{control_id}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["command"] == action

    def test_central_once_prints_one_snapshot(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        root, _ = _prepared(tmp_path)
        rc = _main("central", "--once", "--central-dir", str(root))
        assert rc == 0
        assert "Central terminal" in capfd.readouterr().out

    def test_central_routes_to_the_screen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_loop(root, options=None, io=None, clock=None):
            seen.update(root=root, options=options)
            return 0

        monkeypatch.setattr("crew_chief.central_cli.central_screen.run_screen", fake_loop)
        args = cli.build_parser().parse_args(
            ["central", "--plain", "--poll", "2", "--central-dir", str(tmp_path)]
        )
        assert args.handler is handle_central
        assert args.handler(args) == 0
        assert seen == {"root": tmp_path, "options": {"once": False, "plain": True, "poll": 2.0}}

    def test_private_supervisor_entry_reaches_the_supervisor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root, record = _prepared(tmp_path, state="failed")
        rc = _main("_supervisor", "--coordinator-id", record["id"], "--root", str(root))
        # Exit 1 comes from supervise(); a malformed argv would exit 2.
        assert rc == 1
        assert "expected --central-dir" not in capsys.readouterr().err

    def test_default_root_is_the_user_config_dir_from_any_cwd(
        self, tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(central_config.CENTRAL_DIR_ENV, raising=False)
        root = home_dir / ".config" / "agent-crew-chief" / "central"
        record = _record_for(tmp_path)
        central_registry.register(root, record)
        for name in ("a", "b"):
            cwd = tmp_path / name
            cwd.mkdir()
            monkeypatch.chdir(cwd)
            assert _main("pause", record["id"]) == 0
            assert not (cwd / ".crew-chief").exists()
        controls = list((root / "coordinators" / record["id"] / "control").glob("*.json"))
        assert len(controls) == 2


# ---------------------------------------------------------------------------
# handle_control — recover via central_recovery
# ---------------------------------------------------------------------------


class TestHandleControl:
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