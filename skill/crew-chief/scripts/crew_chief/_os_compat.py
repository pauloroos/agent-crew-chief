"""Portable OS helpers for cross-platform (Linux/macOS/Windows) support."""

from __future__ import annotations

import os
import sys


def pid_alive(pid: int) -> bool:
    """Return ``True`` when a process with *pid* is still running.

    POSIX: uses ``os.kill(pid, 0)`` — ProcessLookupError means dead,
    PermissionError means alive (owned by another user).

    Windows: uses ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` +
    ``GetExitCodeProcess``; signal 0 on Windows sends CTRL_C_EVENT to the
    console group and must never be used as a liveness probe.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        return _pid_alive_win32(pid)

    # POSIX path
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_alive_win32(pid: int) -> bool:
    """Windows-specific liveness check via Win32 API."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    GetExitCodeProcess.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        CloseHandle(handle)