"""Portable ``acc`` command wrappers for one explicit released bundle.

The module writes a single small wrapper into a *user* bin directory so
that ``acc`` runs a released copy of agent-crew-chief:

* Windows gets ``acc.cmd``, a batch file with CRLF line endings;
* Linux and macOS get ``acc``, an executable ``/bin/sh`` script.

Both wrappers call the explicit interpreter with ``-I`` (isolated mode) and
the explicit entry script of the bundle, so the command never depends on
``PYTHONPATH``, on a user site directory, on the current working directory
or on a virtual environment that happens to be active. Windows needs no
pty, curses, tmux or ConPTY: the batch file starts the interpreter on the
terminal the user already has, and the interactive session is the one the
interrupted entry point persisted.

Every path is quoted, so a bundle or an interpreter living in a directory
with spaces or non-ASCII characters still works. A Windows wrapper whose
paths are not plain ASCII switches the console to UTF-8 first (``chcp``),
because ``cmd.exe`` reads a batch file in the console code page; that is
the only setting the wrappers touch, and it is visible in the console.

Nothing here changes global preferences, ``PATH`` or any administrator
setting. The module writes exactly one file inside the bin directory and
reports where that directory is, so the owner decides whether (and how) to
put it on ``PATH``. The documented defaults are ``~/.local/bin`` on Linux
and macOS and ``%LOCALAPPDATA%/agent-crew-chief/bin`` on Windows.

Only the standard library is used, and the wrappers are plain text files:
no compilation, no package manager and no third-party runtime.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from .records import write_atomic

#: Marker that identifies a wrapper written by this module.
OWNER_MARKER = "agent-crew-chief:acc-wrapper:v1"

#: The command the wrapper provides.
COMMAND_NAME = "acc"

#: The entry script of a released bundle.
ENTRY_NAME = "crew-chief.py"

#: Where the entry script lives inside a release root.
BUNDLE_ENTRY = Path("skill") / "crew-chief" / "scripts" / ENTRY_NAME

#: Where the entry script lives when the source *is* the skill directory.
SKILL_ENTRY = Path("scripts") / ENTRY_NAME

#: Wrapper file name per platform family.
WINDOWS_WRAPPER = "acc.cmd"
POSIX_WRAPPER = "acc"

#: Platform spellings this module accepts, mapped to one of the families.
_PLATFORM_FAMILIES = {
    "windows": "windows",
    "win32": "windows",
    "win": "windows",
    "nt": "windows",
    "cygwin": "windows",
    "msys": "windows",
    "linux": "linux",
    "linux2": "linux",
    "macos": "macos",
    "macosx": "macos",
    "darwin": "macos",
    "osx": "macos",
}


class CommandInstallError(ValueError):
    """Raised when a command wrapper cannot be installed.

    It is a :class:`ValueError`, so the CLI reports it as a one-line error
    and exits with its usual code.
    """


# --- platform ---------------------------------------------------------------


def normalize_platform(platform: str) -> str:
    """``"windows"``, ``"linux"`` or ``"macos"`` for ``platform``.

    The usual spellings (``sys.platform`` values and the family names) are
    accepted; anything else is refused instead of guessed.
    """
    key = str(platform).strip().lower()
    try:
        return _PLATFORM_FAMILIES[key]
    except KeyError:
        raise CommandInstallError(
            f"{platform}: unknown platform (expected windows, linux or macos)"
        ) from None


def current_platform() -> str:
    """The platform family of the machine running this code."""
    return normalize_platform(sys.platform)


def wrapper_path(bin_dir: Path, platform: str) -> Path:
    """Where the wrapper of ``platform`` lives inside ``bin_dir``."""
    name = (
        WINDOWS_WRAPPER
        if normalize_platform(platform) == "windows"
        else POSIX_WRAPPER
    )
    return Path(bin_dir) / name


def default_bin_dir(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The user bin directory where the ``acc`` wrapper goes.

    Windows uses ``%LOCALAPPDATA%/agent-crew-chief/bin`` (or the same path
    under the home when the variable is missing); Linux and macOS use
    ``~/.local/bin``. The directory is a suggestion: nothing here changes
    ``PATH`` and the directory is not created.
    """
    family = normalize_platform(platform if platform else current_platform())
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if family == "windows":
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "agent-crew-chief" / "bin"
    return home / ".local" / "bin"


def path_hint(bin_dir: Path) -> str:
    """The line telling the owner to put ``bin_dir`` on ``PATH`` themselves."""
    return (
        f"add {bin_dir} to your PATH to use `{COMMAND_NAME}` "
        "(nothing was changed for you)"
    )


# --- the source bundle ------------------------------------------------------


def entry_script(source: Path) -> Path:
    """The entry script inside the released bundle ``source``.

    ``source`` is either a release root (holding ``skill/crew-chief``) or
    the ``skill/crew-chief`` directory itself.
    """
    source = Path(source)
    for relative in (BUNDLE_ENTRY, SKILL_ENTRY):
        candidate = source / relative
        if candidate.is_file():
            return candidate
    raise CommandInstallError(
        f"{source}: no {ENTRY_NAME} found "
        f"(expected {BUNDLE_ENTRY.as_posix()})"
    )


def is_owned_wrapper(path: Path) -> bool:
    """Whether ``path`` is a wrapper this module wrote."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return OWNER_MARKER in text


# --- rendering --------------------------------------------------------------


def _sh_quote(text: str) -> str:
    """``text`` as one single-quoted POSIX shell word."""
    return "'" + text.replace("'", "'\\''") + "'"


def _cmd_quote(text: str) -> str:
    """``text`` as one double-quoted Windows command word."""
    return '"' + text.replace("%", "%%") + '"'


def _has_non_ascii(text: str) -> bool:
    """Whether ``text`` holds a character outside ASCII."""
    return any(ord(char) > 127 for char in text)


def _render_sh(entry: Path, python: Path) -> str:
    """The POSIX wrapper: a ``/bin/sh`` script that execs the interpreter."""
    command = f"exec {_sh_quote(str(python))} -I {_sh_quote(str(entry))} \"$@\""
    lines = [
        "#!/bin/sh",
        f"# {OWNER_MARKER}",
        "# Written by agent-crew-chief; edit the release, not this file.",
        command,
    ]
    return "\n".join(lines) + "\n"


def _render_cmd(entry: Path, python: Path) -> str:
    """The Windows wrapper: a batch file that runs the interpreter."""
    command = f"{_cmd_quote(str(python))} -I {_cmd_quote(str(entry))} %*"
    lines = ["@echo off"]
    if _has_non_ascii(command):
        # cmd.exe reads a batch file in the console code page; switch to
        # UTF-8 first so a path with non-ASCII characters survives.
        lines.append("chcp 65001 >nul")
    lines += [
        f"REM {OWNER_MARKER}",
        "REM Written by agent-crew-chief; edit the release, not this file.",
        command,
        "exit /b %ERRORLEVEL%",
    ]
    return "\r\n".join(lines) + "\r\n"


def render_wrapper(entry: Path, python: Path, platform: str) -> str:
    """The wrapper text running ``entry`` with ``python`` on ``platform``."""
    family = normalize_platform(platform)
    if family == "windows":
        return _render_cmd(Path(entry), Path(python))
    return _render_sh(Path(entry), Path(python))


# --- installing -------------------------------------------------------------


def _make_executable(target: Path, family: str) -> None:
    """Give the POSIX wrapper the executable bit (a no-op on Windows)."""
    if family == "windows":
        return
    mode = target.stat().st_mode
    target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_command(
    source: Path,
    bin_dir: Path,
    python: Path,
    platform: str,
    replace_owned: bool = False,
) -> Path:
    """Install (or update) the ``acc`` wrapper of the bundle in ``source``.

    ``source`` is an explicit released bundle, ``bin_dir`` the user bin
    directory that receives the wrapper and ``python`` the interpreter that
    runs it. The wrapper starts that interpreter with ``-I`` and the
    bundle's entry script, so the command is reproducible everywhere.

    An existing wrapper this module did not write is never replaced: the
    call fails and the file stays as it is. An existing ACC wrapper is
    replaced only when ``replace_owned`` is true; installing the same
    bundle again is a no-op.

    Returns the path of the wrapper. Nothing outside ``bin_dir`` is
    written: no preferences file, no ``PATH``, no administrator setting.
    """
    family = normalize_platform(platform)
    entry = entry_script(source)
    interpreter = Path(python)
    if not interpreter.is_file():
        raise CommandInstallError(f"{interpreter}: no such interpreter")

    # Bind both paths to their install-time location so the wrapper keeps
    # working no matter what the caller's current directory is later. The
    # interpreter keeps the name the caller gave (absolute, links not
    # followed): a venv or a python3 -> python3.x link must survive upgrades.
    entry = Path(entry).resolve()
    interpreter = interpreter.absolute()

    bin_dir = Path(bin_dir)
    target = wrapper_path(bin_dir, family)
    content = render_wrapper(entry, interpreter, family)

    if target.exists():
        if not is_owned_wrapper(target):
            raise CommandInstallError(
                f"{target}: not an agent-crew-chief wrapper; "
                "refusing to replace it"
            )
        if target.read_bytes() == content.encode("utf-8"):
            _make_executable(target, family)
            return target
        if not replace_owned:
            raise CommandInstallError(
                f"{target}: an agent-crew-chief wrapper is already installed; "
                "pass replace_owned=True to update it"
            )

    write_atomic(target, content)
    _make_executable(target, family)
    return target
