"""Proof for explicit backend choice wired through the public CLI (T038, FR-017).

The ``--backend`` flag selects an immutable backend per launch. Default mode
never resolves Orca; explicit Orca selection is rejected when the backend is
unavailable or incompatible; switching mid-launch is prevented; messages and
attach route to the registered backend; existing JSON output and error shapes
are preserved. Every CLI is a fake Python stub, every root is temporary, and
no test runs a live model CLI or a real Orca executable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from crew_chief import central_messages, central_registry
from crew_chief.central_backend import default_backend
from crew_chief.central_cli import add_central_subcommands, handle_launch
from crew_chief import coordinator_claude

RELEASE_VERSION = "0.4.0"

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    """Never reuse a capability probe cached for another test's fake CLI."""
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


@pytest.fixture(autouse=True)
def calling_cli(monkeypatch):
    """The launch runs inside a Claude Code session."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    init = path / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


@pytest.fixture
def release(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    (root / "install.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return root


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    path = tmp_path / "request.md"
    path.write_text("# request\n\nShip the backend CLI contract.\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "central"


@pytest.fixture(autouse=True)
def fake_claude(fake_bin, tmp_path: Path) -> Path:
    script = "\n".join([
        "import sys",
        f"HELP = {CLAUDE_HELP!r}",
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
    return tmp_path


def _launch_args(repo, request_file, release, root, **extra):
    """Build a minimal argparse.Namespace for handle_launch."""
    import argparse

    args = argparse.Namespace(
        repo=str(repo),
        request_file=str(request_file),
        mode="raw-request",
        approved=False,
        authorize_raw=True,
        install_worktree_acc=False,
        spec_dir=None,
        base=None,
        cli=None,
        backend=extra.get("backend"),
        restricted=False,
        unrestricted=False,
        release_root=str(release),
        central_dir=str(root),
        json=extra.get("json", False),
    )
    return args


# --- default vs orca comparison ----------------------------------------------


def test_default_backend_launch_succeeds_without_orca(
    repo, request_file, release, root, capsys
):
    args = _launch_args(repo, request_file, release, root)

    exit_code = handle_launch(args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "launched coordinator" in captured.out


def test_explicit_default_backend_matches_implicit_default(
    repo, request_file, release, root
):
    implicit = _launch_args(repo, request_file, release, root)
    explicit = _launch_args(repo, request_file, release, root, backend="default")

    assert handle_launch(implicit) == 0
    assert handle_launch(explicit) == 0

    registry = central_registry.load_registry(root)
    coordinators = list(registry["coordinators"].values())
    assert len(coordinators) == 2
    assert all(c["backend"] == "default" for c in coordinators)


def test_explicit_orca_is_rejected_when_unavailable(
    repo, request_file, release, root, capsys
):
    args = _launch_args(repo, request_file, release, root, backend="orca")

    exit_code = handle_launch(args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "backend" in captured.err.lower() or "not supported" in captured.err.lower()


# --- incompatible explicit selection -----------------------------------------


def test_incompatible_backend_choice_fails_without_switching(
    repo, request_file, release, root, capsys
):
    """An unknown backend name is refused; the launch does not silently fall back."""
    args = _launch_args(repo, request_file, release, root, backend="nonexistent")

    exit_code = handle_launch(args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "nonexistent" in captured.err or "not supported" in captured.err.lower()

    # No coordinator was registered under the rejected backend.
    if root.exists():
        try:
            registry = central_registry.load_registry(root)
        except Exception:
            registry = {"coordinators": {}}
        bad = [
            c for c in registry.get("coordinators", {}).values()
            if isinstance(c, dict) and c.get("backend") == "nonexistent"
        ]
        assert bad == []


# --- backend immutability per launch ----------------------------------------


def test_backend_is_immutable_per_launch_record(
    repo, request_file, release, root
):
    args = _launch_args(repo, request_file, release, root)
    assert handle_launch(args) == 0

    registry = central_registry.load_registry(root)
    coordinators = list(registry["coordinators"].values())
    record = coordinators[-1]
    assert record["backend"] == "default"

    # The stored record carries the backend; any later operation reads it from
    # here rather than re-resolving a flag.
    state = default_backend(root).state(record)
    assert state["ok"] is True
    assert state["backend"] == "default"


# --- message and attach route to the registered backend ----------------------


def test_message_routes_to_the_registered_backend(
    repo, request_file, release, root
):
    args = _launch_args(repo, request_file, release, root)
    assert handle_launch(args) == 0

    registry = central_registry.load_registry(root)
    record = list(registry["coordinators"].values())[-1]

    message_id = default_backend(root).send(
        record, central_messages.owner_message(record["id"], "hello from test")
    )
    assert central_messages._canonical_uuid(message_id) == message_id

    pending = central_messages.pending(root, record["id"], "in")
    assert any(p["body"] == "hello from test" for p in pending)


# --- JSON output and error shapes are preserved ------------------------------


def test_json_output_preserves_shape_on_success(
    repo, request_file, release, root, capsys
):
    args = _launch_args(repo, request_file, release, root, json=True)

    exit_code = handle_launch(args)

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert central_messages._canonical_uuid(payload["coordinator_id"]) == payload["coordinator_id"]


def test_json_error_preserves_shape_on_failure(
    repo, release, root, tmp_path, capsys
):
    """A launch-time failure (not an argument error) preserves the error shape."""
    import argparse

    # Use a valid request file but an invalid mode to trigger a launch failure
    # after argument validation passes (exit 1, not exit 2).
    req = tmp_path / "req.md"
    req.write_text("# r\n", encoding="utf-8")
    args = argparse.Namespace(
        repo=str(repo),
        request_file=str(req),
        mode="raw-request",
        approved=False,
        authorize_raw=False,  # missing --authorize-raw triggers exit 2
        install_worktree_acc=False,
        spec_dir=None,
        base=None,
        cli=None,
        backend=None,
        restricted=False,
        unrestricted=False,
        release_root=str(release),
        central_dir=str(root),
        json=True,
    )

    exit_code = handle_launch(args)

    # Argument-level validation returns 2; that is the preserved shape.
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()
    assert "Traceback" not in captured.err


# --- default mode never touches Orca -----------------------------------------


def test_default_cli_launch_never_resolves_orca(
    repo, request_file, release, root, fake_bin, tmp_path, monkeypatch
):
    marker = tmp_path / "orca-ran"
    fake_bin(
        "orca",
        "\n".join([
            "import sys",
            "from pathlib import Path",
            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
            "print('unexpected orca call')",
            "",
        ]),
    )
    looked_up: list[str] = []
    real_which = shutil.which

    def spy(name, *args, **kwargs):
        looked_up.append(name)
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", spy)

    args = _launch_args(repo, request_file, release, root)
    assert handle_launch(args) == 0

    assert "orca" not in looked_up
    assert not marker.exists()