"""Proof for the optional backend contract over the accepted core (T036, FR-017).

The default backend is the accepted v0.4.0 core. Every CLI is a fake Python
stub, every repository, release and registry root is temporary, and no test
runs a live model CLI, a live Orca executable or a real ``~/.config``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from crew_chief import central_attach, central_messages, central_registry, central_supervisor
from crew_chief import coordinator_claude
from crew_chief.central_backend import Backend, BackendError, default_backend

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
    """A temporary git repository holding the released ACC."""
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
    """A released bundle holding the selected ACC version."""
    root = tmp_path / "release"
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    (root / "install.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return root


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    path = tmp_path / "request.md"
    path.write_text("# request\n\nShip the backend contract.\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The injectable central registry root."""
    return tmp_path / "central"


@pytest.fixture(autouse=True)
def fake_claude(fake_bin, tmp_path: Path) -> Path:
    """Install a fake ``claude`` that only answers probe questions."""
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


def _options(request_file: Path, release: Path, **extra) -> dict:
    options = {
        "request_file": request_file,
        "mode": "raw-request",
        "authorize_raw": True,
        "release_root": release,
    }
    options.update(extra)
    return options


def _created(root: Path, repo: Path, request_file: Path, release: Path) -> dict:
    """One created coordinator record through the default backend."""
    result = default_backend(root).create(repo, _options(request_file, release))
    assert result["ok"] is True, result["error"]
    return result["record"]


# --- the contract ------------------------------------------------------------


def test_the_default_backend_implements_the_contract(root):
    backend = default_backend(root)

    assert isinstance(backend, Backend)
    for name in ("probe", "create", "start", "send", "attach", "state"):
        assert callable(getattr(backend, name))


def test_probe_reports_the_default_backend_without_any_executable(root):
    result = default_backend(root).probe()

    assert result["ok"] is True
    assert result["backend"] == "default"
    assert result["supported"] is True


# --- create ------------------------------------------------------------------


def test_create_registers_a_coordinator_with_the_default_backend(
    root, repo, request_file, release
):
    result = default_backend(root).create(repo, _options(request_file, release))

    assert result["ok"] is True, result["error"]
    record = result["record"]
    assert record["id"] == result["id"]
    assert record["backend"] == "default"
    assert record["cli"] == "claude"
    assert record["state"] == "created"
    assert Path(record["worktree"]).is_dir()
    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["id"] == record["id"]


def test_create_keeps_the_core_diagnostic_when_the_launch_fails(root, repo, release, tmp_path):
    missing = tmp_path / "missing.md"

    result = default_backend(root).create(repo, _options(missing, release))

    assert result["ok"] is False
    assert "request" in result["error"]


def test_create_needs_one_request_file(root, repo, release):
    options = _options(Path("unused"), release)
    options.pop("request_file")

    result = default_backend(root).create(repo, options)

    assert result["ok"] is False
    assert "request_file" in result["error"]


def test_create_refuses_an_explicit_orca_choice_instead_of_switching_backend(
    root, repo, request_file, release
):
    result = default_backend(root).create(
        repo, _options(request_file, release, backend="orca")
    )

    assert result["ok"] is False
    assert "backend" in result["error"]


# --- send --------------------------------------------------------------------


def test_send_stores_the_message_and_returns_its_id(root, repo, request_file, release):
    record = _created(root, repo, request_file, release)

    message_id = default_backend(root).send(
        record, central_messages.owner_message(record["id"], "please continue")
    )

    assert central_messages._canonical_uuid(message_id) == message_id
    pending = central_messages.pending(root, record["id"], "in")
    assert [item["id"] for item in pending] == [message_id]
    assert pending[0]["body"] == "please continue"


def test_send_reports_the_core_error(root, repo, request_file, release):
    record = _created(root, repo, request_file, release)
    envelope = central_messages.owner_message(record["id"], "hello")
    envelope["unknown"] = "field"

    with pytest.raises(BackendError) as excinfo:
        default_backend(root).send(record, envelope)

    assert "unknown envelope field" in str(excinfo.value)


# --- state -------------------------------------------------------------------


def test_state_reports_the_stored_state_and_preserves_the_ids(
    root, repo, request_file, release
):
    record = _created(root, repo, request_file, release)
    central_registry.update_record(root, record["id"], {"session_id": "session-42"})

    state = default_backend(root).state(record)

    assert state["ok"] is True
    assert state["id"] == record["id"]
    assert state["state"] == "created"
    assert state["session_id"] == "session-42"
    assert state["backend"] == "default"
    assert state["cli"] == "claude"
    assert state["worktree"] == record["worktree"]


def test_state_reports_a_missing_coordinator(root, repo, request_file, release):
    _created(root, repo, request_file, release)

    state = default_backend(root).state({"id": str(uuid.uuid4())})

    assert state["ok"] is False
    assert "no coordinator" in state["error"]


def test_state_refuses_a_malformed_coordinator_id(root):
    with pytest.raises(BackendError) as excinfo:
        default_backend(root).state({"id": "not-a-uuid"})

    assert "canonical UUID" in str(excinfo.value)


# --- start -------------------------------------------------------------------


def test_start_delegates_to_the_core_supervisor(root, repo, request_file, release, monkeypatch):
    record = _created(root, repo, request_file, release)
    seen = {}

    def fake_start(stored, supervisor_root):
        seen["record"] = stored
        seen["root"] = supervisor_root
        return {"ok": True, "error": None, "coordinator_id": record["id"], "pid": 4242}

    monkeypatch.setattr(central_supervisor, "start_supervisor", fake_start)

    result = default_backend(root).start(record)

    assert result["ok"] is True
    assert result["coordinator_id"] == record["id"]
    assert seen["record"]["id"] == record["id"]
    assert Path(seen["root"]) == root


def test_start_preserves_the_core_error(root, repo, request_file, release, monkeypatch):
    record = _created(root, repo, request_file, release)

    def fake_start(stored, supervisor_root):
        return {"ok": False, "error": "the owner lease is taken", "coordinator_id": record["id"]}

    monkeypatch.setattr(central_supervisor, "start_supervisor", fake_start)

    result = default_backend(root).start(record)

    assert result["ok"] is False
    assert result["error"] == "the owner lease is taken"


# --- attach ------------------------------------------------------------------


def test_attach_forwards_the_terminal_and_preserves_the_core_result(
    root, repo, request_file, release, monkeypatch
):
    record = _created(root, repo, request_file, release)
    terminal = object()
    seen = {}

    def fake_attach(attach_root, coordinator_id, term, timeout_s, on_open=None):
        seen["root"] = attach_root
        seen["id"] = coordinator_id
        seen["terminal"] = term
        seen["timeout_s"] = timeout_s
        return {"ok": False, "state": "paused", "reason": "busy: supervisor did not yield"}

    monkeypatch.setattr(central_attach, "attach", fake_attach)

    result = default_backend(root).attach(
        {**record, "terminal": terminal, "attach_timeout_s": 5.0}
    )

    assert result["ok"] is False
    assert result["reason"] == "busy: supervisor did not yield"
    assert seen["id"] == record["id"]
    assert seen["terminal"] is terminal
    assert seen["timeout_s"] == 5.0
    assert Path(seen["root"]) == root


# --- the default mode never needs Orca --------------------------------------


def test_the_default_mode_never_resolves_or_invokes_orca(
    root, repo, request_file, release, fake_bin, tmp_path, monkeypatch
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

    backend = default_backend(root)
    assert backend.probe()["ok"] is True
    record = _created(root, repo, request_file, release)
    message_id = backend.send(record, central_messages.owner_message(record["id"], "hi"))
    assert backend.state(record)["ok"] is True

    assert "orca" not in looked_up
    assert not marker.exists()
    assert message_id


def test_default_import_and_probe_do_not_import_orca_in_a_fresh_process(root):
    scripts = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
    code = f"""
import sys, importlib.abc
sys.path.insert(0, {str(scripts)!r})
class NoOrca(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'crew_chief.central_orca':
            raise AssertionError('default mode attempted to import Orca')
sys.meta_path.insert(0, NoOrca())
from pathlib import Path
from crew_chief.central_backend import default_backend
assert default_backend(Path({str(root)!r})).probe()['ok']
assert 'crew_chief.central_orca' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
