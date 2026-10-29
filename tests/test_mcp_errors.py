"""Tests for MCP error sanitation (T004, FR-007).

Verifies that internal paths, traceback frames, and machine-specific
identifiers are stripped from error messages before they reach JSON-RPC
clients.
"""
from __future__ import annotations

import pytest


def test_strips_internal_paths_and_tracebacks() -> None:
    """sanitize_error must remove /home/ paths, traceback lines, and hostnames."""
    from crew_chief.mcp_errors import sanitize_error

    raw = (
        "Traceback (most recent call last):\n"
        '  File "/home/orca/acc/agent-crew-chief/skill/crew-chief/scripts/crew_chief/mcp_tools.py", line 42, in start_job\n'
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom at /home/orca/.cache/acc/state.json on host devbox-7\n"
    )

    result = sanitize_error(raw)

    assert isinstance(result, dict)
    assert "code" in result
    assert "message" in result
    assert "/home/" not in result["message"]
    assert "/home/orca" not in result["message"]
    assert "Traceback" not in result["message"]
    assert 'File "' not in result["message"]
    assert "line 42" not in result["message"]
    assert "devbox-7" not in result["message"]
    # The actual error class/message should survive
    assert "boom" in result["message"]


def test_sanitize_error_returns_code_and_message_for_plain_string() -> None:
    """Plain strings without tracebacks still produce {code, message}."""
    from crew_chief.mcp_errors import sanitize_error

    result = sanitize_error("something went wrong")
    assert result == {"code": -32603, "message": "something went wrong"}


def test_sanitize_error_preserves_non_sensitive_content() -> None:
    """Non-sensitive error text is preserved verbatim after stripping."""
    from crew_chief.mcp_errors import sanitize_error

    raw = "ValueError: invalid literal for int() with base 10: 'abc'"
    result = sanitize_error(raw)
    assert result["message"] == raw
    assert result["code"] == -32603


def test_sanitize_error_strips_multiple_paths_in_one_message() -> None:
    """All occurrences of absolute paths are removed, not just the first."""
    from crew_chief.mcp_errors import sanitize_error

    raw = (
        "Failed to read /home/user/config.yaml and /var/log/app.log; "
        "also tried /tmp/staging/data.json"
    )
    result = sanitize_error(raw)
    assert "/home/" not in result["message"]
    assert "/var/log/" not in result["message"]
    assert "/tmp/" not in result["message"]
    assert "Failed to read" in result["message"]