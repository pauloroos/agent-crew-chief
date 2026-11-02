"""Standard names for the crew's terminals (ACC 0.12.0)."""

from __future__ import annotations

import pathlib
import shlex

ROLES = ("engineer", "driver", "scrutineer")


def _clean(value: str, argument: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{argument}' must be a string")
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError(f"'{argument}' must not be empty")
    return cleaned


def terminal_name(slug: str, lane: str, role: str, attempt: int = 1) -> str:
    """`acc-<slug>-<lane>-<role>` for attempt 1, `...-<role>-<n>` from 2 on."""
    slug = _clean(slug, "slug")
    lane = _clean(lane, "lane")
    role = _clean(role, "role")
    if role not in ROLES:
        raise ValueError(f"'role' must be one of {', '.join(ROLES)}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("'attempt' must be an int >= 1")
    name = f"acc-{slug}-{lane}-{role}"
    return name if attempt == 1 else f"{name}-{attempt}"


# --- tmux mode (T03) -----------------------------------------------------------
#
# These helpers only build command strings. Running them is the caller's job.


def log_file(logs_dir, name: str) -> pathlib.Path:
    """`<logs_dir>/<name>.log`, the same log path used by Plain mode."""
    return pathlib.Path(logs_dir) / f"{name}.log"


def _quoted_path(path) -> str:
    text = pathlib.PurePath(path).as_posix()
    return f'"{text}"' if any(char.isspace() for char in text) else text


def tmux_new_session(name: str, command: str) -> str:
    """Create the named detached session running `command`."""
    return f"tmux new-session -d -s {shlex.quote(name)} {shlex.quote(command)}"


def tmux_pipe_pane(name: str, log_path) -> str:
    """Append the session's output to `log_path`."""
    inner = f"cat >> {_quoted_path(log_path)}"
    return f"tmux pipe-pane -o -t {shlex.quote(name)} {shlex.quote(inner)}"


def tmux_session_pid(name: str) -> str:
    """Print the session's PID, for the PID file."""
    return f"tmux display-message -p -t {shlex.quote(name)} '#{{session_pid}}'"


def tmux_attach(name: str) -> str:
    """Enter the named session."""
    return f"tmux attach -t {shlex.quote(name)}"


def tmux_kill_session(name: str) -> str:
    """End the session; replaces `kill -- -<pid>`."""
    return f"tmux kill-session -t {shlex.quote(name)}"


def tmux_start(name: str, log_path, command: str) -> list[str]:
    """The three commands that start a session and record its log and PID."""
    return [tmux_new_session(name, command), tmux_pipe_pane(name, log_path), tmux_session_pid(name)]
