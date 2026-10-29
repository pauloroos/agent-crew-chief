"""Unit tests for portable OS compatibility helpers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from crew_chief._os_compat import pid_alive


def test_pid_alive_current_process() -> None:
    """Current running process PID must be reported alive."""
    assert pid_alive(os.getpid()) is True


def test_pid_alive_finished_child() -> None:
    """A reaped finished child process must be reported not alive."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    proc.wait()
    assert pid_alive(proc.pid) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_no_os_kill_zero_outside_os_compat() -> None:
    """Ensure no product code calls os.kill(pid, 0) directly outside _os_compat.py."""
    root = Path(__file__).resolve().parent.parent
    scripts_dir = root / "skill" / "crew-chief" / "scripts" / "crew_chief"

    pattern = re.compile(r"os\.kill\([^,]+,\s*0\)")
    violations: list[str] = []

    for py_file in scripts_dir.rglob("*.py"):
        if py_file.name == "_os_compat.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            # Ignore comments
            clean_line = line.split("#")[0]
            if pattern.search(clean_line):
                violations.append(f"{py_file.relative_to(root)}:{line_no}: {line.strip()}")

    assert not violations, (
        f"Found direct os.kill(*, 0) calls outside _os_compat.py:\n"
        + "\n".join(violations)
    )
