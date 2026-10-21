"""Tests for ``install.py --command``: the command-only ``acc`` installation.

Every test builds a temporary *released bundle* (this repository's
``install.py`` plus ``skill/crew-chief``) and installs into a temporary bin
directory, with ``HOME``, ``USERPROFILE``, ``LOCALAPPDATA`` and
``AGENT_CREW_CHIEF_GLOBAL_PREFS`` pointing at temporary paths. The real
``~/.config``, the real ``PATH`` and any model CLI are never touched: a
sentinel preferences file is written once with known bytes and read back to
prove the installer left it alone.

The three OS families are covered by content (``windows`` writes ``acc.cmd``,
``linux`` and ``macos`` write an executable ``acc``); the wrapper is executed
only for the family of the machine running the tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install.py"
LEGACY_SKILL = INSTALL.parent / "skill" / "crew-chief"
LEGACY_ENTRY = LEGACY_SKILL / "scripts" / "crew-chief.py"

#: A fake entry point: prints its arguments, exits with $FAKE_ENTRY_EXIT.
FAKE_ENTRY = """import os
import sys

print("fake-entry:" + "|".join(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_ENTRY_EXIT", "0")))
"""

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


# --- helpers ----------------------------------------------------------------


def _native_platform() -> str:
    """The platform family of the machine running the tests."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _native_wrapper() -> str:
    """The wrapper file name of the machine running the tests."""
    return "acc.cmd" if _native_platform() == "windows" else "acc"


def _release(tmp_path: Path, name: str = "release") -> Path:
    """A released bundle: ``install.py`` next to the real ``skill/crew-chief``."""
    root = tmp_path / name
    root.mkdir(parents=True)
    shutil.copy2(INSTALL, root / "install.py")
    shutil.copytree(
        LEGACY_SKILL, root / "skill" / "crew-chief", ignore=IGNORE
    )
    return root


def _fake_release(tmp_path: Path, name: str = "release") -> Path:
    """A released bundle whose entry point only prints its arguments."""
    root = _release(tmp_path, name)
    (root / "skill" / "crew-chief" / "scripts" / "crew-chief.py").write_text(
        FAKE_ENTRY, encoding="utf-8"
    )
    return root


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A throwaway environment: temp home, temp bin root, temp preferences."""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["USERPROFILE"] = str(tmp_path / "home")
    env["LOCALAPPDATA"] = str(tmp_path / "localappdata")
    env["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = str(tmp_path / "prefs.toml")
    env.update(extra)
    return env


def _run(
    release: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the released ``install.py`` exactly as a user would."""
    return subprocess.run(
        [sys.executable, "-I", str(release / "install.py"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _entry_of(release: Path) -> Path:
    """The entry script inside a released bundle."""
    return release / "skill" / "crew-chief" / "scripts" / "crew-chief.py"


def _installer_module():
    """This repository's ``install.py`` as a module, for platform content checks."""
    if str(INSTALL.parent) not in sys.path:
        sys.path.insert(0, str(INSTALL.parent))
    import install

    return install


# --- command-only installation ---------------------------------------------


def test_command_only_installs_a_wrapper(tmp_path):
    release = _fake_release(tmp_path)
    bin_dir = tmp_path / "bin"

    result = _run(
        release, "--command", "--bin-dir", str(bin_dir), env=_env(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    target = bin_dir / _native_wrapper()
    assert target.is_file()
    assert sorted(p.name for p in bin_dir.iterdir()) == [target.name]


def test_command_only_never_creates_project_files(tmp_path):
    release = _fake_release(tmp_path)
    project = tmp_path / "elsewhere"
    project.mkdir()

    result = _run(
        release,
        "--command",
        "--bin-dir",
        str(tmp_path / "bin"),
        cwd=project,
        env=_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert list(project.iterdir()) == []
    assert not (release / "AGENTS.md").exists()
    assert not (release / ".claude").exists()
    assert not (release / ".agents").exists()
    assert not (release / "docs").exists()


def test_command_only_leaves_preferences_and_path_alone(tmp_path):
    release = _fake_release(tmp_path)
    env = _env(tmp_path)
    prefs = Path(env["AGENT_CREW_CHIEF_GLOBAL_PREFS"])
    prefs.write_bytes(b'# sentinel: review_default = "x:y"\r\n')
    before_prefs = prefs.read_bytes()
    before_path = env["PATH"]

    result = _run(
        release, "--command", "--bin-dir", str(tmp_path / "bin"), env=env
    )

    assert result.returncode == 0, result.stderr
    assert prefs.read_bytes() == before_prefs
    assert env["PATH"] == before_path
    assert "nothing was changed for you" in result.stdout
    assert not Path(env["HOME"]).exists() or list(
        Path(env["HOME"]).iterdir()
    ) == []


def test_command_only_defaults_to_the_user_bin_dir(tmp_path):
    release = _fake_release(tmp_path)
    env = _env(tmp_path)
    if _native_platform() == "windows":
        expected = (
            Path(env["LOCALAPPDATA"]) / "agent-crew-chief" / "bin"
        ) / "acc.cmd"
    else:
        expected = Path(env["HOME"]) / ".local" / "bin" / "acc"

    result = _run(release, "--command", env=env)

    assert result.returncode == 0, result.stderr
    assert expected.is_file()
    assert "PATH" in result.stdout


def test_command_only_installs_with_spaces_and_unicode_paths(tmp_path):
    release = _fake_release(tmp_path, "pacote ação")
    bin_dir = tmp_path / "binários do usuário"

    result = _run(
        release, "--command", "--bin-dir", str(bin_dir), env=_env(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    target = bin_dir / _native_wrapper()
    text = target.read_text(encoding="utf-8")
    entry = str(_entry_of(release))
    assert entry in text
    if _native_platform() == "windows":
        assert f'"{entry}"' in text
        assert "chcp 65001" in text
    else:
        assert f"'{entry}'" in text


# --- the three OS families --------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "name"), [("windows", "acc.cmd"), ("linux", "acc"), ("macos", "acc")]
)
def test_command_only_wrapper_name_per_family(tmp_path, monkeypatch, platform, name):
    install = _installer_module()
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(sys, "platform", platform)

    code = install.main(["--command", "--bin-dir", str(bin_dir)])

    assert code == 0
    target = bin_dir / name
    assert target.is_file()
    raw = target.read_bytes()
    assert str(_entry_of(INSTALL.parent)).encode("utf-8") in raw
    assert b" -I " in raw


def test_the_native_wrapper_runs_the_release(tmp_path, monkeypatch):
    release = _fake_release(tmp_path, "fake release")
    bin_dir = tmp_path / "bin"

    result = _run(
        release, "--command", "--bin-dir", str(bin_dir), env=_env(tmp_path)
    )
    assert result.returncode == 0, result.stderr

    monkeypatch.setenv("FAKE_ENTRY_EXIT", "3")
    run = subprocess.run(
        [str(bin_dir / _native_wrapper()), "one", "two words"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert run.returncode == 3, run.stderr
    assert "fake-entry:one|two words" in run.stdout


# --- updating ---------------------------------------------------------------


def test_command_only_updates_an_owned_wrapper(tmp_path):
    first = _fake_release(tmp_path, "release-a")
    second = _fake_release(tmp_path, "release-b")
    bin_dir = tmp_path / "bin"
    env = _env(tmp_path)

    assert _run(first, "--command", "--bin-dir", str(bin_dir), env=env).returncode == 0
    target = bin_dir / _native_wrapper()
    before = target.read_bytes()

    assert _run(second, "--command", "--bin-dir", str(bin_dir), env=env).returncode == 0
    after = target.read_bytes()

    assert after != before
    assert str(_entry_of(second)) in after.decode("utf-8")


def test_installing_the_same_bundle_twice_changes_nothing(tmp_path):
    release = _fake_release(tmp_path)
    bin_dir = tmp_path / "bin"
    env = _env(tmp_path)

    first = _run(release, "--command", "--bin-dir", str(bin_dir), env=env)
    target = bin_dir / _native_wrapper()
    before = target.read_bytes()

    second = _run(release, "--command", "--bin-dir", str(bin_dir), env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert target.read_bytes() == before


# --- the legacy project installation and entry point -----------------------


def test_project_installation_still_works_without_the_command(tmp_path):
    release = _release(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    result = _run(release, str(project), env=_env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert (project / ".claude" / "skills" / "crew-chief" / "SKILL.md").is_file()
    assert (project / ".agents" / "skills" / "crew-chief" / "SKILL.md").is_file()
    assert (project / "AGENTS.md").is_file()
    assert not (tmp_path / "localappdata").exists()


def test_project_and_command_together_do_both(tmp_path):
    release = _release(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"

    result = _run(
        release,
        str(project),
        "--command",
        "--bin-dir",
        str(bin_dir),
        env=_env(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert (project / "AGENTS.md").is_file()
    assert (bin_dir / _native_wrapper()).is_file()


def test_the_legacy_entry_script_is_untouched_and_still_runs(tmp_path):
    release = _release(tmp_path)
    entry = _entry_of(release)
    assert entry.read_bytes() == LEGACY_ENTRY.read_bytes()

    assert _run(
        release, "--command", "--bin-dir", str(tmp_path / "bin"), env=_env(tmp_path)
    ).returncode == 0

    assert entry.read_bytes() == LEGACY_ENTRY.read_bytes()

    result = subprocess.run(
        [sys.executable, "-I", str(entry), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "crew-chief" in result.stdout


# --- errors -----------------------------------------------------------------


def test_an_unrelated_wrapper_is_refused(tmp_path):
    release = _fake_release(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    foreign = bin_dir / _native_wrapper()
    foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    result = _run(
        release, "--command", "--bin-dir", str(bin_dir), env=_env(tmp_path)
    )

    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"


def test_a_bundle_without_the_skill_source_is_refused(tmp_path):
    release = tmp_path / "broken"
    release.mkdir()
    shutil.copy2(INSTALL, release / "install.py")
    bin_dir = tmp_path / "bin"

    result = _run(
        release, "--command", "--bin-dir", str(bin_dir), env=_env(tmp_path)
    )

    assert result.returncode == 2
    assert len(result.stderr.strip().splitlines()) == 1
    assert not bin_dir.exists()


def test_neither_a_project_nor_the_command_is_a_usage_error(tmp_path):
    release = _fake_release(tmp_path)

    result = _run(release, env=_env(tmp_path))

    assert result.returncode == 2
    assert "--command" in result.stderr
