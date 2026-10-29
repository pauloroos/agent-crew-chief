"""Proof for the central launch preflight (T010, FR-004/005/021, US1/AC2/AC4).

Every CLI here is a fake Python stub and every release is a temporary bundle:
no test runs a live model CLI, touches a real home or reads real preferences.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from crew_chief import central_preflight, coordinator_claude
from crew_chief.central_preflight import preflight

RELEASE_VERSION = "0.4.0"
OLD_VERSION = "0.3.3"

HELP_FULL = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "dontAsk", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""

HELP_NO_STREAM_JSON = """Usage: claude [options]

Options:
  -p, --print                       Print response and exit
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --dangerously-skip-permissions    Bypass all permission checks
"""

HELP_NO_DONTASK = """Usage: claude [options]

Options:
  -p, --print                       Print response and exit
  --output-format <format>          Output format (text, json, stream-json)
  --verbose                         Verbose output
  -r, --resume [sessionId]          Resume a conversation
  --permission-mode <mode>          Permission mode (choices: "default", "plan")
  --dangerously-skip-permissions    Bypass all permission checks
"""


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    """Never reuse a probe cached for another test's temporary CLI."""
    monkeypatch.setattr(coordinator_claude, "_PROBED", {})


def _fake_claude(fake_bin, log: Path, help_text: str = HELP_FULL) -> Path:
    """Install a fake ``claude`` that records argv and never calls a model."""
    script = "\n".join([
        "import json, sys",
        f"HELP = {help_text!r}",
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


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


CODEX_STUB = r"""
import os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.50.0")
elif args == ["--help"]:
    print("Commands:\n  exec    Run Codex non-interactively\n  resume  Resume a session\n"
          "--ask-for-approval never\n--sandbox read-only workspace-write\n"
          "--dangerously-bypass-approvals-and-sandbox  Skip approvals")
elif args[:2] == ["exec", "--help"]:
    print(os.environ.get("FAKE_CODEX_EXEC_HELP", "--json\n  resume  Resume a session\n"
                         "--dangerously-bypass-approvals-and-sandbox"))
elif args[:3] == ["exec", "resume", "--help"]:
    print("--json --dangerously-bypass-approvals-and-sandbox")
else:
    print("unexpected call")
    raise SystemExit(2)
"""


def _fake_codex(fake_bin, monkeypatch, exec_help: str | None = None) -> None:
    """Install a fake ``codex``; it only answers probe questions."""
    fake_bin("codex", CODEX_STUB)
    if exec_help is not None:
        monkeypatch.setenv("FAKE_CODEX_EXEC_HELP", exec_help)


def _write_version(root: Path, version: str) -> Path:
    path = root / ".claude" / "skills" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    return path


def _release(tmp_path: Path, version: str = RELEASE_VERSION) -> Path:
    """A released bundle holding the selected installer and version."""
    root = tmp_path / "release"
    release_init = (
        root / "skill" / "crew-chief" / "scripts" / "crew_chief" / "__init__.py"
    )
    release_init.parent.mkdir(parents=True, exist_ok=True)
    release_init.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    log = tmp_path / "install.log"
    payload = f'__version__ = "{version}"\n'
    (root / "install.py").write_text(
        "\n".join([
            "import json, os, sys",
            "from pathlib import Path",
            f"log = Path({str(log)!r})",
            "log.write_text(json.dumps({'argv': sys.argv, 'cwd': os.getcwd()}), encoding='utf-8')",
            "project = Path(sys.argv[1])",
            "init = project / '.claude' / 'skills' / 'crew-chief' / 'scripts' / 'crew_chief' / '__init__.py'",
            "init.parent.mkdir(parents=True, exist_ok=True)",
            f"init.write_text({payload!r}, encoding='utf-8')",
            "",
        ]),
        encoding="utf-8",
    )
    return root


def _install_log(tmp_path: Path) -> Path:
    return tmp_path / "install.log"


def _worktree(tmp_path: Path, version: str | None = RELEASE_VERSION) -> Path:
    worktree = tmp_path / "repo.crew" / "central-0123"
    worktree.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        for args in (
            ["init", "-q"],
            ["config", "user.name", "Test Author"],
            ["config", "user.email", "test@example.invalid"],
        ):
            subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)
    if version is not None:
        _write_version(worktree, version)
    return worktree


def _spec_dir(tmp_path: Path, **overrides: str) -> Path:
    directory = tmp_path / "external-spec"
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "spec.md": "# spec\n",
        "plan.md": "# plan\n",
        "tasks.md": "# tasks\n",
        "crew.toml": "[meta]\n",
    }
    files.update(overrides)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def _record(worktree: Path, mode: str = "direct-spec", **authorization) -> dict:
    authorization.setdefault("settings", {"coordinator_unrestricted": True})
    return {
        "id": "0d6f7a9e-1111-4a2b-8c3d-4e5f60718293",
        "repo": str(worktree),
        "worktree": str(worktree),
        "cli": "claude",
        "mode": mode,
        "session_id": None,
        "authorization": authorization,
    }


# --- mode and authorization -------------------------------------------------


def test_approved_spec_without_an_approval_record_never_probes(tmp_path, fake_bin):
    calls = _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    result = preflight(
        _record(worktree, "approved-spec"),
        "do the thing",
        _spec_dir(tmp_path),
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is False
    assert "approv" in result["error"]
    assert _calls(calls) == []


def test_raw_request_without_authorization_is_refused(tmp_path, fake_bin):
    calls = _fake_claude(fake_bin, tmp_path / "claude.log")
    result = preflight(
        _record(_worktree(tmp_path), "raw-request"),
        "do the thing",
        None,
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is False
    assert "authoriz" in result["error"]
    assert _calls(calls) == []


def test_raw_request_with_explicit_authorization_needs_no_spec(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    result = preflight(
        _record(worktree, "raw-request", authorize_raw=True),
        "do the thing",
        None,
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is True
    assert result["spec_dir"] is None
    assert not (worktree / "specs").exists()


def test_direct_spec_needs_no_approval_record(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    result = preflight(
        _record(_worktree(tmp_path), "direct-spec"),
        "do the thing",
        _spec_dir(tmp_path),
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is True
    assert result["authorization"]["approved"] is False


def test_unknown_mode_is_refused_before_any_probe(tmp_path, fake_bin):
    calls = _fake_claude(fake_bin, tmp_path / "claude.log")
    result = preflight(
        _record(_worktree(tmp_path), "yolo"),
        "do the thing",
        _spec_dir(tmp_path),
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is False
    assert "mode" in result["error"]
    assert _calls(calls) == []


def test_approved_spec_digest_covers_every_selected_file(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    spec = _spec_dir(tmp_path)
    record = _record(worktree, "approved-spec", approved=True)
    first = preflight(record, "do it", spec, False, _release(tmp_path))
    assert first["ok"] is True
    digest = first["authorization"]["spec_digest"]

    (spec / "plan.md").write_text("# plan changed\n", encoding="utf-8")
    record["authorization"]["spec_digest"] = digest
    second = preflight(record, "do it", spec, False, _release(tmp_path))
    assert second["ok"] is False
    assert "changed" in second["error"]


def test_approved_spec_requires_a_spec_directory(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    result = preflight(
        _record(_worktree(tmp_path), "approved-spec", approved=True),
        "do it",
        None,
        False,
        _release(tmp_path),
    )
    assert result["ok"] is False
    assert "spec" in result["error"]


# --- CLI capabilities -------------------------------------------------------


def test_missing_cli_is_a_visible_error_and_starts_nothing(tmp_path, monkeypatch):
    # Keep git reachable: the identity check fails closed without it, which is
    # a separate diagnostic from the missing coordinator CLI checked here.
    monkeypatch.setenv("PATH", str(Path(shutil.which("git")).parent))
    result = preflight(
        _record(_worktree(tmp_path)),
        "do it",
        _spec_dir(tmp_path),
        False,
        _release(tmp_path),
    )
    assert result["ok"] is False
    assert "claude" in result["error"]
    assert result["probe"]["available"] is False


def test_missing_required_capability_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log", HELP_NO_STREAM_JSON)
    result = preflight(
        _record(_worktree(tmp_path)),
        "do it",
        _spec_dir(tmp_path),
        False,
        _release(tmp_path),
    )
    assert result["ok"] is False
    assert "stream_json" in result["error"]


def test_restricted_mode_requires_a_deny_without_prompt_capability(
    tmp_path, fake_bin
):
    _fake_claude(fake_bin, tmp_path / "claude.log", HELP_NO_DONTASK)
    record = _record(_worktree(tmp_path))
    record["authorization"]["settings"]["coordinator_unrestricted"] = False
    result = preflight(record, "do it", _spec_dir(tmp_path), False, _release(tmp_path))
    assert result["ok"] is False
    assert "dontAsk" in result["error"]


def test_codex_passes_only_with_its_required_capabilities(
    tmp_path, fake_bin, monkeypatch
):
    _fake_codex(fake_bin, monkeypatch)
    record = _record(_worktree(tmp_path))
    record["cli"] = "codex"
    result = preflight(record, "do it", _spec_dir(tmp_path), False, _release(tmp_path))
    assert result["ok"] is True
    assert result["cli"] == "codex"
    assert result["probe"]["available"] is True


def test_codex_with_an_installed_but_unsupported_cli_is_refused(
    tmp_path, fake_bin, monkeypatch
):
    _fake_codex(fake_bin, monkeypatch, exec_help="resume without json output")
    record = _record(_worktree(tmp_path))
    record["cli"] = "codex"
    result = preflight(record, "do it", _spec_dir(tmp_path), False, _release(tmp_path))
    assert result["ok"] is False
    assert "codex capabilities" in result["error"]
    assert not (tmp_path / "repo.crew" / "central-0123" / "specs").exists()


# --- worktree ACC version ---------------------------------------------------


def test_matching_worktree_acc_version_passes_without_installing(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    result = preflight(
        _record(worktree),
        "do it",
        _spec_dir(tmp_path),
        False,
        _release(tmp_path),
    )
    assert result["ok"] is True
    assert result["acc"] == {
        "installed": RELEASE_VERSION,
        "release": RELEASE_VERSION,
        "action": "none",
    }
    assert not _install_log(tmp_path).exists()


def test_declined_mismatch_leaves_the_worktree_and_preferences_alone(
    tmp_path, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    prefs = tmp_path / "global-preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))
    worktree = _worktree(tmp_path, OLD_VERSION)
    init = worktree / ".claude/skills/crew-chief/scripts/crew_chief/__init__.py"
    result = preflight(
        _record(worktree),
        "do it",
        _spec_dir(tmp_path),
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is False
    assert result["acc"]["action"] == "mismatch"
    assert result["acc"]["installed"] == OLD_VERSION
    assert OLD_VERSION in result["error"] and RELEASE_VERSION in result["error"]
    assert not _install_log(tmp_path).exists()
    assert OLD_VERSION in init.read_text(encoding="utf-8")
    assert not prefs.exists()
    assert not (worktree / "specs").exists()


def test_missing_worktree_acc_is_offered_before_any_install(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path, version=None)
    result = preflight(
        _record(worktree),
        "do it",
        _spec_dir(tmp_path),
        consent=False,
        release_root=_release(tmp_path),
    )
    assert result["ok"] is False
    assert result["acc"]["action"] == "missing"
    assert not _install_log(tmp_path).exists()


def test_consented_update_runs_only_the_selected_installer_in_the_worktree(
    tmp_path, fake_bin, monkeypatch
):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    prefs = tmp_path / "global-preferences.toml"
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))
    worktree = _worktree(tmp_path, OLD_VERSION)
    release = _release(tmp_path)
    result = preflight(
        _record(worktree),
        "do it",
        _spec_dir(tmp_path),
        consent=True,
        release_root=release,
    )
    assert result["ok"] is True
    assert result["acc"]["action"] == "updated"
    assert result["acc"]["installed"] == RELEASE_VERSION
    logged = json.loads(_install_log(tmp_path).read_text(encoding="utf-8"))
    assert logged["argv"] == [str(release / "install.py"), str(worktree)]
    assert Path(logged["cwd"]).resolve() == worktree.resolve()
    assert not prefs.exists()


def test_installer_failure_is_a_bounded_diagnostic(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    release = _release(tmp_path)
    (release / "install.py").write_text(
        "import sys\nprint('installer exploded')\nraise SystemExit(3)\n",
        encoding="utf-8",
    )
    result = preflight(
        _record(_worktree(tmp_path, OLD_VERSION)),
        "do it",
        _spec_dir(tmp_path),
        consent=True,
        release_root=release,
    )
    assert result["ok"] is False
    assert "installer exploded" in result["error"]
    assert "\n" not in result["error"]


def test_release_without_a_readable_version_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    release = _release(tmp_path)
    (release / "skill/crew-chief/scripts/crew_chief/__init__.py").unlink()
    result = preflight(
        _record(_worktree(tmp_path)),
        "do it",
        _spec_dir(tmp_path),
        False,
        release,
    )
    assert result["ok"] is False
    assert "release" in result["error"]


# --- selected spec transport ------------------------------------------------


def test_spec_copy_carries_only_the_selected_files(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    spec = _spec_dir(tmp_path)
    (spec / "neighbor-secret.txt").write_text("sk-" + "a" * 30 + "\n", encoding="utf-8")
    result = preflight(_record(worktree), "do it", spec, False, _release(tmp_path))
    assert result["ok"] is True
    copied = Path(result["spec_dir"])
    assert copied.parent.parent == worktree
    assert sorted(p.name for p in copied.iterdir()) == [
        "crew.toml",
        "plan.md",
        "spec.md",
        "tasks.md",
    ]
    assert b"sk-" not in b"".join(
        p.read_bytes() for p in worktree.rglob("*") if p.is_file()
    )


def test_spec_copy_preserves_bom_and_crlf(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    spec = _spec_dir(tmp_path)
    (spec / "spec.md").write_bytes(b"\xef\xbb\xbf# spec\r\nline\r\n")
    result = preflight(
        _record(_worktree(tmp_path)), "do it", spec, False, _release(tmp_path)
    )
    assert result["ok"] is True
    assert (Path(result["spec_dir"]) / "spec.md").read_bytes() == (
        b"\xef\xbb\xbf# spec\r\nline\r\n"
    )


def test_spec_with_a_secret_is_refused_before_copying(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    spec = _spec_dir(tmp_path, **{"plan.md": 'key = "sk-' + "b" * 30 + '"\n'})
    result = preflight(_record(worktree), "do it", spec, False, _release(tmp_path))
    assert result["ok"] is False
    assert "secret" in result["error"].casefold() or "openai" in result["error"]
    assert not (worktree / "specs").exists()


def test_spec_with_a_private_path_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    spec = _spec_dir(tmp_path, **{"tasks.md": "read client/.env before running\n"})
    result = preflight(
        _record(_worktree(tmp_path)), "do it", spec, False, _release(tmp_path)
    )
    assert result["ok"] is False
    assert "sensitive" in result["error"]


def test_spec_symlink_escaping_the_selection_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    spec = _spec_dir(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    target = spec / "plan.md"
    target.unlink()
    try:
        os.symlink(outside, target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks")
    result = preflight(
        _record(_worktree(tmp_path)), "do it", spec, False, _release(tmp_path)
    )
    assert result["ok"] is False
    assert "symbolic" in result["error"] or "escape" in result["error"]


def test_request_with_a_secret_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    result = preflight(
        _record(_worktree(tmp_path)),
        "deploy with token ghp_" + "c" * 25,
        _spec_dir(tmp_path),
        False,
        _release(tmp_path),
    )
    assert result["ok"] is False
    assert "github-token" in result["error"]


def test_spec_copy_destination_collision_is_refused(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "claude.log")
    worktree = _worktree(tmp_path)
    spec = _spec_dir(tmp_path)
    destination = worktree / "specs" / spec.name
    destination.mkdir(parents=True)
    (destination / "spec.md").write_text("# existing\n", encoding="utf-8")
    result = preflight(_record(worktree), "do it", spec, False, _release(tmp_path))
    assert result["ok"] is False
    assert "exists" in result["error"]
    assert (destination / "spec.md").read_text(encoding="utf-8") == "# existing\n"


@contextmanager
def _directory_link(link, target):
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, shell=False)
        if result.returncode:
            pytest.skip("temporary junction unavailable")
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.parametrize("broken", [False, True])
def test_destination_link_cannot_write_outside_worktree(tmp_path, fake_bin, broken):
    _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path)
    spec = _spec_dir(tmp_path)
    (worktree / "specs").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with _directory_link(worktree / "specs" / spec.name, outside):
        if broken:
            outside.rmdir()
        result = preflight(_record(worktree), "synthetic", spec, False, _release(tmp_path))
        assert result["ok"] is False
        assert not outside.exists() if broken else list(outside.iterdir()) == []


def test_worktree_install_never_follows_external_skill_root(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path, version=None)
    outside = tmp_path / "outside"
    outside.mkdir()
    with _directory_link(worktree / ".claude", outside):
        result = preflight(_record(worktree, "raw-request", authorize_raw=True),
                           "synthetic", None, True, _release(tmp_path))
        assert result["ok"] is False
        assert list(outside.iterdir()) == []
        assert not _install_log(tmp_path).exists()


@pytest.mark.parametrize("private_in_spec", [False, True])
def test_configured_private_paths_are_enforced(tmp_path, fake_bin, private_in_spec):
    _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path)
    prefs = worktree / "docs/crew-chief/preferences.toml"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('sensitive_paths = ["private-material"]\n', encoding="utf-8")
    before = prefs.read_bytes()
    spec = _spec_dir(tmp_path)
    request = "read private-material/source" if not private_in_spec else "synthetic"
    if private_in_spec:
        (spec / "plan.md").write_text("read private-material/source", encoding="utf-8")
    result = preflight(_record(worktree), request, spec, False, _release(tmp_path))
    assert result["ok"] is False
    assert "sensitive" in result["error"]
    assert prefs.read_bytes() == before


def test_preflight_never_uses_unbounded_read_bytes(tmp_path, fake_bin, monkeypatch):
    _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree, spec, release = _worktree(tmp_path), _spec_dir(tmp_path), _release(tmp_path)
    def unbounded(*args, **kwargs):
        raise AssertionError("preflight must open and read at most cap + 1")
    monkeypatch.setattr(Path, "read_bytes", unbounded)
    result = preflight(_record(worktree), "synthetic", spec, False, release)
    assert result["ok"] is True


def test_explicit_update_repairs_disagreeing_installed_versions(tmp_path, fake_bin):
    _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path, OLD_VERSION)
    other = worktree / ".agents/skills/crew-chief/scripts/crew_chief/__init__.py"
    other.parent.mkdir(parents=True)
    other.write_text(f'__version__ = "{RELEASE_VERSION}"\n', encoding="utf-8")
    result = preflight(_record(worktree, "raw-request", authorize_raw=True),
                       "synthetic", None, True, _release(tmp_path))
    assert result["ok"] is True
    assert result["acc"]["action"] == "updated"

def test_selected_spec_dir_matching_configured_sensitive_path_is_refused(
    tmp_path, fake_bin
):
    log = _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path)
    prefs = worktree / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('sensitive_paths = ["external-spec"]' + chr(10), encoding="utf-8")
    before_prefs = prefs.read_bytes()
    spec = _spec_dir(tmp_path)
    before_spec = {p.name: p.read_bytes() for p in spec.iterdir()}
    result = preflight(_record(worktree), "synthetic", spec, False, _release(tmp_path))
    assert result["ok"] is False
    assert "sensitive" in result["error"]
    assert not (worktree / "specs").exists()
    assert _calls(log) == []
    assert prefs.read_bytes() == before_prefs
    for path in spec.iterdir():
        assert path.read_bytes() == before_spec[path.name]


def test_selected_spec_behind_a_junction_alias_is_refused(tmp_path, fake_bin):
    log = _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path)
    prefs = worktree / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text('sensitive_paths = ["external-spec"]' + chr(10), encoding="utf-8")
    spec = _spec_dir(tmp_path)
    alias = tmp_path / "selected-spec"
    with _directory_link(alias, spec):
        result = preflight(
            _record(worktree), "synthetic", alias, False, _release(tmp_path)
        )
    assert result["ok"] is False
    assert "sensitive" in result["error"] or "link" in result["error"].casefold()
    assert not (worktree / "specs").exists()
    assert _calls(log) == []


def test_selected_spec_resolving_into_a_sensitive_path_is_refused(tmp_path, fake_bin):
    log = _fake_claude(fake_bin, tmp_path / "cli.log")
    worktree = _worktree(tmp_path)
    prefs = worktree / "docs" / "crew-chief" / "preferences.toml"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(
        'sensitive_paths = ["outside/external-spec"]' + chr(10), encoding="utf-8"
    )
    outside = tmp_path / "outside"
    real = _spec_dir(outside)
    alias_parent = tmp_path / "alias"
    with _directory_link(alias_parent, outside):
        result = preflight(
            _record(worktree),
            "synthetic",
            alias_parent / real.name,
            False,
            _release(tmp_path),
        )
    assert result["ok"] is False
    assert "sensitive" in result["error"]
    assert not (worktree / "specs").exists()
    assert _calls(log) == []
