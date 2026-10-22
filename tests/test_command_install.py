"""Tests for ``crew_chief.command_install``: the portable ``acc`` wrappers.

Every test works in temporary directories only: a fake released bundle, a
fake user bin directory and a throwaway home. The real ``~/.config``, the
real ``PATH`` and any real CLI are never touched: the global preferences
file is written once with known bytes and read back afterwards to prove the
installer left it alone.

The three OS families are covered by content (``windows`` writes ``acc.cmd``,
``linux`` and ``macos`` write an executable ``acc``); the wrapper is
*executed* only for the family of the machine running the tests.
"""

from __future__ import annotations

import ast
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from crew_chief.command_install import (
    BUNDLE_ENTRY,
    OWNER_MARKER,
    CommandInstallError,
    default_bin_dir,
    install_command,
    is_owned_wrapper,
    normalize_platform,
    wrapper_path,
)

#: The legacy entry script of this repository (the one a wrapper calls).
LEGACY_SKILL = Path(__file__).resolve().parents[1] / "skill" / "crew-chief"
LEGACY_ENTRY = LEGACY_SKILL / "scripts" / "crew-chief.py"

#: A fake entry point: prints its arguments, exits with $FAKE_ENTRY_EXIT.
FAKE_ENTRY = """import os
import sys

print("fake-entry:" + "|".join(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_ENTRY_EXIT", "0")))
"""


# --- helpers ----------------------------------------------------------------


def _native_platform() -> str:
    """The platform family of the machine running the tests."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _release(tmp_path: Path, name: str = "release") -> Path:
    """A fake released bundle holding the legacy entry script."""
    root = tmp_path / name
    script = root / BUNDLE_ENTRY
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(FAKE_ENTRY, encoding="utf-8")
    return root


def _entry_of(release: Path) -> Path:
    """The entry script inside a fake release."""
    return release / BUNDLE_ENTRY


def _relative_to_anchor(interpreter: str, anchor: Path) -> Path | None:
    """A relative path to ``interpreter`` from ``anchor``, or ``None``.

    The relative-interpreter scenario only applies where the native platform
    permits one: on Windows ``os.path.relpath`` cannot bridge different
    drives and raises (``path is on mount ..``, CI may place the interpreter
    on ``C:`` while ``tmp_path`` is on ``D:``). ``None`` means the case is
    impossible on this machine, not a bug.
    """
    try:
        return Path(os.path.relpath(interpreter, anchor))
    except ValueError:
        return None


def _run_wrapper(target: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a wrapper the way a user would, and capture its output."""
    return subprocess.run(
        [str(target), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# --- the three OS families --------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "name"),
    [("windows", "acc.cmd"), ("linux", "acc"), ("macos", "acc")],
)
def test_wrapper_name_per_family(tmp_path, platform, name):
    release = _release(tmp_path)

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), platform
    )

    assert target.name == name
    assert target.parent == tmp_path / "bin"
    assert wrapper_path(tmp_path / "bin", platform) == target


def test_wrapper_keeps_a_linked_interpreter_unresolved(tmp_path):
    release = _release(tmp_path)
    link = tmp_path / "python-link"
    try:
        link.symlink_to(sys.executable)
    except OSError:
        pytest.skip("symlinks are not available here")

    target = install_command(release, tmp_path / "bin", link, "linux")

    assert f"'{link}'" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("platform", ["windows", "linux", "macos"])
def test_wrapper_runs_the_explicit_interpreter_in_isolated_mode(
    tmp_path, platform
):
    release = _release(tmp_path)
    python = Path(sys.executable)

    target = install_command(release, tmp_path / "bin", python, platform)

    text = target.read_text(encoding="utf-8")
    assert " -I " in text
    assert text.index(" -I ") < text.index(str(_entry_of(release)))
    assert str(python) in text


def test_windows_wrapper_is_a_crlf_batch_file(tmp_path):
    release = _release(tmp_path)

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), "windows"
    )

    raw = target.read_bytes()
    assert raw.startswith(b"@echo off\r\n")
    assert b"\n\n" not in raw
    assert OWNER_MARKER.encode("utf-8") in raw
    assert f'"{sys.executable}"'.encode("utf-8") in raw
    assert b"%*" in raw


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_posix_wrapper_is_an_executable_sh_script(tmp_path, platform):
    release = _release(tmp_path)

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), platform
    )

    text = target.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\r\n" not in text
    assert OWNER_MARKER in text
    assert f"'{sys.executable}'" in text
    assert f"'{_entry_of(release)}'" in text
    assert '"$@"' in text
    if os.name != "nt":
        # Windows filesystems have no executable bit; POSIX ones do.
        mode = target.stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH


def test_unknown_platform_is_refused(tmp_path):
    release = _release(tmp_path)

    with pytest.raises(CommandInstallError):
        install_command(
            release, tmp_path / "bin", Path(sys.executable), "plan9"
        )

    assert not (tmp_path / "bin").exists()


def test_platform_aliases_are_accepted():
    assert normalize_platform("win32") == "windows"
    assert normalize_platform("Windows") == "windows"
    assert normalize_platform("linux") == "linux"
    assert normalize_platform("darwin") == "macos"


# --- quoting ----------------------------------------------------------------


def test_posix_wrapper_quotes_spaces_and_unicode(tmp_path):
    release = _release(tmp_path, "pacote ação")
    bin_dir = tmp_path / "binários do usuário"

    target = install_command(
        release, bin_dir, Path(sys.executable), "linux"
    )

    text = target.read_text(encoding="utf-8")
    assert f"'{_entry_of(release)}'" in text
    assert f"'{sys.executable}'" in text
    assert "ação" in text
    # Nothing is left unquoted: every path sits inside single quotes.
    command = text.splitlines()[-1]
    assert command.startswith("exec '")
    assert command.endswith('"$@"')


def test_posix_wrapper_escapes_single_quotes(tmp_path):
    release = _release(tmp_path, "release'with'quotes")

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), "linux"
    )

    text = target.read_text(encoding="utf-8")
    assert "'\\''" in text


def test_windows_wrapper_quotes_spaces_and_unicode(tmp_path):
    release = _release(tmp_path, "pacote ação")

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), "windows"
    )

    text = target.read_text(encoding="utf-8")
    assert f'"{_entry_of(release)}"' in text
    assert f'"{sys.executable}"' in text
    assert "ação" in text


def test_windows_wrapper_escapes_percent_in_paths(tmp_path):
    release = _release(tmp_path, "100%_release")

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), "windows"
    )

    text = target.read_text(encoding="utf-8")
    assert "100%%_release" in text


# --- installing, updating, refusing ----------------------------------------


def test_install_creates_the_bin_directory(tmp_path):
    release = _release(tmp_path)
    bin_dir = tmp_path / "new" / "bin"

    target = install_command(
        release, bin_dir, Path(sys.executable), "linux"
    )

    assert target.is_file()
    assert sorted(p.name for p in bin_dir.iterdir()) == [target.name]


def test_installing_twice_changes_nothing(tmp_path):
    release = _release(tmp_path)
    bin_dir = tmp_path / "bin"

    first = install_command(
        release, bin_dir, Path(sys.executable), "linux"
    )
    before = first.read_bytes()

    second = install_command(
        release, bin_dir, Path(sys.executable), "linux"
    )

    assert second == first
    assert second.read_bytes() == before


def test_updating_an_owned_wrapper_needs_the_explicit_flag(tmp_path):
    first_release = _release(tmp_path, "release-a")
    second_release = _release(tmp_path, "release-b")
    bin_dir = tmp_path / "bin"

    target = install_command(
        first_release, bin_dir, Path(sys.executable), "linux"
    )
    before = target.read_bytes()

    with pytest.raises(CommandInstallError):
        install_command(
            second_release, bin_dir, Path(sys.executable), "linux"
        )
    assert target.read_bytes() == before

    updated = install_command(
        second_release, bin_dir, Path(sys.executable), "linux",
        replace_owned=True,
    )
    assert updated == target
    assert updated.read_bytes() != before
    assert str(_entry_of(second_release)) in updated.read_text(
        encoding="utf-8"
    )


def test_updating_twice_is_idempotent(tmp_path):
    release = _release(tmp_path)
    bin_dir = tmp_path / "bin"

    install_command(
        release, bin_dir, Path(sys.executable), "linux", replace_owned=True
    )
    first = (bin_dir / "acc").read_bytes()
    install_command(
        release, bin_dir, Path(sys.executable), "linux", replace_owned=True
    )

    assert (bin_dir / "acc").read_bytes() == first


def test_an_unrelated_wrapper_is_never_replaced(tmp_path):
    release = _release(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mine = bin_dir / "acc"
    mine.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    with pytest.raises(CommandInstallError):
        install_command(
            release, bin_dir, Path(sys.executable), "linux"
        )
    assert mine.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"

    with pytest.raises(CommandInstallError):
        install_command(
            release, bin_dir, Path(sys.executable), "linux",
            replace_owned=True,
        )
    assert mine.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
    assert not is_owned_wrapper(mine)


def test_an_owned_wrapper_is_recognized(tmp_path):
    release = _release(tmp_path)

    target = install_command(
        release, tmp_path / "bin", Path(sys.executable), "linux"
    )

    assert is_owned_wrapper(target)


def test_source_without_an_entry_script_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(CommandInstallError):
        install_command(
            empty, tmp_path / "bin", Path(sys.executable), "linux"
        )


def test_the_skill_directory_itself_is_a_valid_source(tmp_path):
    release = _release(tmp_path)
    skill = release / "skill" / "crew-chief"

    target = install_command(
        skill, tmp_path / "bin", Path(sys.executable), "linux"
    )

    assert str(skill / "scripts" / "crew-chief.py") in target.read_text(
        encoding="utf-8"
    )


def test_a_missing_interpreter_is_refused(tmp_path):
    release = _release(tmp_path)

    with pytest.raises(CommandInstallError):
        install_command(
            release, tmp_path / "bin", tmp_path / "no-such-python", "linux"
        )


# --- nothing else is touched ------------------------------------------------


def test_install_never_touches_preferences_path_or_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    prefs = tmp_path / "preferences.toml"
    prefs.write_text('review_default = "x:y"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_CREW_CHIEF_GLOBAL_PREFS", str(prefs))

    before_prefs = prefs.read_bytes()
    before_path = os.environ.get("PATH")

    release = _release(tmp_path)
    bin_dir = tmp_path / "bin"
    target = install_command(
        release, bin_dir, Path(sys.executable), _native_platform()
    )

    assert prefs.read_bytes() == before_prefs
    assert os.environ.get("PATH") == before_path
    assert list(home.iterdir()) == []
    assert sorted(p.name for p in bin_dir.iterdir()) == [target.name]


def test_the_legacy_entry_script_is_untouched_and_still_runs(tmp_path):
    release = _release(tmp_path)
    entry = _entry_of(release)
    before = entry.read_bytes()

    install_command(
        release, tmp_path / "bin", Path(sys.executable), "linux"
    )

    assert entry.read_bytes() == before
    assert entry.is_file()


def test_the_repository_legacy_entry_still_runs_after_install(tmp_path):
    release = tmp_path / "repo-release"
    shutil.copytree(
        LEGACY_SKILL, release / "skill" / "crew-chief",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    script = release / BUNDLE_ENTRY
    assert script.read_bytes() == LEGACY_ENTRY.read_bytes()

    install_command(
        release, tmp_path / "bin", Path(sys.executable), "linux"
    )

    result = subprocess.run(
        [sys.executable, "-I", str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "crew-chief" in result.stdout


# --- the wrapper runs -------------------------------------------------------


def test_the_native_wrapper_runs_the_fake_entry_point(tmp_path, monkeypatch):
    release = _release(tmp_path, "fake release")
    bin_dir = tmp_path / "bin"

    target = install_command(
        release, bin_dir, Path(sys.executable), _native_platform()
    )
    monkeypatch.setenv("FAKE_ENTRY_EXIT", "3")

    result = _run_wrapper(target, "one", "two words")

    assert result.returncode == 3, result.stderr
    assert "fake-entry:one|two words" in result.stdout


@pytest.mark.skipif(
    sys.platform.startswith("win"), reason="the POSIX wrapper needs a POSIX shell"
)
def test_the_posix_wrapper_runs_with_unicode_paths(tmp_path):
    release = _release(tmp_path, "pacote ação")
    bin_dir = tmp_path / "binários"

    target = install_command(
        release, bin_dir, Path(sys.executable), "linux"
    )

    result = _run_wrapper(target, "ação")

    assert result.returncode == 0, result.stderr
    assert "fake-entry:ação" in result.stdout


def test_the_native_wrapper_runs_from_another_working_directory(tmp_path, monkeypatch):
    """A relative source must not leak into the wrapper's runtime cwd."""
    release = _release(tmp_path, "fake release")
    bin_dir = tmp_path / "binários do usuário"
    elsewhere = tmp_path / "elsewhere dir"
    elsewhere.mkdir()

    monkeypatch.chdir(tmp_path)
    target = install_command(
        Path("fake release"), bin_dir, Path(sys.executable), _native_platform()
    )
    monkeypatch.setenv("FAKE_ENTRY_EXIT", "0")

    result = subprocess.run(
        [str(target), "one", "two words"],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "fake-entry:one|two words" in result.stdout


def test_the_native_wrapper_runs_with_a_relative_interpreter(tmp_path, monkeypatch):
    """A relative interpreter must also be bound to its install-time path."""
    _release(tmp_path, "release")
    bin_dir = tmp_path / "bin"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    relative_python = _relative_to_anchor(sys.executable, tmp_path)
    if relative_python is None:
        pytest.skip(
            "the interpreter and tmp_path sit on different Windows drives; "
            "a relative interpreter is not expressible here"
        )

    monkeypatch.chdir(tmp_path)
    target = install_command(
        Path("release"), bin_dir, relative_python, _native_platform()
    )
    monkeypatch.setenv("FAKE_ENTRY_EXIT", "7")

    result = subprocess.run(
        [str(target), "rel"],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 7, result.stderr
    assert "fake-entry:rel" in result.stdout


# --- the portable relative-interpreter helper -------------------------------


def test_a_relative_interpreter_is_impossible_across_windows_drives():
    """Regression: the different-drive case must be detected, not fatal.

    On Windows CI the interpreter can live on ``C:`` while the test's
    temporary directory lives on ``D:``; ``os.path.relpath`` raises there.
    The helper turns that into ``None``; on a single-mount POSIX platform
    no such case exists, so the same call simply yields a path.
    """
    result = _relative_to_anchor(r"C:\python\python.exe", Path(r"D:\tmp\run"))

    if os.name == "nt":
        assert result is None
    else:
        assert result is not None
        assert not result.is_absolute()


def test_a_relative_interpreter_still_works_on_the_same_drive(tmp_path):
    """The helper keeps producing plain relative paths in the normal case."""
    inside = tmp_path / "venv" / "python"

    result = _relative_to_anchor(str(inside), tmp_path)

    assert result == Path("venv") / "python"


# --- the documented user bin directory --------------------------------------


def test_default_bin_dir_per_family(tmp_path):
    home = tmp_path / "home"
    env = {"LOCALAPPDATA": str(tmp_path / "localappdata")}

    assert default_bin_dir("windows", env=env, home=home) == (
        tmp_path / "localappdata" / "agent-crew-chief" / "bin"
    )
    assert default_bin_dir("linux", env=env, home=home) == home / ".local" / "bin"
    assert default_bin_dir("macos", env=env, home=home) == home / ".local" / "bin"


def test_default_bin_dir_without_localappdata(tmp_path):
    home = tmp_path / "home"

    assert default_bin_dir("windows", env={}, home=home) == (
        home / "AppData" / "Local" / "agent-crew-chief" / "bin"
    )


# --- runtime dependencies ---------------------------------------------------


def test_the_module_needs_only_the_standard_library():
    source = Path(
        __import__("crew_chief.command_install", fromlist=["__file__"]).__file__
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert imported
    assert imported <= set(sys.stdlib_module_names)


def test_the_module_needs_no_pty_curses_or_conpty():
    source = Path(
        __import__("crew_chief.command_install", fromlist=["__file__"]).__file__
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"pty", "curses", "termios", "tty", "asyncio"}
    # Writing a wrapper is pure text: no process is ever spawned.
    assert "subprocess" not in imported
