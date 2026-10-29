"""Live git identity: refuse launch and dispatch without an author (T037).

Temporary repositories with an isolated HOME/XDG/GIT_CONFIG_* environment and
no identity: preflight fails with the actionable message before any CLI runs,
and dispatch records ``failed`` without starting the fake executor (no sentinel
file, no spend). Identity from ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` env or repo
config passes. A directory that is not a Git repository and a missing ``git``
fail closed. ACC never writes git config. No live models, no real home.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from crew_chief import coordinator_claude, records, spend
from crew_chief.central_preflight import preflight
from crew_chief.config import Prefs
from crew_chief.dispatch import dispatch
from crew_chief.dispatch import git_identity_error
from crew_chief.tools import Executor

RELEASE_VERSION = "0.4.0"


def test_non_repository_with_explicit_identity_is_still_refused(
    tmp_path, no_git_identity, no_identity_env,
):
    no_identity_env()
    directory = tmp_path / "not-a-repository"
    directory.mkdir()
    # git var alone succeeds here: identity is independent of repository state.
    assert _git(directory, "var", "GIT_AUTHOR_IDENT").returncode == 0
    assert git_identity_error(directory) is not None

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


@pytest.fixture
def no_git_identity(tmp_path, monkeypatch):
    """Isolate every git config source and remove any effective identity."""
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_home / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
        "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    return empty_home


@pytest.fixture
def no_identity_env(no_git_identity, monkeypatch):
    """The same isolation, plus the identity through both ident env pairs."""

    def _install() -> None:
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Author")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Env Author")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "author@example.invalid")

    return _install


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8",
    )


def _noid_repo(path: Path) -> Path:
    """A real git repo with one commit and no local user.name/user.email."""
    path.mkdir(parents=True)
    assert _git(path, "init").returncode == 0
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    # -c keeps the identity out of the persisted local config.
    assert _git(path, "-c", "user.name=Seed", "-c", "user.email=seed@example.com",
                     "commit", "-m", "initial").returncode == 0
    assert _git(path, "config", "--local", "user.name").returncode != 0
    return path


def _write_acc_version(worktree: Path, version: str = RELEASE_VERSION) -> None:
    init = (worktree / ".claude" / "skills" / "crew-chief" / "scripts"
            / "crew_chief" / "__init__.py")
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _release(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    init = root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    log = tmp_path / "install.log"
    (root / "install.py").write_text(
        "\n".join([
            "import json, os, sys",
            "from pathlib import Path",
            f"log = Path({str(log)!r})",
            "log.write_text(json.dumps({'argv': sys.argv}), encoding='utf-8')",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / 'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text('__version__ = \"{RELEASE_VERSION}\"\\n', encoding='utf-8')",
            "",
        ]),
        encoding="utf-8",
    )
    return root


def _fake_claude(fake_bin, log: Path) -> None:
    fake_bin("claude", "\n".join([
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
    ]))


def _fake_stub(fake_bin, sentinel: Path) -> None:
    """An executor stand-in that proves it ran by writing ``sentinel``."""
    fake_bin("stub", "\n".join([
        "from pathlib import Path",
        f"Path({str(sentinel)!r}).write_text('ran\\n', encoding='utf-8')",
        "Path('sentinel.txt').write_text('ran\\n', encoding='utf-8')",
        "print('done')",
        "",
    ]))


def _record(worktree: Path) -> dict:
    return {
        "id": "0d6f7a9e-1111-4a2b-8c3d-4e5f60718293",
        "repo": str(worktree),
        "worktree": str(worktree),
        "cli": "claude",
        "mode": "raw-request",
        "session_id": None,
        "authorization": {
            "authorize_raw": True,
            "settings": {"coordinator_unrestricted": True},
        },
    }


def _prefs() -> Prefs:
    return Prefs(
        tools=[], models=[], strong_order=[], explore_rate=0.1,
        forbid_training=False, sensitive_paths=[], issue_tracker="none",
        dispatch_timeout_s=900, review_default="",
    )


def _executor(cost: str = "cheap") -> Executor:
    tool = {"name": "stub", "kind": "generic", "bin": "stub",
            "run_cmd": ["stub", "{prompt}"], "models": ["m"]}
    return Executor(id="stub:m", tool=tool, model="m", cost=cost, trains=False)


def _seed_job(repo: Path) -> dict:
    job = records.new_job(repo, "Wire the dispatch", "do it", True, "two parts")
    records.add_piece(job, "Run one attempt", "code", "low")
    records.save_job(repo, job)
    return job


def _spend_mentions(job_id: str) -> bool:
    path = spend.spend_file()
    if not path.exists():
        return False
    return job_id in path.read_text(encoding="utf-8")


# --- preflight ---------------------------------------------------------------


def test_preflight_refuses_without_identity_before_probing(
    tmp_path, fake_bin, no_git_identity,
):
    log = tmp_path / "claude.log"
    _fake_claude(fake_bin, log)
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is False
    assert result["error"].startswith("infrastructure: git identity missing")
    assert "Git needs an author identity" in result["error"]
    assert "user.name" in result["error"]
    assert not log.exists()  # no coordinator CLI ran


def test_preflight_needs_the_committer_ident_too(
    tmp_path, fake_bin, no_git_identity, monkeypatch,
):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Env Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "author@example.invalid")

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is False
    assert result["error"].startswith("infrastructure: git identity missing")


def test_preflight_passes_with_env_identity(
    tmp_path, fake_bin, no_identity_env,
):
    log = tmp_path / "claude.log"
    _fake_claude(fake_bin, log)
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)
    no_identity_env()

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is True, result["error"]
    assert log.exists()  # the coordinator CLI was probed


def test_preflight_passes_with_repo_config_identity(
    tmp_path, fake_bin, no_git_identity,
):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)
    assert _git(worktree, "config", "user.name", "Repo User").returncode == 0
    assert _git(worktree, "config", "user.email", "repo@example.invalid").returncode == 0

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is True, result["error"]


def test_preflight_refuses_a_directory_that_is_not_a_git_repository(
    tmp_path, fake_bin, no_git_identity,
):
    log = tmp_path / "claude.log"
    _fake_claude(fake_bin, log)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _write_acc_version(worktree)

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is False
    assert result["error"].startswith("infrastructure: git identity missing")
    assert not log.exists()


def test_preflight_refuses_when_git_cannot_run(
    tmp_path, fake_bin, no_git_identity, monkeypatch,
):
    log = tmp_path / "claude.log"
    _fake_claude(fake_bin, log)
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))

    assert result["ok"] is False
    assert result["error"].startswith("infrastructure: git identity missing")
    assert not log.exists()


# --- dispatch ----------------------------------------------------------------


def test_dispatch_records_failed_without_starting_the_executor(
    tmp_path, fake_bin, no_git_identity,
):
    repo = _noid_repo(tmp_path / "repo")
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(repo)

    attempt = dispatch(repo, job["id"], 1, _executor(), "Write out.py\n", _prefs())

    assert attempt["status"] == "failed"
    assert attempt["note"].startswith("infrastructure: git identity missing")
    assert "user.name" in attempt["note"]
    assert attempt["branch"] is None
    assert attempt["worktree"] is None
    assert not sentinel.exists()
    assert not (repo.parent / f"{repo.name}.crew").exists()
    assert _spend_mentions(job["id"]) is False


def test_dispatch_refuses_a_project_that_is_not_a_git_repository(
    tmp_path, fake_bin, no_git_identity,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(repo)

    attempt = dispatch(repo, job["id"], 1, _executor(), "Write out.py\n", _prefs())

    assert attempt["status"] == "failed"
    assert attempt["note"].startswith("infrastructure: git identity missing")
    assert not sentinel.exists()


def test_dispatch_refuses_when_git_cannot_run(
    tmp_path, fake_bin, no_git_identity, monkeypatch,
):
    repo = _noid_repo(tmp_path / "repo")
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(repo)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    attempt = dispatch(repo, job["id"], 1, _executor(), "Write out.py\n", _prefs())

    assert attempt["status"] == "failed"
    assert attempt["note"].startswith("infrastructure: git identity missing")
    assert not sentinel.exists()


def test_review_is_refused_before_the_executor_runs(
    tmp_path, fake_bin, no_git_identity,
):
    repo = _noid_repo(tmp_path / "repo")
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(repo)

    attempt = dispatch(repo, job["id"], 1, _executor(), "review it\n", _prefs(),
                       role="review", review_target=repo)

    assert attempt["status"] == "failed"
    assert attempt["note"].startswith("infrastructure: git identity missing")
    assert not sentinel.exists()
    assert not (repo / ".crew-chief-brief.md").exists()


@pytest.mark.parametrize("identity", ["env", "config"])
def test_dispatch_completes_with_identity(
    tmp_path, fake_bin, no_git_identity, no_identity_env, monkeypatch, identity,
):
    repo = _noid_repo(tmp_path / "repo")
    if identity == "env":
        no_identity_env()
    else:
        assert _git(repo, "config", "user.name", "Repo User").returncode == 0
        assert _git(repo, "config", "user.email", "repo@example.invalid").returncode == 0
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(repo)

    attempt = dispatch(repo, job["id"], 1, _executor(cost="free"),
                       "Write out.py\n", _prefs())

    assert attempt["status"] == "ok", attempt["note"]
    assert (Path(attempt["worktree"]) / "sentinel.txt").exists()


def test_git_config_files_are_never_written(
    tmp_path, fake_bin, no_git_identity,
):
    log = tmp_path / "claude.log"
    _fake_claude(fake_bin, log)
    worktree = _noid_repo(tmp_path / "repo")
    _write_acc_version(worktree)
    local_config = worktree / ".git" / "config"
    before_local = local_config.read_bytes()
    missing_global = Path(no_git_identity / "no-gitconfig")
    sentinel = tmp_path / "sentinel.txt"
    _fake_stub(fake_bin, sentinel)
    job = _seed_job(worktree)

    result = preflight(
        _record(worktree), "do the thing", None, False, _release(tmp_path))
    attempt = dispatch(worktree, job["id"], 1, _executor(), "Write out.py\n", _prefs())

    assert result["ok"] is False
    assert attempt["status"] == "failed"
    assert local_config.read_bytes() == before_local
    assert _git(worktree, "config", "--local", "user.name").returncode != 0
    assert not missing_global.exists()


def test_a_guessed_identity_is_refused(tmp_path, no_git_identity, monkeypatch):
    # macOS git guesses "user@host.local" when nothing is configured; EMAIL
    # makes Linux git guess the same way. A guess is not an identity.
    repo = _noid_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Named Only")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Named Only")
    monkeypatch.setenv("EMAIL", "guessed@host.example")
    assert _git(repo, "var", "GIT_AUTHOR_IDENT").returncode == 0  # git alone accepts it
    reason = git_identity_error(repo)
    assert reason is not None and reason.startswith("var GIT_AUTHOR_IDENT")
