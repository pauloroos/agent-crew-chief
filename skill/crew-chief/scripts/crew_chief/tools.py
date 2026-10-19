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
        # JSON events carry the step count and the tokens (parse_metrics);
        # output_text turns them back into the readable answer.
        return [exe, "run", "--format", "json", "-m", model, prompt]

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


#: ``tokens used: 12,345`` (or the number on the next line) in codex's log.
_CODEX_TOKENS = re.compile(r"tokens used\s*:?\s*([\d][\d,.]*)", re.IGNORECASE)


def _json_lines(text: str) -> list[dict]:
    """Every line of ``text`` that parses as a JSON object, in order."""
    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            events.append(data)
    return events


def _int(value: object) -> int:
    """``value`` as a token count; anything that is not a number counts 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _metrics(
    steps: int | None, tokens_in: int | None, tokens_out: int | None,
    tokens_total: int | None = None,
) -> dict:
    """The metrics dict; the total defaults to ``in + out`` when both are known."""
    if tokens_total is None and tokens_in is not None and tokens_out is not None:
        tokens_total = tokens_in + tokens_out
    return {
        "steps": steps,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
    }


def _opencode_metrics(stdout: str) -> dict:
    """Steps and tokens from ``opencode run --format json`` events.

    One step per ``step_start`` event (one turn of the agent loop). Tokens
    are summed over the ``step_finish`` events: in = input + cache read +
    cache write, out = output + reasoning. opencode never closes the last
    step with a ``step_finish``, so the final answer's tokens are missing;
    with no ``step_finish`` at all the tokens are unknown.
    """
    events = _json_lines(stdout)
    steps = sum(1 for event in events if event.get("type") == "step_start")
    tokens_in = tokens_out = 0
    finished = 0
    for event in events:
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        tokens = part.get("tokens") if isinstance(part, dict) else None
        if not isinstance(tokens, dict):
            continue
        finished += 1
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        tokens_in += (
            _int(tokens.get("input")) + _int(cache.get("read"))
            + _int(cache.get("write"))
        )
        tokens_out += _int(tokens.get("output")) + _int(tokens.get("reasoning"))
    if not finished:
        return _metrics(steps or None, None, None)
    return _metrics(steps or None, tokens_in, tokens_out)


def _result_event(stdout: str) -> dict | None:
    """The claude-code ``result`` object: the whole JSON or the last event."""
    try:
        whole = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        whole = None
    events = [whole] if isinstance(whole, dict) else _json_lines(stdout)
    for event in reversed(events):
        if event.get("type") == "result" or "num_turns" in event:
            return event
    return None


def parse_metrics(tool: dict, stdout: str, stderr: str = "") -> dict:
    """Agent steps and tokens the CLI reported, as far as its output tells.

    Returns ``{"steps", "tokens_in", "tokens_out", "tokens_total"}``; a value
    the output does not carry is ``None`` (the caller then estimates the
    tokens). Per kind:

    * ``opencode`` (``--format json``): see :func:`_opencode_metrics`.
    * ``claude-code`` (json or stream-json): ``num_turns`` and ``usage`` of
      the ``result`` event; in = input + cache creation + cache read.
    * ``antigravity`` (json): ``num_turns`` and ``usage``; in = input + cache
      read, out = output + thinking, total = ``total_tokens`` when present.
    * ``codex``: only the total, from ``tokens used`` in its log.
    * ``generic``: nothing.
    """
    kind = tool.get("kind")
    if kind == "opencode":
        return _opencode_metrics(stdout)
    if kind in ("claude-code", "antigravity"):
        data = _result_event(stdout)
        if data is None:
            return _metrics(None, None, None)
        turns = data.get("num_turns")
        steps = _int(turns) if isinstance(turns, (int, float)) else None
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return _metrics(steps, None, None)
        if kind == "claude-code":
            tokens_in = (
                _int(usage.get("input_tokens"))
                + _int(usage.get("cache_creation_input_tokens"))
                + _int(usage.get("cache_read_input_tokens"))
            )
            return _metrics(steps, tokens_in, _int(usage.get("output_tokens")))
        tokens_in = _int(usage.get("input_tokens")) + _int(
            usage.get("cache_read_tokens")
        )
        tokens_out = _int(usage.get("output_tokens")) + _int(
            usage.get("thinking_tokens")
        )
        total = usage.get("total_tokens")
        return _metrics(
            steps, tokens_in, tokens_out,
            _int(total) if isinstance(total, (int, float)) else None,
        )
    if kind == "codex":
        matches = _CODEX_TOKENS.findall(f"{stderr or ''}\n{stdout or ''}")
        if matches:
            digits = re.sub(r"[^\d]", "", matches[-1])
            if digits:
                return _metrics(None, None, None, int(digits))
    return _metrics(None, None, None)


def output_text(tool: dict, stdout: str) -> str:
    """The readable answer inside ``stdout``.

    ``opencode --format json`` prints events; their ``text`` parts are joined
    with blank lines. Any other kind, or output with no text event, is
    returned unchanged.
    """
    if tool.get("kind") != "opencode":
        return stdout
    texts = []
    for event in _json_lines(stdout):
        part = event.get("part")
        if event.get("type") == "text" and isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n\n".join(texts) if texts else stdout


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


def _env_and_missing(tool: dict) -> tuple[dict[str, str], list[str]]:
    """The environment for ``tool`` plus its still-missing declared names.

    Starts from ``os.environ``; a declared name missing from it is read from
    ``HKCU\\Environment`` on Windows (no registry elsewhere). Registry values
    are never printed.
    """
    env = dict(os.environ)
    missing = [name for name in tool.get("env", []) if name not in env]
    if missing and os.name == "nt":
        missing = _fill_from_registry(missing, env)
    return env, missing


def missing_env(tool: dict) -> list[str]:
    """Declared env var names that are still missing for ``tool``.

    Uses the same lookup as :func:`tool_env`: ``os.environ`` first, then
    ``HKCU\\Environment`` on Windows. Returns ``[]`` when the tool declares
    nothing or everything is satisfied.
    """
    return _env_and_missing(tool)[1]


def tool_env(tool: dict) -> dict[str, str] | None:
    """Environment for ``tool``: ``os.environ`` plus its declared names.

    A name missing from ``os.environ`` is read from ``HKCU\\Environment``
    on Windows (no registry on other platforms). Returns ``None`` when a
    declared name is still missing.
    """
    env, missing = _env_and_missing(tool)
    return None if missing else env
