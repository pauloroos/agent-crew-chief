"""Claude Code coordinator adapter: capability probe, argv and event parsing.

The adapter never spawns a model run itself: :func:`headless` returns argv and
the caller writes the prompt to the child's stdin, so request text can never
reach a shell or the argument vector. ``probe`` only runs ``--version`` and
``--help``.

The normalized event dict carries ``session_id``, ``text``, ``tokens_in``,
``tokens_out``, ``tokens_total``, ``estimated``, ``model``, ``quota_family``,
``permission_denied`` and ``error``. ``text`` is the last assistant-role answer
(the ``result`` event's own answer field when a CLI version emits no assistant
event); tool payloads are never selected.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from .coordinator_protocol import MAX_PROMPT_BYTES, MAX_RESULT_BYTES
from .tools import QUOTA_PATTERNS

#: Canonical name of this CLI, as recorded in the registry.
CLI = "claude"
#: Quota family reported for a quota-shaped failure.
QUOTA_FAMILY = "claude"
#: Capability commands are cheap; a hung CLI must not block the caller.
PROBE_TIMEOUT_S = 10
#: Bounded stdout accepted by :func:`parse_events`; logs are capped upstream.
MAX_EVENT_CHARS = 16 * 1024 * 1024
_MAX_ERROR_CHARS = 500
_MAX_SESSION_CHARS = 256
_MAX_DENIALS = 32
_VERSION = re.compile(r"\d+\.\d+\.\d+[-+0-9A-Za-z.]*")

_PROBED: dict[str, dict] = {}


class AdapterError(ValueError):
    """The CLI cannot be launched as requested; nothing was started."""


def _exe() -> str:
    """Absolute path of ``claude`` when it is on PATH, else the bare name."""
    return shutil.which(CLI) or CLI


def _diag(text: Any) -> str:
    """One bounded diagnostic line, never a raw multi-line transcript."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:_MAX_ERROR_CHARS]


def _last_line(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    for line in reversed(text.splitlines()):
        if line.strip():
            return _diag(line)
    return ""


def _capabilities_from(help_text: str) -> dict[str, bool]:
    has_mode = "--permission-mode" in help_text
    return {
        "resume": "--resume" in help_text,
        "stream_json": "stream-json" in help_text,
        "verbose": "--verbose" in help_text,
        "bypass_permissions": "--dangerously-skip-permissions" in help_text,
        "permission_mode": has_mode,
        # Permission modes that deny without ever prompting for approval.
        "dontAsk": has_mode and "dontAsk" in help_text,
        "plan": has_mode and "plan" in help_text,
    }


def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def probe() -> dict:
    """Probe the installed CLI capabilities without launching a model.

    ``available`` is false when the executable cannot report a version;
    ``capabilities`` is empty when help could not be read. A failed help read
    keeps ``available`` true and records why in ``error``.
    """
    exe = _exe()
    cached = _PROBED.get(exe)
    if cached is not None:
        return {**cached, "capabilities": dict(cached["capabilities"])}
    result = _probe(exe)
    _PROBED[exe] = result
    return {**result, "capabilities": dict(result["capabilities"])}


def _probe(exe: str) -> dict:
    result: dict[str, Any] = {
        "cli": CLI,
        "available": False,
        "version": None,
        "executable": exe,
        "capabilities": {},
        "restricted_headless": False,
        "error": None,
    }
    version = _run([exe, "--version"])
    if version is None:
        result["error"] = f"{CLI} is not runnable"
        return result
    if version.returncode != 0:
        result["error"] = (
            _last_line(version.stderr) or f"{CLI} --version exited {version.returncode}"
        )
        return result
    match = _VERSION.search(version.stdout or "")
    result["version"] = match.group(0) if match else None
    result["available"] = True

    help_proc = _run([exe, "--help"])
    if help_proc is None:
        result["error"] = f"{CLI} capability probe failed"
        return result
    help_text = (help_proc.stdout or "") + (help_proc.stderr or "")
    if help_proc.returncode != 0 or not help_text.strip():
        result["error"] = (
            _last_line(help_proc.stderr) or f"{CLI} --help exited {help_proc.returncode}"
        )
        return result
    capabilities = _capabilities_from(help_text)
    result["capabilities"] = capabilities
    result["restricted_headless"] = capabilities["dontAsk"] or capabilities["plan"]
    return result


def _unrestricted(record: dict) -> bool:
    """Whether bypass flags are allowed, from the resolved launch settings."""
    authorization = record.get("authorization")
    settings = authorization.get("settings") if isinstance(authorization, dict) else None
    if isinstance(settings, dict) and "coordinator_unrestricted" in settings:
        value = settings["coordinator_unrestricted"]
        if not isinstance(value, bool):
            raise AdapterError(
                f"coordinator_unrestricted must be a boolean, got {value!r}"
            )
        return value
    return True


def _session(record: dict) -> str | None:
    """The persisted session to resume, or ``None`` on a first launch."""
    value = record.get("session_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdapterError(f"session_id must be text, got {value!r}")
    session = value.strip()
    if not session:
        return None
    if len(session) > _MAX_SESSION_CHARS or any(
        character.isspace() or ord(character) < 32 for character in session
    ):
        raise AdapterError("session_id contains whitespace or control characters")
    return session


def _restricted_flags() -> list[str]:
    """A supported deny-without-prompt mode, or a visible capability error."""
    info = probe()
    if not info["available"]:
        raise AdapterError(
            "restricted claude round needs a capability probe: "
            + (info["error"] or f"{CLI} is not available")
        )
    capabilities = info["capabilities"]
    if capabilities.get("dontAsk"):
        return ["--permission-mode", "dontAsk"]
    if capabilities.get("plan"):
        return ["--permission-mode", "plan"]
    raise AdapterError(
        "restricted claude headless rounds are unsupported: no non-prompt "
        "deny permission mode was reported; use unrestricted or attach interactively"
    )


def headless(record: dict, prompt: str) -> list[str]:
    """argv for one headless round; the caller writes ``prompt`` to stdin."""
    if not isinstance(record, dict):
        raise AdapterError("record must be an object")
    if not isinstance(prompt, str) or not prompt.strip():
        raise AdapterError("round prompt must be non-empty text")
    try:
        size = len(prompt.encode("utf-8"))
    except UnicodeError as exc:
        raise AdapterError("round prompt is not valid UTF-8 text") from exc
    if size > MAX_PROMPT_BYTES:
        raise AdapterError(f"round prompt exceeds {MAX_PROMPT_BYTES} UTF-8 bytes")

    argv = [_exe(), "-p", "--output-format", "stream-json", "--verbose"]
    session = _session(record)
    if session is not None:
        argv += ["--resume", session]
    if _unrestricted(record):
        argv.append("--dangerously-skip-permissions")
    else:
        argv += _restricted_flags()
    return argv


def interactive(record: dict) -> list[str]:
    """argv that resumes the persisted session in the caller's terminal."""
    if not isinstance(record, dict):
        raise AdapterError("record must be an object")
    session = _session(record)
    if session is None:
        raise AdapterError("interactive attach requires a persisted session_id")
    argv = [_exe(), "--resume", session]
    if _unrestricted(record):
        argv.append("--dangerously-skip-permissions")
    return argv


def _events(stdout: str) -> list[dict]:
    """The JSON objects in ``stdout``: the whole text if it is one, else lines."""
    text = stdout.removeprefix("\ufeff").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
    except (ValueError, RecursionError):
        whole = None
    if isinstance(whole, dict):
        return [whole]
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _blocks_text(content: Any) -> str:
    """Text blocks of one assistant message; tool payloads are skipped."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _is_error(event: dict) -> bool:
    if event.get("is_error") is True:
        return True
    subtype = event.get("subtype")
    return isinstance(subtype, str) and subtype.startswith("error")


def _denials(event: dict) -> list[str]:
    """Denied tool names only; tool input may hold secrets and is dropped."""
    raw = event.get("permission_denials")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw[:_MAX_DENIALS]:
        name = item.get("tool_name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip()[:100])
    return names


def _quota(text: str) -> bool:
    return any(pattern.search(text) for pattern in QUOTA_PATTERNS)


def parse_events(stdout: str, stderr: str) -> dict:
    """Normalize one stream-json transcript, keeping failures visible."""
    result: dict[str, Any] = {
        "session_id": None,
        "text": "",
        "tokens_in": None,
        "tokens_out": None,
        "tokens_total": None,
        "estimated": False,
        "model": None,
        "quota_family": None,
        "permission_denied": [],
        "error": None,
    }
    stdout = stdout if isinstance(stdout, str) else ""
    stderr = stderr if isinstance(stderr, str) else ""
    if len(stdout) > MAX_EVENT_CHARS:
        result["error"] = f"{CLI} output exceeds {MAX_EVENT_CHARS} characters"
        return result

    events = _events(stdout)
    sessions: list[str] = []
    model: str | None = None
    text = ""
    result_event: dict | None = None
    for event in events:
        session = event.get("session_id")
        if isinstance(session, str) and session.strip() and session not in sessions:
            sessions.append(session)
        if event.get("type") == "result" or "num_turns" in event:
            result_event = event
        if event.get("type") == "system" and isinstance(event.get("model"), str):
            model = event["model"]
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            if isinstance(message.get("model"), str):
                model = message["model"]
            answer = _blocks_text(message.get("content"))
            if answer:
                text = answer

    usage = None
    error = ""
    if result_event is not None:
        failed = _is_error(result_event)
        reported = result_event.get("result")
        if failed:
            error = _diag(reported) or (
                f"{CLI} reported {result_event.get('subtype') or 'an error'}"
            )
        elif not text and isinstance(reported, str) and reported.strip():
            text = reported
        usage = result_event.get("usage")
        result["permission_denied"] = _denials(result_event)
        model_usage = result_event.get("modelUsage")
        if model is None and isinstance(model_usage, dict) and model_usage:
            first = next(iter(model_usage))
            model = first if isinstance(first, str) else None

    if not text:
        result["text"] = ""
    elif len(text.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        result["error"] = f"assistant text exceeds {MAX_RESULT_BYTES} UTF-8 bytes"
        return result
    else:
        result["text"] = text

    if isinstance(usage, dict):
        result["tokens_in"] = (
            _int(usage.get("input_tokens"))
            + _int(usage.get("cache_creation_input_tokens"))
            + _int(usage.get("cache_read_input_tokens"))
        )
        result["tokens_out"] = _int(usage.get("output_tokens"))
        result["tokens_total"] = result["tokens_in"] + result["tokens_out"]
    elif text:
        # shortcut: the estimate counts answer characters only; upgrade when
        # the CLI starts reporting usage for these rounds.
        result["estimated"] = True
        result["tokens_total"] = (len(text) + 3) // 4

    result["model"] = model
    if error:
        result["error"] = error
    if len(sessions) > 1:
        result["error"] = f"{CLI} reported conflicting session ids"
    elif sessions:
        result["session_id"] = sessions[0]
    if _quota(f"{error}\n{_last_line(stderr)}"):
        result["quota_family"] = QUOTA_FAMILY
    if result["error"] is None and not events:
        result["error"] = _last_line(stderr) or f"{CLI} reported no usable events"
    return result
