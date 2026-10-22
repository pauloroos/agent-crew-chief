"""Proof for the central launch preparation (T011, FR-002..FR-006, US1/AC1, US1/AC2).

Every CLI is a fake Python stub and every repository, release and registry
root is temporary: no test runs a live model CLI, touches a real home or a
real ``~/.config``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from crew_chief import central_registry, coordinator_claude, coordinator_codex
from crew_chief.central_launch import adapter_for, prepare_launch

RELEASE_VERSION = "0.4.0"
OTHER_VERSION = "0.4.1"

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""

CODEX_STUB = r'''
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.50.0")
elif args == ["--help"]:
    print("Commands:\n  exec    Run Codex non-interactively\n  resume  Resume a previous session\n"
          "--ask-for-approval never\n--sandbox read-only workspace-write\n"
          "--dangerously-bypass-approvals-and-sandbox  Skip approvals")
elif args[:2] == ["exec", "--help"]:
    print("--json  Print events as JSONL\n  resume  Resume a previous session\n"
          "--dangerously-bypass-approvals-and-sandbox  Skip approvals")
elif args[:3] == ["exec", "resume", "--help"]:
    print("--json --dangerously-bypass-approvals-and-sandbox")
else:
    print("unexpected call")
    raise SystemExit(2)
'''


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    """Never reuse a probe cached for another test's temporary CLI."""
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


@pytest.fixture(autouse=True)
def calling_cli(monkeypatch):
    """The launch runs inside a Claude Code session."""
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")


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


def _write_acc_version(root: Path, version: str) -> Path:
    path = root / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return path


def _make_repo(path: Path, version: str = RELEASE_VERSION) -> Path:
    """A real git repository holding the released ACC."""
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    _write_acc_version(path, version)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A temporary git repository with the released ACC committed."""
    return _make_repo(tmp_path / "repo")


def _make_release(root: Path, version: str, log: Path) -> Path:
    """A released bundle with the selected installer and version."""
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "install.py").write_text(
        "\n".join([
            "import json, os, sys",
            "from pathlib import Path",
            f"log = Path({str(log)!r})",
            "log.write_text(json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}), encoding='utf-8')",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / 'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text({init.read_text(encoding='utf-8')!r}, encoding='utf-8')",
            "",
        ]),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A released bundle holding the selected installer and version."""
    return _make_release(tmp_path / "release", RELEASE_VERSION, tmp_path / "install.log")


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    path = tmp_path / "request.md"
    path.write_text("# request\n\nShip the central launch.\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The injectable central registry root."""
    return tmp_path / "central"


@pytest.fixture
def spec_dir(tmp_path: Path) -> Path:
    """An external selected spec with the four selected files."""
    directory = tmp_path / "external-spec"
    directory.mkdir()
    for name, text in (
        ("spec.md", "# spec\n"),
        ("plan.md", "# plan\n"),
        ("tasks.md", "# tasks\n"),
        ("crew.toml", "[meta]\n"),
    ):
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _fake_claude(fake_bin, log: Path, fail: bool = False) -> Path:
    """Install a fake ``claude`` that only answers probe questions."""
    script = "\n".join([
        "import json, sys",
        f"HELP = {CLAUDE_HELP!r}",
        f"LOG = {str(log)!r}",
        "with open(LOG, 'a', encoding='utf-8') as fh:",
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')",
        "if '--version' in sys.argv[1:]:",
        "    print('1.2.3 (Claude Code)')" if not fail else "    raise SystemExit(3)",
        "elif '--help' in sys.argv[1:]:",
        "    print(HELP)",
        "else:",
        "    print('unexpected call')",
        "    raise SystemExit(2)",
        "",
    ])
    fake_bin("claude", script)
    return log


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _options(release: Path, **extra) -> dict:
    options = {"mode": "raw-request", "authorize_raw": True, "release_root": release}
    options.update(extra)
    return options


# --- the durable record ------------------------------------------------------


def test_launch_registers_a_durable_record_with_authorization_evidence(
    repo, request_file, release, root, fake_bin
):
    log = _fake_claude(fake_bin, repo.parent / "claude.log")
    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is True, result["error"]
    record = result["record"]
    assert record["id"] == result["id"]
    assert record["state"] == "created"
    assert record["session_id"] is None
    assert record["cli"] == "claude"
    assert record["backend"] == "default"
    assert record["cli_version"] == "1.2.3"
    assert record["acc_version"] == RELEASE_VERSION
    assert isinstance(record["created_at"], str) and record["created_at"]
    assert isinstance(record["updated_at"], str) and record["updated_at"]
    assert record["branch"] == f"central/{record['id']}"
    assert Path(record["worktree"]).is_dir()
    assert record["authorization"]["request_digest"]
    assert record["authorization"]["authorized_at"]
    assert record["authorization"]["settings"]["coordinator_unrestricted"] is True

    stored = central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["worktree"] == record["worktree"]
    assert stored["created_at"] == record["created_at"]
    assert result["adapter"] is coordinator_claude
    # the launch only probed the CLI; it never started a model
    assert all(call in (["--version"], ["--help"]) for call in _calls(log))


def test_the_validated_request_is_persisted_next_to_the_record(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is True, result["error"]
    stored = root / "coordinators" / result["id"] / "request.md"
    assert stored.read_bytes() == request_file.read_bytes()


# --- unique worktrees, untouched sources --------------------------------------


def test_two_repositories_and_two_worktrees_stay_unique(
    repo, request_file, release, root, fake_bin, tmp_path
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    other = _make_repo(tmp_path / "other")
    heads = {target: _git(target, "rev-parse", "HEAD") for target in (repo, other)}

    ids, branches, worktrees = [], [], []
    for target in (repo, other, repo, other):
        result = prepare_launch(target, request_file, _options(release), root)
        assert result["ok"] is True, result["error"]
        record = result["record"]
        ids.append(record["id"])
        branches.append((target, record["branch"]))
        worktrees.append(record["worktree"])

    assert len(set(ids)) == 4
    assert len({branch for _, branch in branches}) == 4
    assert len(set(worktrees)) == 4
    for target, head in heads.items():
        assert _git(target, "status", "--porcelain") == ""
        assert _git(target, "rev-parse", "HEAD") == head
        assert _git(target, "remote") == ""
    for target, branch in branches:
        assert _git(target, "rev-list", "--count", branch).strip() == "1"
        # a merge commit would name two parents
        assert len(_git(target, "rev-list", "--parents", "-n", "1", branch).split()) <= 2


def test_a_dirty_source_checkout_is_preserved(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    dirty = repo / "dirty.txt"
    dirty.write_text("work in progress\n", encoding="utf-8")
    before = dirty.read_bytes()
    head = _git(repo, "rev-parse", "HEAD")

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is True, result["error"]
    assert dirty.read_bytes() == before
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (Path(result["record"]["worktree"]) / "dirty.txt").exists()


# --- CLI and permission resolution -------------------------------------------


def test_the_launch_cli_wins_over_the_project_preference_and_the_caller(
    repo, request_file, release, root, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    fake_bin("codex", CODEX_STUB)
    prefs = repo / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('coordinator_default = "codex"\n', encoding="utf-8")
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")

    result = prepare_launch(repo, request_file, _options(release, cli="codex"), root)
    assert result["ok"] is True, result["error"]
    assert result["record"]["cli"] == "codex"

    # without the launch override the project preference wins over the caller
    result = prepare_launch(repo, request_file, _options(release), root)
    assert result["ok"] is True, result["error"]
    assert result["record"]["cli"] == "codex"


def test_the_calling_cli_is_used_when_nothing_else_resolves_one(
    repo, request_file, release, root, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    monkeypatch.setenv("CREW_CHIEF_COORDINATOR", "claude")

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is True, result["error"]
    assert result["record"]["cli"] == "claude"


def test_the_restricted_override_is_honored_and_the_default_is_unrestricted(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(repo, request_file, _options(release), root)
    assert result["ok"] is True, result["error"]
    assert result["record"]["authorization"]["settings"]["coordinator_unrestricted"] is True

    result = prepare_launch(repo, request_file, _options(release, restricted=True), root)
    assert result["ok"] is True, result["error"]
    assert result["record"]["authorization"]["settings"]["coordinator_unrestricted"] is False


# --- modes and their own prerequisites ---------------------------------------


def test_approved_spec_needs_an_explicit_approval_record(
    repo, request_file, release, root, fake_bin, spec_dir
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(
        repo, request_file, _options(release, mode="approved-spec", spec_dir=spec_dir), root
    )

    assert result["ok"] is False
    assert "approved" in result["error"]
    assert result["record"]["state"] == "failed"


def test_approved_spec_copies_the_selected_spec_into_the_worktree(
    repo, request_file, release, root, fake_bin, spec_dir
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(
        repo,
        request_file,
        _options(release, mode="approved-spec", spec_dir=spec_dir, approved=True),
        root,
    )

    assert result["ok"] is True, result["error"]
    worktree = Path(result["record"]["worktree"])
    copied = Path(result["record"]["authorization"]["spec_path"])
    assert copied == worktree / "specs" / spec_dir.name
    assert (copied / "spec.md").read_text(encoding="utf-8") == "# spec\n"
    assert (copied / "crew.toml").read_text(encoding="utf-8") == "[meta]\n"


def test_raw_request_needs_explicit_authorization(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(
        repo, request_file, _options(release, authorize_raw=False), root
    )

    assert result["ok"] is False
    assert "authorization" in result["error"]


def test_direct_spec_needs_a_selected_spec_directory(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(repo, request_file, _options(release, mode="direct-spec"), root)

    assert result["ok"] is False
    assert "spec directory" in result["error"]


# --- failed preflight ---------------------------------------------------------


def test_a_failed_preflight_keeps_the_worktree_and_the_diagnostic(
    repo, request_file, release, root, fake_bin
):
    _fake_claude(fake_bin, repo.parent / "claude.log", fail=True)

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is False
    record = result["record"]
    assert record["state"] == "failed"
    assert "not available" in record["last_error"]
    assert Path(record["worktree"]).is_dir()
    # no supervisor was started
    assert not (root / "coordinators" / record["id"] / "supervisor.json").exists()


def test_a_release_without_a_readable_version_is_refused(
    repo, request_file, root, fake_bin, tmp_path
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    empty = tmp_path / "empty-release"
    empty.mkdir()

    result = prepare_launch(repo, request_file, _options(empty), root)

    assert result["ok"] is False
    assert "ACC version" in result["error"]
    assert Path(result["record"]["worktree"]).is_dir()


def test_the_release_must_be_selected_explicitly(repo, request_file, root, fake_bin):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(
        repo, request_file, {"mode": "raw-request", "authorize_raw": True}, root
    )

    assert result["ok"] is False
    assert "release_root" in result["error"]
    assert central_registry.load_registry(root)["coordinators"] == {}


def test_only_the_selected_installer_runs_and_only_in_the_worktree(
    repo, request_file, root, fake_bin, tmp_path
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    marker = tmp_path / "repo-installer-ran"
    (repo / "install.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    other = _make_release(tmp_path / "other-release", OTHER_VERSION, tmp_path / "install.log")

    result = prepare_launch(
        repo, request_file, _options(other, install_worktree_acc=True), root
    )

    assert result["ok"] is True, result["error"]
    assert not marker.exists()
    worktree = Path(result["record"]["worktree"])
    assert result["record"]["acc_version"] == OTHER_VERSION
    log = json.loads((tmp_path / "install.log").read_text(encoding="utf-8"))
    assert Path(log["cwd"]).resolve() == worktree.resolve()
    assert Path(log["argv"][1]).resolve() == worktree.resolve()


# --- preferences, flags and backends -----------------------------------------


def test_malformed_preferences_are_refused_and_never_swallowed(
    repo, request_file, release, root, fake_bin, monkeypatch, tmp_path
):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    broken = tmp_path / "broken-preferences.toml"
    broken.write_text("coordinator_default = \n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(broken))

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is False
    assert "broken-preferences.toml" in result["error"]
    assert central_registry.load_registry(root)["coordinators"] == {}


def test_a_bad_central_preference_is_refused(repo, request_file, release, root, fake_bin):
    _fake_claude(fake_bin, repo.parent / "claude.log")
    prefs = repo / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('coordinator_unrestricted = "yes"\n', encoding="utf-8")

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is False
    assert "coordinator_unrestricted" in result["error"]


@pytest.mark.parametrize("flag", ["approved", "authorize_raw", "install_worktree_acc"])
def test_boolean_launch_flags_must_be_real_booleans(
    repo, request_file, release, root, fake_bin, flag
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(repo, request_file, _options(release, **{flag: "yes"}), root)

    assert result["ok"] is False
    assert flag in result["error"]


def test_only_the_default_backend_is_supported(repo, request_file, release, root, fake_bin):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(repo, request_file, _options(release, backend="orca"), root)

    assert result["ok"] is False
    assert "backend" in result["error"]


# --- failures -----------------------------------------------------------------


def test_a_failed_registry_update_is_reported_as_a_failure(
    repo, request_file, release, root, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    def refuse(*args, **kwargs):
        raise central_registry.RegistryError("registry write refused")

    monkeypatch.setattr(central_registry, "update_record", refuse)

    result = prepare_launch(repo, request_file, _options(release), root)

    assert result["ok"] is False
    assert "registry write refused" in result["error"]


def test_a_missing_request_file_is_refused(repo, release, root, tmp_path, fake_bin):
    _fake_claude(fake_bin, repo.parent / "claude.log")

    result = prepare_launch(repo, tmp_path / "missing.md", _options(release), root)

    assert result["ok"] is False
    assert "request" in result["error"]


def test_adapter_for_selects_the_matching_adapter():
    assert adapter_for("claude") is coordinator_claude
    assert adapter_for("codex") is coordinator_codex
    with pytest.raises(ValueError):
        adapter_for("gemini")


def test_request_reads_are_bounded_before_allocation(
    repo, request_file, release, root, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, repo.parent / "cli.log")
    original = Path.read_bytes
    def checked(path):
        if path == request_file:
            raise AssertionError("request must use a bounded binary read")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", checked)
    result = prepare_launch(repo, request_file, _options(release), root)
    assert result["ok"] is True, result["error"]


def test_request_persistence_cannot_escape_the_central_root(
    repo, request_file, release, root, fake_bin, tmp_path
):
    _fake_claude(fake_bin, repo.parent / "cli.log")
    root.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "coordinators"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, shell=False)
        if result.returncode:
            pytest.skip("temporary junction unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = prepare_launch(repo, request_file, _options(release), root)
        assert result["ok"] is False
        assert list(outside.iterdir()) == []
        stored = central_registry.load_registry(root)["coordinators"][result["id"]]
        assert stored["state"] == "failed"
        assert stored["last_error"]
    finally:
        os.rmdir(link) if os.name == "nt" else link.unlink()


def test_request_persistence_failure_is_durable(
    repo, request_file, release, root, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, repo.parent / "cli.log")
    original = Path.open
    def refuse(path, mode="r", *args, **kwargs):
        if path.name == "request.md" and "coordinators" in path.parts and mode[0] in "wx":
            raise PermissionError("synthetic write refusal")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", refuse)
    result = prepare_launch(repo, request_file, _options(release), root)
    assert result["ok"] is False
    stored = central_registry.load_registry(root)["coordinators"][result["id"]]
    assert stored["state"] == "failed"
    assert "synthetic write refusal" in stored["last_error"]


def test_request_bom_crlf_bytes_and_digest_survive_preparation(repo, request_file, release, root, fake_bin):
    import hashlib
    _fake_claude(fake_bin, repo.parent / "cli.log")
    payload = b"\xef\xbb\xbfsynthetic request\r\nsecond line\r\n"
    request_file.write_bytes(payload)
    result = prepare_launch(repo, request_file, _options(release), root)
    assert result["ok"] is True, result["error"]
    stored = root / "coordinators" / result["id"] / "request.md"
    assert stored.read_bytes() == payload
    assert request_file.read_bytes() == payload
    expected = hashlib.sha256(payload.decode("utf-8-sig").encode("utf-8")).hexdigest()
    assert result["record"]["authorization"]["request_digest"] == expected
