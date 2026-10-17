"""Tool kinds: model listing, command building and output parsing.

A *tool dict* describes one executor CLI: which kind it is, how to list
its models, how to build the dispatch command and which environment
variable names it requires. This module is pure helpers over those
dicts; it holds no project state.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Kinds whose dispatch command accepts an effort/reasoning flag by
#: default (tools may still set ``effort`` explicitly).
EFFORT_DEFAULT_KINDS = ("codex", "antigravity")

#: Patterns that mark a failure as quota/rate-limit related.
QUOTA_PATTERNS: list[re.Pattern] = [
    re.compile("429", re.IGNORECASE),
    re.compile("quota", re.IGNORECASE),
    re.compile("rate limit", re.IGNORECASE),
    re.compile("usage limit", re.IGNORECASE),
    re.compile("too many requests", re.IGNORECASE),
]


@dataclass
class Executor:
    """One usable ``tool`` + ``model`` pair with its cost classification."""

    id: str
    tool: dict
    model: str
    cost: str
    trains: bool


def wants_effort(tool: dict) -> bool:
    """Whether this tool accepts an effort/reasoning flag.

    Defaults to true for ``codex`` and ``antigravity``, false otherwise;
    an explicit ``effort`` key always wins.
    """
    return bool(tool.get("effort", tool["kind"] in EFFORT_DEFAULT_KINDS))


def _resolve_exe(name: str) -> str:
    """Absolute path of ``name`` when it is on PATH, else the bare name."""
    return shutil.which(name) or name


def _run_lines(argv: list[str], timeout_s: int) -> list[str]:
    """Run ``argv`` and return its stdout lines; ``[]`` on any failure."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return (proc.stdout or "").splitlines()


def list_models(tool: dict, timeout_s: int = 60) -> list[str]:
    """Model ids advertised by ``tool``, or ``[]`` on failure.

    Never raises: a CLI failure or a timeout yields ``[]``, and the
    caller turns that into a note. A non-empty ``models`` list is a
    fixed list that overrides listing.
    """
    kind = tool["kind"]
    fixed = list(tool.get("models") or [])
    if kind in ("codex", "claude-code"):
        return fixed
    if kind == "generic":
        if tool.get("list_cmd"):
            argv = [str(a) for a in tool["list_cmd"]]
            return _run_lines([_resolve_exe(argv[0]), *argv[1:]], timeout_s)
        return fixed
    if fixed:
        return fixed
    lines = _run_lines([_resolve_exe(tool["bin"]), "models"], timeout_s)
    if kind == "opencode":
        return [line.strip() for line in lines if line.strip()]
    # antigravity: first tab-separated field, skipping the progress line.
    models = []
    for line in lines:
        if line.startswith("Fetching"):
            continue
        first = line.split("\t", 1)[0].strip()
        if first:
            models.append(first)
    return models


def build_command(
    tool: dict,
    model: str,
    worktree: Path,
    prompt: str,
    brief_file: Path,
    output_file: Path,
    effort: str | None,
    review: bool,
) -> list[str]:
    """Command line that runs ``tool`` on ``prompt`` inside ``worktree``."""
    kind = tool["kind"]
    argv0 = str(tool["run_cmd"][0]) if kind == "generic" else tool["bin"]
    exe = shutil.which(tool["bin"]) or argv0
    use_effort = wants_effort(tool) and effort is not None

    if kind == "claude-code":
        cmd = [
            exe, "-p", prompt, "--model", model, "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if review:
            cmd.append("--disallowedTools=Bash,Edit,Write,NotebookEdit")
        if use_effort:
            cmd += ["--effort", effort]
        return cmd

    if kind == "opencode":
        return [exe, "run", "-m", model, prompt]

    if kind == "codex":
        cmd = [
            exe, "exec", "-C", str(worktree), "-s",
            "read-only" if review else "workspace-write",
            "-m", model, "-o", str(output_file),
        ]
        if use_effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        return cmd + [prompt]

    if kind == "antigravity":
        cmd = [
            exe, "-p", prompt, "--model", model,
            "--dangerously-skip-permissions", "--output-format", "json",
        ]
        if review:
            cmd += ["--mode", "plan"]
        if use_effort:
            cmd += ["--effort", effort]
        return cmd

    # generic: run_cmd with the placeholders formatted.
    values = {
        "worktree": str(worktree),
        "model": model,
        "prompt": prompt,
        "brief_file": str(brief_file),
        "output_file": str(output_file),
    }
    rest = [str(arg).format(**values) for arg in tool["run_cmd"][1:]]
    return [exe, *rest]


def parse_reported_model(tool: dict, stdout: str) -> str | None:
    """Model id the CLI reported in ``stdout``, when it reports one."""
    kind = tool["kind"]
    if kind not in ("claude-code", "antigravity"):
        return None
    try:
        data = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if kind == "claude-code":
        usage = data.get("modelUsage")
        if isinstance(usage, dict) and usage:
            first = next(iter(usage))
            return first if isinstance(first, str) else None
        return None
    model = data.get("model")
    return model if isinstance(model, str) else None


def _fill_from_registry(names: list[str], env: dict[str, str]) -> list[str]:
    """Copy ``names`` from ``HKCU\\Environment`` into ``env``.

    Returns the names still missing. Registry values are never printed.
    """
    import winreg  # Windows-only; imported lazily.

    still_missing = list(names)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in list(still_missing):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if isinstance(value, str):
                    env[name] = value
                    still_missing.remove(name)
    except OSError:
        pass
    return still_missing


def tool_env(tool: dict) -> dict[str, str] | None:
    """Environment for ``tool``: ``os.environ`` plus its declared names.

    A name missing from ``os.environ`` is read from ``HKCU\\Environment``
    on Windows (no registry on other platforms). Returns ``None`` when a
    declared name is still missing.
    """
    env = dict(os.environ)
    missing = [name for name in tool.get("env", []) if name not in env]
    if missing and os.name == "nt":
        missing = _fill_from_registry(missing, env)
    return None if missing else env
