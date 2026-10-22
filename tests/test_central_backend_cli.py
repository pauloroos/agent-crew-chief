"""Proof for explicit backend choice wired through the public CLI (T038, FR-017).

The ``--backend`` flag selects an immutable backend per launch. Default mode
never resolves Orca; explicit Orca selection is rejected when the backend is
unavailable or incompatible; switching mid-launch is prevented; message,
attach and controls route by the stored backend name without probing Orca, so
removing or upgrading Orca never blocks an existing coordinator, while an
unknown stored backend is refused; existing JSON output and error shapes are
preserved. Every CLI is a fake Python stub, every root is temporary, and
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

from crew_chief import central_controls, central_messages, central_registry, central_supervisor, cli
from crew_chief.central_backend import default_backend
from crew_chief.central_cli import handle_launch
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


@pytest.fixture(autouse=True)
def no_installed_orca(monkeypatch):
    """Hide any Orca installed on this machine: only a test's fake may resolve."""
    keep = [
        part for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and shutil.which("orca", path=part) is None
    ]
    monkeypatch.setenv("PATH", os.pathsep.join(keep))


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
    err = capsys.readouterr().err
    assert "error: launch failed: backend 'orca' is unavailable" in err
    assert "no 'orca' executable" in err
    assert "no fallback" in err
    assert not (root / "registry.json").exists()


# --- incompatible explicit selection -----------------------------------------


def test_incompatible_backend_choice_fails_without_switching(
    repo, request_file, release, root, capsys
):
    """An unknown backend name is refused; the launch does not silently fall back."""
    args = _launch_args(repo, request_file, release, root, backend="nonexistent")

    exit_code = handle_launch(args)

    assert exit_code == 1
    assert "unknown backend 'nonexistent'" in capsys.readouterr().err
    # Refused before any checkout or registry row exists.
    assert not (root / "registry.json").exists()
    assert not (repo.parent / "repo.crew").exists()


# --- message and controls on a default row, through the public CLI ----------


def test_message_and_stop_on_a_default_row_go_through_the_cli(
    repo, request_file, release, root, started, capsys
):
    assert _cli_launch(repo, request_file, release, root) == 0
    row = _launched(capsys, root)
    assert row["backend"] == "default"

    assert _acc("message", row["id"], "--text", "hello from test", "--json",
                "--central-dir", str(root)) == 0
    message_id = json.loads(capsys.readouterr().out)["message_id"]
    pending = central_messages.pending(root, row["id"], "in")
    assert [(p["id"], p["body"]) for p in pending] == [(message_id, "hello from test")]

    assert _acc("stop", row["id"], "--central-dir", str(root)) == 0
    [control] = _controls(root, row["id"])
    assert json.loads(control.read_text(encoding="utf-8"))["command"] == "stop"


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
    repo, request_file, release, root, capsys
):
    """A launch-time failure under --json keeps the v0.4.0 error shape.

    Exit 1, one ``error: launch failed: ...`` line on stderr and nothing on
    stdout, so no consumer can parse a failure as a launched coordinator.
    """
    args = _launch_args(repo, request_file, release, root, backend="orca", json=True)

    exit_code = handle_launch(args)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: launch failed: backend 'orca' is unavailable")
    assert len(captured.err.splitlines()) == 1
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


# --- explicit Orca through the public CLI, with a fake ``orca`` on PATH ------

FAKE_ORCA = r"""
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_ORCA_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
mode = os.environ.get('FAKE_ORCA_MODE', '')
if args == ['--version']:
    print('unsupported' if mode == 'version' else '1.4.237')
elif args == ['--help']:
    print('orca agent-context worktree create --json')
elif args == ['agent-context', '--json']:
    flags = ['repo', 'name', 'setup', 'base-branch', 'no-parent', 'json']
    print(json.dumps({'schemaVersion': 1,
                      'commands': [{'command': 'worktree create', 'flags': flags}]}))
elif args[:2] == ['worktree', 'create']:
    repo = args[args.index('--repo') + 1][len('path:'):]
    name = args[args.index('--name') + 1]
    base = args[args.index('--base-branch') + 1]
    wt = str(Path(repo).parent / ('orca-' + name))
    subprocess.run(['git', 'worktree', 'add', '-b', name, wt, base],
                   cwd=repo, check=True, capture_output=True)
    print(json.dumps({'ok': True, 'result': {'worktree': {'id': 'repo-fixture::' + wt}}}))
else:
    raise SystemExit('unexpected orca command: ' + repr(args))
"""


@pytest.fixture
def fake_orca(fake_bin, tmp_path, monkeypatch) -> Path:
    """A fake ``orca`` on PATH; returns its call log (one JSON argv per line)."""
    log = tmp_path / "orca-calls.jsonl"
    monkeypatch.setenv("FAKE_ORCA_LOG", str(log))
    monkeypatch.delenv("FAKE_ORCA_MODE", raising=False)
    fake_bin("orca", FAKE_ORCA)
    return log


@pytest.fixture
def started(monkeypatch) -> list[str]:
    """Record supervisor starts instead of leaving a detached process behind."""
    calls: list[str] = []

    def fake_start(record, root):
        calls.append(record["id"])
        return {"ok": True, "error": None, "coordinator_id": record["id"], "pid": None, "argv": []}

    monkeypatch.setattr(central_supervisor, "start_supervisor", fake_start)
    return calls


def _orca_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _acc(*argv: str) -> int:
    return cli.main(list(argv))


def _cli_launch(repo, request_file, release, root, *extra: str) -> int:
    return _acc(
        "launch", "--json", "--repo", str(repo), "--request-file", str(request_file),
        "--mode", "raw-request", "--authorize-raw", "--release-root", str(release),
        "--central-dir", str(root), *extra,
    )


def _launched(capsys, root) -> dict:
    coordinator_id = json.loads(capsys.readouterr().out)["coordinator_id"]
    return central_registry.load_registry(root)["coordinators"][coordinator_id]


def _controls(root: Path, coordinator_id: str) -> list[Path]:
    control = root / "coordinators" / coordinator_id / "control"
    return sorted(control.glob("*.json")) if control.exists() else []


def test_orca_launch_creates_and_starts_through_orca(
    repo, request_file, release, root, fake_orca, started, capsys
):
    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 0

    row = _launched(capsys, root)
    assert row["backend"] == "orca"
    assert row["authorization"]["settings"]["central_backend"] == "orca"
    assert Path(row["worktree"]).name == f"orca-central-{row['id']}"
    assert started == [row["id"]]
    calls = _orca_calls(fake_orca)
    assert calls[:3] == [["--version"], ["--help"], ["agent-context", "--json"]]
    assert sum(c[:2] == ["worktree", "create"] for c in calls) == 1
    # The default checkout was never made: no switch after selection.
    assert not (repo.parent / "repo.crew").exists()


def test_orca_launch_with_missing_binary_fails_without_fallback(
    repo, request_file, release, root, started, capsys
):
    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 1

    err = capsys.readouterr().err
    assert "backend 'orca' is unavailable: no 'orca' executable" in err
    assert started == []
    assert not (root / "registry.json").exists()
    assert not (repo.parent / "repo.crew").exists()


def test_orca_launch_with_incompatible_version_fails_without_fallback(
    repo, request_file, release, root, fake_orca, started, capsys, monkeypatch
):
    monkeypatch.setenv("FAKE_ORCA_MODE", "version")

    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 1

    err = capsys.readouterr().err
    assert "backend 'orca' is unavailable: unsupported Orca version" in err
    assert "no fallback" in err
    assert started == []
    assert not any(c[:2] == ["worktree", "create"] for c in _orca_calls(fake_orca))
    assert not (root / "registry.json").exists()
    assert not (repo.parent / "repo.crew").exists()


@pytest.fixture
def orca_row(repo, request_file, release, root, fake_orca, started, capsys) -> dict:
    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 0
    row = _launched(capsys, root)
    fake_orca.unlink()
    return row


@pytest.fixture(params=["removed", "incompatible"])
def orca_gone(request, tmp_path, monkeypatch) -> str:
    """Orca is uninstalled or upgraded to an incompatible version after launch."""
    if request.param == "removed":
        for stub in (tmp_path / "bin").glob("orca*"):
            stub.unlink()
        assert shutil.which("orca") is None
    else:
        monkeypatch.setenv("FAKE_ORCA_MODE", "version")
    return request.param


def test_message_to_orca_row_needs_no_orca(root, orca_row, orca_gone, fake_orca, capsys):
    assert _acc("message", orca_row["id"], "--text", "hi orca", "--central-dir", str(root)) == 0

    assert "sent message" in capsys.readouterr().out
    assert [m["body"] for m in central_messages.pending(root, orca_row["id"], "in")] == ["hi orca"]
    # Orca only created the worktree: the core owns the lifecycle.
    assert _orca_calls(fake_orca) == []


def test_attach_to_orca_row_needs_no_orca(root, orca_row, orca_gone, fake_orca, capsys):
    # A test terminal is not interactive, so the core attach answers with its fallback.
    assert _acc("attach", orca_row["id"], "--central-dir", str(root)) == 1

    assert "non-interactive" in capsys.readouterr().err
    assert _orca_calls(fake_orca) == []


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_controls_on_orca_row_need_no_orca(
    root, orca_row, orca_gone, fake_orca, capsys, action
):
    """FR-011: an owner who removes or upgrades Orca can still steer and stop."""
    assert _acc(action, orca_row["id"], "--json", "--central-dir", str(root)) == 0

    control_id = json.loads(capsys.readouterr().out)["control_id"]
    path = root / "coordinators" / orca_row["id"] / "control" / f"{control_id}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["command"] == action
    assert _orca_calls(fake_orca) == []


def test_recover_on_orca_row_needs_no_orca(root, orca_row, orca_gone, fake_orca, capsys):
    central_registry.update_record(root, orca_row["id"], {"state": "stopped"})

    assert _acc("recover", orca_row["id"], "--central-dir", str(root)) == 0

    assert central_registry.load_registry(root)["coordinators"][orca_row["id"]]["state"] == "created"
    assert _orca_calls(fake_orca) == []


def _stored_row(tmp_path: Path, backend: str, state: str = "running") -> dict:
    """A registry row as a launch with ``backend`` would have stored it."""
    stamp = "2026-01-01T00:00:00+00:00"
    return {
        "schema_version": 1, "id": str(uuid.uuid4()),
        "repo": str(tmp_path), "worktree": str(tmp_path),
        "branch": "central/x", "base_commit": "0" * 40, "backend": backend,
        "cli": "claude", "cli_version": "", "acc_version": "", "session_id": None,
        "mode": "raw-request", "authorization": {}, "job_ids": [],
        "active_job_id": None, "state": state, "created_at": stamp,
        "updated_at": stamp, "last_error": None, "supervisor_nonce": None,
    }


def test_unknown_stored_backend_is_a_clear_error(root, tmp_path, capsys):
    row = _stored_row(tmp_path, "mystery", state="stopped")
    central_registry.register(root, row)

    for argv in (["message", row["id"], "--text", "x"], ["attach", row["id"]],
                 ["pause", row["id"]], ["stop", row["id"]], ["recover", row["id"]]):
        assert _acc(*argv, "--central-dir", str(root)) == 1
        assert "unknown backend 'mystery'" in capsys.readouterr().err
    assert central_messages.pending(root, row["id"], "in") == []
    assert _controls(root, row["id"]) == []
    assert central_registry.load_registry(root)["coordinators"][row["id"]]["state"] == "stopped"


def test_preference_change_after_launch_never_switches_backend(
    repo, request_file, release, root, fake_orca, started, capsys
):
    prefs = repo / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('central_backend = "default"\n', encoding="utf-8")
    assert _cli_launch(repo, request_file, release, root) == 0
    default_row = _launched(capsys, root)
    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 0
    orca_row = _launched(capsys, root)
    fake_orca.unlink()

    prefs.write_text('central_backend = "orca"\n', encoding="utf-8")
    assert _acc("message", default_row["id"], "--text", "d", "--central-dir", str(root)) == 0
    assert _orca_calls(fake_orca) == []

    prefs.write_text('central_backend = "default"\n', encoding="utf-8")
    assert _acc("message", orca_row["id"], "--text", "o", "--central-dir", str(root)) == 0
    assert central_controls.registered_backend(root, orca_row["id"])[1].name == "orca"
    assert _orca_calls(fake_orca) == []

    rows = central_registry.load_registry(root)["coordinators"]
    assert rows[default_row["id"]]["backend"] == "default"
    assert rows[orca_row["id"]]["backend"] == "orca"


def test_orca_and_default_rows_meet_the_same_contract(
    repo, request_file, release, root, fake_orca, started, capsys
):
    from crew_chief.central_orca import orca_backend

    assert _cli_launch(repo, request_file, release, root) == 0
    default_row = _launched(capsys, root)
    assert _cli_launch(repo, request_file, release, root, "--backend", "orca") == 0
    orca_row = _launched(capsys, root)

    assert set(default_row) == set(orca_row)
    assert default_row["state"] == orca_row["state"] == "created"
    assert started == [default_row["id"], orca_row["id"]]
    default_state = default_backend(root).state(default_row)
    orca_state = orca_backend(root, shutil.which("orca")).state(orca_row)
    assert set(default_state) == set(orca_state)
    assert (default_state["ok"], default_state["backend"]) == (True, "default")
    assert (orca_state["ok"], orca_state["backend"]) == (True, "orca")
    for row in (default_row, orca_row):
        assert _acc("message", row["id"], "--text", "same", "--json", "--central-dir", str(root)) == 0
        message_id = json.loads(capsys.readouterr().out)["message_id"]
        assert central_messages._canonical_uuid(message_id) == message_id
