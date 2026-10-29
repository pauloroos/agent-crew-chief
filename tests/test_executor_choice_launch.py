"""Proof for T078 [P] [US9]: central launch --executors flag (FR-043, FR-044).

Verification boundary:
``central launch`` CLI accepts ``--executors own|subagents|free_first``.
``central_launch.py`` validates the option, records it in launch settings,
and forwards it to the coordinator supervisor bootstrap. In background
launches or when stdin is not a TTY, missing ``--executors`` defaults
silently without waiting for input or hanging.

Every CLI is a fake Python stub and every repository and registry root is
temporary: no test runs a live model CLI or touches a real home.

Proof: ``python -m pytest tests/test_executor_choice_launch.py -q``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from crew_chief import central_registry, coordinator_claude
from crew_chief.central_cli import add_central_subcommands, handle_launch
from crew_chief.central_launch import prepare_launch


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    """Never reuse a probe cached for another test's temporary CLI."""
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


@pytest.fixture(autouse=True)
def calling_cli(monkeypatch):
    """The launch runs inside a Claude Code session."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")


# ---------------------------------------------------------------------------
# parser helpers
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


# ---------------------------------------------------------------------------
# repo / release fixtures (mirrors test_central_launch patterns)
# ---------------------------------------------------------------------------

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    init = path / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text('__version__ = "0.4.2"\n', encoding="utf-8")
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


def _fake_claude(fake_bin, log: Path) -> Path:
    script = "\n".join([
        "import json, sys",
        f"HELP = {CLAUDE_HELP!r}",
        f"LOG = {str(log)!r}",
        "with open(LOG, 'a', encoding='utf-8') as fh:",
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')",
        "if '--version' in sys.argv[1:]:",
        "    print('1.2.3 (Claude Code)')",
        "elif '--help' in sys.argv[1:]:",
        "    print(HELP)",
        "else:",
        "    print('unexpected call')",
        "    raise SystemExit(2)",
        "",
    ])
    fake_bin("claude", script)
    return log


def _options(release: Path, **extra) -> dict:
    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    options.update(extra)
    return options


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "repo")


@pytest.fixture
def release(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text('__version__ = "0.4.2"\n', encoding="utf-8")
    log = tmp_path / "install.log"
    (root / "install.py").write_text(
        "\n".join([
            "import json, os, sys",
            "from pathlib import Path",
            f"log = Path({str(log)!r})",
            "log.write_text(json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}), encoding='utf-8')",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / "
            "'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text({init.read_text(encoding='utf-8')!r}, encoding='utf-8')",
            "",
        ]),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    path = tmp_path / "request.md"
    path.write_text("# request\n\nShip the executor choice.\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "central"


# ---------------------------------------------------------------------------
# parser acceptance
# ---------------------------------------------------------------------------


class TestParserAcceptance:
    def test_executors_own_parses(self) -> None:
        args = _parse([
            "launch",
            "--repo", "/r",
            "--request-file", "/req.md",
            "--mode", "raw-request",
            "--authorize-raw",
            "--executors", "own",
        ])
        assert args.command == "launch"
        assert args.executors == "own"

    def test_executors_subagents_parses(self) -> None:
        args = _parse([
            "launch",
            "--repo", "/r",
            "--request-file", "/req.md",
            "--mode", "raw-request",
            "--authorize-raw",
            "--executors", "subagents",
        ])
        assert args.executors == "subagents"

    def test_executors_free_first_parses(self) -> None:
        args = _parse([
            "launch",
            "--repo", "/r",
            "--request-file", "/req.md",
            "--mode", "raw-request",
            "--authorize-raw",
            "--executors", "free_first",
        ])
        assert args.executors == "free_first"

    def test_executors_invalid_rejected_by_parser(self) -> None:
        with pytest.raises(SystemExit):
            _parse([
                "launch",
                "--repo", "/r",
                "--request-file", "/req.md",
                "--mode", "raw-request",
                "--authorize-raw",
                "--executors", "invalid",
            ])

    def test_executors_omitted_defaults_to_none(self) -> None:
        args = _parse([
            "launch",
            "--repo", "/r",
            "--request-file", "/req.md",
            "--mode", "raw-request",
            "--authorize-raw",
        ])
        assert args.executors is None


# ---------------------------------------------------------------------------
# prepare_launch validation and recording
# ---------------------------------------------------------------------------


class TestPrepareLaunchExecutors:
    def test_valid_executors_recorded_in_settings(
        self, repo, request_file, release, root, fake_bin
    ) -> None:
        _fake_claude(fake_bin, repo.parent / "claude.log")
        for mode in ("own", "subagents", "free_first"):
            result = prepare_launch(
                repo, request_file, _options(release, executors=mode), root
            )
            assert result["ok"] is True, result.get("error")
            settings = result["record"]["authorization"]["settings"]
            assert settings["executors"] == mode

    def test_invalid_executors_rejected(
        self, repo, request_file, release, root, fake_bin
    ) -> None:
        _fake_claude(fake_bin, repo.parent / "claude.log")
        result = prepare_launch(
            repo, request_file, _options(release, executors="bogus"), root
        )
        assert result["ok"] is False
        assert "executors must be one of" in result["error"]

    def test_missing_executors_silent_default(
        self, repo, request_file, release, root, fake_bin
    ) -> None:
        """Missing --executors does not hang or prompt; settings omit the key."""
        _fake_claude(fake_bin, repo.parent / "claude.log")
        result = prepare_launch(repo, request_file, _options(release), root)
        assert result["ok"] is True, result.get("error")
        settings = result["record"]["authorization"]["settings"]
        # When omitted, the key is absent — coordinator resolves its own default.
        assert "executors" not in settings

    def test_executors_forwarded_through_authorization_settings(
        self, repo, request_file, release, root, fake_bin
    ) -> None:
        """The value survives into the persisted registry record."""
        _fake_claude(fake_bin, repo.parent / "claude.log")
        result = prepare_launch(
            repo, request_file, _options(release, executors="subagents"), root
        )
        assert result["ok"] is True, result.get("error")
        stored = central_registry.load_registry(root)["coordinators"][result["id"]]
        assert stored["authorization"]["settings"]["executors"] == "subagents"


# ---------------------------------------------------------------------------
# handle_launch integration (parser → prepare_launch)
# ---------------------------------------------------------------------------


class TestHandleLaunchExecutors:
    def test_handle_launch_forwards_executors(
        self, tmp_path: Path, fake_bin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _fake_claude(fake_bin, tmp_path / "claude.log")
        repo = _make_repo(tmp_path / "repo")
        req = tmp_path / "req.md"
        req.write_text("# request\n\nRun.\n", encoding="utf-8")
        release = tmp_path / "release"
        init = release / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text('__version__ = "0.4.2"\n', encoding="utf-8")
        install_log = tmp_path / "install.log"
        (release / "install.py").write_text(
            "\n".join([
                "import json, os, sys",
                "from pathlib import Path",
                f"log = Path({str(install_log)!r})",
                "log.write_text(json.dumps({'argv': sys.argv}), encoding='utf-8')",
                "project = Path(sys.argv[1])",
                "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / "
                "'crew_chief' / '__init__.py'",
                "init.parent.mkdir(parents=True, exist_ok=True)",
                f"init.write_text({init.read_text(encoding='utf-8')!r}, encoding='utf-8')",
            ]),
            encoding="utf-8",
        )
        central_dir = tmp_path / "central"

        args = _parse([
            "launch",
            "--repo", str(repo),
            "--request-file", str(req),
            "--mode", "raw-request",
            "--authorize-raw",
            "--executors", "own",
            "--central-dir", str(central_dir),
            "--release-root", str(release),
        ])
        args.project = str(tmp_path)
        rc = handle_launch(args)

        captured = capsys.readouterr()
        assert rc == 0, captured.err
        # The coordinator was created; verify the setting landed.
        registry = central_registry.load_registry(central_dir)
        cid = next(iter(registry["coordinators"]))
        assert registry["coordinators"][cid]["authorization"]["settings"]["executors"] == "own"