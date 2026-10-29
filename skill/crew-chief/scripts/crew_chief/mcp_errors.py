"""Structured error sanitation for MCP JSON-RPC responses (T004, FR-007).

Ensures that exception strings and error messages returned to MCP clients
never leak internal filesystem paths, traceback frames, or machine-specific
identifiers. All public errors are normalised to ``{code, message}`` dicts
compatible with JSON-RPC 2.0 error objects.
"""
from __future__ import annotations

import re
from typing import Any

# Re-use the standard JSON-RPC internal-error code from mcp_server so callers
# can stay consistent without importing the server module itself.
INTERNAL_ERROR = -32603

# Patterns stripped from every error message before it reaches a client.
# Order matters: traceback frames are removed first so later path patterns
# don't accidentally preserve fragments embedded in frame lines.
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):\s*$", re.MULTILINE)
_TRACEBACK_FRAME = re.compile(
    r'^\s*File "[^"]*", line \d+, in [^\n]*\n(?:\s*[^\n]*\n)?', re.MULTILINE
)
_ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9_./+-]+")
_HOSTNAME_LIKE = re.compile(
    r"\b(?:host|on|at)\s+[A-Za-z0-9_.-]+\b", re.IGNORECASE
)


def _strip_sensitive(text: str) -> str:
    """Remove traceback scaffolding, absolute paths, and host identifiers."""
    cleaned = _TRACEBACK_HEADER.sub("", text)
    cleaned = _TRACEBACK_FRAME.sub("", cleaned)
    cleaned = _ABSOLUTE_PATH.sub("", cleaned)
    cleaned = _HOSTNAME_LIKE.sub("", cleaned)
    # Collapse whitespace left behind by removals so the message stays tidy.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


def sanitize_error(exc: BaseException | str | Any, *, code: int = INTERNAL_ERROR) -> dict[str, Any]:
    """Return a JSON-RPC-safe ``{code, message}`` dict for *exc*.

    Accepts an exception instance, a string, or any other object (coerced via
    ``str()``). The message is sanitised to remove internal paths, traceback
    frames, and machine-specific identifiers before being returned.
    """
    if isinstance(exc, BaseException):
        raw = f"{type(exc).__name__}: {exc}"
    else:
        raw = str(exc)
    message = _strip_sensitive(raw)
    if not message:
        message = "Internal error"
    return {"code": code, "message": message}