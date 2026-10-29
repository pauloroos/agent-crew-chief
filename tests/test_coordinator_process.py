"""Tests for coordinator exit classification and process-group signalling."""
from __future__ import annotations

import os
import signal

import pytest


def test_detects_429_5xx_crash_stall(tmp_path: pytest.TempPathFactory) -> None:
    """classify_exit must detect quota errors, server errors, crashes and stalls."""
    from crew_chief.coordinator_process import classify_exit

    # HTTP 429 / rate-limit patterns → "quota"
    assert classify_exit(returncode=1, stderr="HTTP 429 Too Many Requests") == "quota"
    assert classify_exit(returncode=1, stderr="rate limit exceeded") == "quota"
    assert classify_exit(returncode=1, stderr="quota exhausted") == "quota"
    assert classify_exit(returncode=1, stderr="usage limit reached") == "quota"
    assert classify_exit(returncode=1, stderr="too many requests") == "quota"

    # HTTP 5xx patterns → "server_error"
    assert classify_exit(returncode=1, stderr="HTTP 500 Internal Server Error") == "server_error"
    assert classify_exit(returncode=1, stderr="HTTP 502 Bad Gateway") == "server_error"
    assert classify_exit(returncode=1, stderr="HTTP 503 Service Unavailable") == "server_error"

    # Connection reset → "connection_reset"
    assert classify_exit(returncode=1, stderr="Connection reset by peer") == "connection_reset"
    assert classify_exit(returncode=1, stderr="connection reset") == "connection_reset"

    # Process crash (non-zero exit, no recognised pattern) → "crash"
    assert classify_exit(returncode=137, stderr="") == "crash"
    assert classify_exit(returncode=-9, stderr="") == "crash"
    assert classify_exit(returncode=1, stderr="some random failure") == "crash"

    # Clean exit → None
    assert classify_exit(returncode=0, stderr="") is None

    # Output stall: last_output_age exceeds coordinator_stall_s → "stall"
    assert classify_exit(returncode=None, stderr="", last_output_age=120.0, coordinator_stall_s=60.0) == "stall"
    # Below threshold → not a stall
    assert classify_exit(returncode=None, stderr="", last_output_age=30.0, coordinator_stall_s=60.0) is None


# ---------------------------------------------------------------------------
# T013 / FR-020: retry loop with backoff delay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="os.killpg and EPERM semantics on process-exit are POSIX-only (specs/003-hermes-pipeline FR-019)",
)
def test_safe_killpg_tolerates_eperm_after_exit() -> None:
    """_safe_killpg swallows EPERM when the process has already exited."""
    import errno
    import unittest.mock
    from crew_chief.coordinator_process import _safe_killpg

    mock_proc = unittest.mock.MagicMock()
    mock_proc.poll.return_value = 0
    mock_proc.pid = 99999

    def _mock_killpg(pgid: int, sig: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    with unittest.mock.patch("os.killpg", side_effect=_mock_killpg), \
         unittest.mock.patch("crew_chief._os_compat.pid_alive", return_value=False):
        # Direct _safe_killpg call returns cleanly without raising
        _safe_killpg(99999, signal.SIGTERM, proc=mock_proc)


@pytest.mark.skipif(
    os.name == "nt",
    reason="os.killpg and EPERM semantics on process-exit are POSIX-only (specs/003-hermes-pipeline FR-019)",
)
def test_safe_killpg_raises_eperm_when_alive() -> None:
    """_safe_killpg re-raises PermissionError when the process is still alive."""
    import errno
    import unittest.mock
    from crew_chief.coordinator_process import _safe_killpg

    mock_proc = unittest.mock.MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.pid = 88888

    def _mock_killpg(pgid: int, sig: int) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    with unittest.mock.patch("os.killpg", side_effect=_mock_killpg), \
         unittest.mock.patch("crew_chief._os_compat.pid_alive", return_value=True):
        # Direct _safe_killpg call raises PermissionError
        with pytest.raises(PermissionError):
            _safe_killpg(88888, signal.SIGTERM, proc=mock_proc)
