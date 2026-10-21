"""Codex coordinator adapter: probe, headless argv and interactive resume (T009).

The adapter never chooses the CLI; ``central_config`` resolved ``codex``
already. It only turns a coordinator record into argv and normalizes the CLI's
JSONL events. It never spawns, runs or communicates with a process.

Restricted mode uses explicit no-prompt sandbox support instead of relying on
timeouts or the CLI's default user configuration. Interactive entry returns the
argv for the caller to run attached to the real terminal: no pty, tmux or
ConPTY is involved.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from typing import Any, Iterator

from .scoring import quota_family
from .tools import QUOTA_PATTERNS

__all__ = [
    "BYPASS_FLAG",
    "CLI",
    "CodexError",
    "SessionMismatchError",
    "headless",
    "interactive",
    "parse_events",
    "probe",
]

#: The coordinator CLI this adapter drives.
CLI = "codex"

#: Permission bypass accepted by ``codex exec`` and ``codex resume``.
BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

#: Cap on one raw log stream (plan: 10 MiB per stream).
LOG_MAX_BYTES = 10 * 1024 * 1024

#: Cap on a retained error diagnostic.
ERROR_MAX_CHARS = 2048

#: Bounded probe call, so a wedged CLI cannot hold the preflight.
PROBE_TIMEOUT_S = 10

#: Round timeout when the resolved settings carry none (T001 default).
DEFAULT_TIMEOUT_S = 900

#: Item kinds whose text is the coordinator's assistant answer.
_ANSWER_ITEMS = ("agent_message", "assistant_message")

#: Event fields that may carry the CLI session/thread identity.
_SESSION_FIELDS = ("thread_id", "session_id", "conversation_id")

#: Safe coordinator session identifiers: no option or shell metacharacters.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class CodexError(RuntimeError):
    """A Codex round could not run or did not finish cleanly."""

    def __init__(
        self,
        message: str,
        *,
        stdout: list[str] | None = None,
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout or []
        self.stderr = stderr or ""
        self.returncode = returncode


class SessionMismatchError(CodexError):
    """The CLI answered with a session other than the one being resumed."""


def _bounded(text: str) -> str:
    """``text`` cut to the retained diagnostic cap."""
    text = text or ""
    return text if len(text) <= ERROR_MAX_CHARS else text[:ERROR_MAX_CHARS]


def _settings(record: dict) -> dict:
    """Resolved T001 settings carried by the launch record."""
    authorization = record.get("authorization")
    if not isinstance(authorization, dict):
        return {}
    settings = authorization.get("settings")
    return settings if isinstance(settings, dict) else {}


def _unrestricted(record: dict) -> bool:
    """Unrestricted permissions, the default unless explicitly disabled."""
    value = _settings(record).get("coordinator_unrestricted", True)
    if value is True:
        return True
    if value is False:
        return False
    raise CodexError("coordinator_unrestricted must be a boolean")


def _timeout(record: dict) -> float:
    """Positive round timeout from the resolved settings."""
    value = _settings(record).get("central_round_timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT_S
    return float(value)


def _executable() -> str:
    """Absolute path of the codex CLI on PATH, else the bare name."""
    return shutil.which(CLI) or CLI


def _command(exe: str) -> list[str]:
    """argv prefix for ``exe``, routing a Windows batch shim through cmd."""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe]
    return [exe]


def _lines(text: str) -> list[str]:
    """Bounded log lines: the stream cut at :data:`LOG_MAX_BYTES`."""
    out: list[str] = []
    size = 0
    for line in (text or "").splitlines():
        size += len(line.encode("utf-8", errors="replace")) + 1
        if size > LOG_MAX_BYTES:
            out.append("[truncated]")
            break
        out.append(line)
    return out


def _count(value: object) -> int | None:
    """A reported token count, or ``None`` when the CLI did not report it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _events(stdout: str) -> Iterator[dict]:
    """Every JSONL event in ``stdout``; non-JSON progress lines are skipped."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            yield value


def _session_of(event: dict) -> str:
    """Session identity reported by one event, if any."""
    for field in _SESSION_FIELDS:
        value = event.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _answer_of(event: dict) -> str:
    """Assistant text carried by one completed item event, if any."""
    if event.get("type") != "item.completed":
        return ""
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") not in _ANSWER_ITEMS:
        return ""
    text = item.get("text")
    return text if isinstance(text, str) else ""


def _error_of(event: dict) -> str:
    """Provider failure text carried by one event, if any."""
    if event.get("type") in ("error", "turn.failed"):
        message = event.get("message")
        if not isinstance(message, str):
            failure = event.get("error")
            message = failure.get("message") if isinstance(failure, dict) else None
        return message.strip() if isinstance(message, str) else ""
    item = event.get("item")
    if (
        event.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "error"
        and isinstance(item.get("message"), str)
    ):
        return item["message"].strip()
    return ""


def _safe_session_id(value: object) -> bool:
    return isinstance(value, str) and _SAFE_SESSION_ID.fullmatch(value) is not None


def _sandbox_mode(help_text: str) -> str | None:
    """Explicit sandbox mode for restricted Codex rounds."""
    if "workspace-write" in (help_text or ""):
        return "workspace-write"
    if "read-only" in (help_text or ""):
        return "read-only"
    return None


def parse_events(stdout: str, stderr: str) -> dict:
    """Normalize one round's CLI output."""
    events = list(_events(stdout))
    session_id = ""
    session_ids: set[str] = set()
    text = ""
    model = None
    usage: dict | None = None
    failures: list[str] = []
    for event in events:
        sid = _session_of(event)
        if sid:
            session_ids.add(sid)
            session_id = sid
        text = _answer_of(event) or text
        reported = event.get("model")
        if isinstance(reported, str) and reported:
            model = reported
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        failure = _error_of(event)
        if failure:
            failures.append(failure)

    if len(session_ids) > 1:
        failures.append("conflicting codex session ids: " + ", ".join(sorted(session_ids)))

    tokens_in = tokens_out = tokens_total = None
    estimated = True
    if usage is not None:
        tokens_in = _count(usage.get("input_tokens"))
        tokens_out = _count(usage.get("output_tokens"))
        tokens_total = _count(usage.get("total_tokens"))
        if tokens_total is None and tokens_in is not None and tokens_out is not None:
            tokens_total = tokens_in + tokens_out
        estimated = tokens_in is None and tokens_out is None and tokens_total is None
    if estimated:
        tokens_in = None
        tokens_out = math.ceil(len(text) / 4) if text else None
        tokens_total = tokens_out

    error = _bounded("\n".join(failures))
    if not error and not events and (stdout or "").strip():
        error = "unrecognized codex event output"
    if not error and not text and (stderr or "").strip():
        error = _bounded(stderr.strip())

    haystack = "\n".join([*failures, stderr or ""])
    quota = quota_family(CLI, "included") if any(
        pattern.search(haystack) for pattern in QUOTA_PATTERNS
    ) else None
    return {
        "session_id": session_id or None,
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "estimated": estimated,
        "model": model,
        "quota_family": quota,
        "error": error,
    }


def _headless_argv(record: dict, exe: str) -> list[str]:
    """Exact argv for one headless round, prompt excluded (it goes to stdin)."""
    session = record.get("session_id")
    if session is not None and not _safe_session_id(session):
        raise CodexError("unsafe or invalid codex session id")
    argv = [*_command(exe), "exec"]
    if session:
        argv.append("resume")
    argv.append("--json")
    if _unrestricted(record):
        argv.append(BYPASS_FLAG)
    else:
        mode = _sandbox_mode(_help([*_command(exe), "exec", "--help"]))
        if mode is None:
            raise CodexError("missing codex capabilities: sandbox")
        argv.extend(["--sandbox", mode])
    if session:
        argv.append(str(session))
    return [*argv, "-"]


def headless(record: dict, prompt: str) -> list[str]:
    """Return argv for one finite headless Codex round (prompt goes to stdin)."""
    if not isinstance(record, dict):
        raise CodexError("a coordinator record is required")
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not worktree:
        raise CodexError("a coordinator worktree is required to launch codex")
    if not isinstance(prompt, str) or not prompt:
        raise CodexError("a non-empty round prompt is required")
    session = record.get("session_id")
    if session is not None and not _safe_session_id(session):
        raise CodexError("unsafe or invalid codex session id")

    exe = _executable()
    if not os.path.isabs(exe):
        raise CodexError("codex CLI not found on PATH")

    result = probe(record)
    if not result.get("supported"):
        raise CodexError(result.get("error") or "missing codex capabilities")

    return _headless_argv(record, exe)


def interactive(record: dict) -> list[str]:
    """argv that resumes the persisted session attached to the real terminal."""
    if not isinstance(record, dict):
        raise CodexError("a coordinator record is required")
    session = record.get("session_id")
    if not isinstance(session, str) or not session:
        raise CodexError("interactive resume needs a persisted session_id")
    if not _safe_session_id(session):
        raise CodexError("unsafe or invalid codex session id")

    exe = _executable()
    if not os.path.isabs(exe):
        raise CodexError("codex CLI not found on PATH")

    result = probe(record)
    if not result.get("supported"):
        raise CodexError(result.get("error") or "missing codex capabilities")

    unrestricted = _unrestricted(record)
    argv = [*_command(exe)]
    if not unrestricted and result.get("capabilities", {}).get("approval_policy"):
        argv.extend(["-a", "never"])
    sandbox = _sandbox_mode(_help([*_command(exe), "exec", "--help"]))
    if not unrestricted and sandbox:
        argv.extend(["--sandbox", sandbox])
    argv.append("resume")
    if unrestricted:
        argv.append(BYPASS_FLAG)
    return [*argv, session]


def _help(argv: list[str]) -> str:
    """Combined stdout/stderr of one bounded help or version call."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def probe(record: dict | None = None) -> dict:
    """Report the installed Codex CLI and the capabilities this adapter needs."""
    exe = _executable()
    result: dict[str, Any] = {
        "cli": CLI,
        "executable": exe if os.path.isabs(exe) else "",
        "available": os.path.isabs(exe),
        "version": "",
        "capabilities": {
            "exec_json": False,
            "exec_resume": False,
            "interactive_resume": False,
            "bypass": False,
            "sandbox": False,
            "approval_policy": False,
        },
        "missing": [],
        "supported": False,
        "error": "",
    }
    if not result["available"]:
        result["error"] = "codex CLI not found on PATH"
        result["missing"] = ["exec_json", "exec_resume", "interactive_resume"]
        return result

    command = _command(exe)
    version = _help([*command, "--version"]).strip().splitlines()
    result["version"] = version[0].strip() if version else ""
    exec_help = _help([*command, "exec", "--help"])
    exec_resume_help = _help([*command, "exec", "resume", "--help"])
    top_help = _help([*command, "--help"])
    capabilities = result["capabilities"]
    capabilities["exec_json"] = "--json" in exec_help
    capabilities["exec_resume"] = "resume" in exec_help
    capabilities["interactive_resume"] = bool(
        re.search(r"^\\s*resume\\b", top_help, re.MULTILINE)
    ) or ("resume" in top_help)
    capabilities["bypass"] = BYPASS_FLAG in exec_help or BYPASS_FLAG in top_help
    capabilities["sandbox"] = (
        ("--sandbox" in exec_help)
        or ("workspace-write" in exec_help)
        or ("read-only" in exec_help)
    )
    capabilities["approval_policy"] = (
        ("--ask-for-approval" in top_help)
        or ("-a" in top_help.split())
        or ("approval_policy" in top_help)
        or ("approval_policy" in exec_help)
    )

    unrestricted = True
    if record is not None:
        try:
            unrestricted = _unrestricted(record)
        except CodexError:
            unrestricted = True
    required = ["exec_json", "exec_resume", "interactive_resume"]
    if unrestricted:
        required.append("bypass")
    else:
        required.append("sandbox")
    result["missing"] = [name for name in required if not capabilities[name]]
    result["supported"] = not result["missing"]
    if not result["supported"]:
        result["error"] = "missing codex capabilities: " + ", ".join(result["missing"])
    return result