"""Sidecar ``crew.toml`` kept next to a spec-kit ``tasks.md``.

spec-kit has no place for the executor-facing facts the coordinator needs
(work type, risk, allowed files, proof command, structured dependencies), so
they live in ``<spec dir>/crew.toml``. The file is read with ``tomllib`` and
written by a small formatter of our own (stdlib only): top-level scalars and
a list, then one ``[tasks.<id>]`` table per task, sorted by task id.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .records import RISKS, WORK_TYPES, write_atomic

SIDECAR_NAME = "crew.toml"

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# Short TOML escapes; every other control character becomes ``\u00XX``.
_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class SidecarError(Exception):
    """Raised when ``crew.toml`` is unreadable or holds invalid values."""


@dataclass
class TaskMeta:
    """Coordinator-side facts about one spec-kit task."""

    type: str
    risk: str
    accept: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    proof: str = ""
    expect: str = ""
    depends: list[str] = field(default_factory=list)
    interfaces: dict[str, str] = field(default_factory=dict)


@dataclass
class Sidecar:
    """Contents of one ``crew.toml``."""

    spec_dir: str
    job: str = ""
    global_constraints: list[str] = field(default_factory=list)
    tasks: dict[str, TaskMeta] = field(default_factory=dict)


# -- reading -----------------------------------------------------------------


def _check_vocab(path: Path, task_id: str, meta: TaskMeta) -> None:
    if meta.type not in WORK_TYPES:
        raise SidecarError(
            f"{path}: task {task_id}: unknown type {meta.type!r} "
            f"(expected one of {', '.join(WORK_TYPES)})"
        )
    if meta.risk not in RISKS:
        raise SidecarError(
            f"{path}: task {task_id}: unknown risk {meta.risk!r} "
            f"(expected one of {', '.join(RISKS)})"
        )


def _string(path: Path, where: str, value: Any) -> str:
    if not isinstance(value, str):
        raise SidecarError(f"{path}: {where} must be a string")
    return value


def _string_list(path: Path, where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SidecarError(f"{path}: {where} must be a list of strings")
    return list(value)


def _string_map(path: Path, where: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(v, str) for v in value.values()
    ):
        raise SidecarError(f"{path}: {where} must be a table of strings")
    return dict(value)


def _parse_task(path: Path, task_id: str, raw: Any) -> TaskMeta:
    where = f"task {task_id}"
    if not isinstance(raw, dict):
        raise SidecarError(f"{path}: {where} must be a table")
    for required in ("type", "risk"):
        if required not in raw:
            raise SidecarError(f"{path}: {where}: missing {required!r}")
    meta = TaskMeta(
        type=_string(path, f"{where}: type", raw["type"]),
        risk=_string(path, f"{where}: risk", raw["risk"]),
        accept=_string_list(path, f"{where}: accept", raw.get("accept", [])),
        files=_string_list(path, f"{where}: files", raw.get("files", [])),
        proof=_string(path, f"{where}: proof", raw.get("proof", "")),
        expect=_string(path, f"{where}: expect", raw.get("expect", "")),
        depends=_string_list(path, f"{where}: depends", raw.get("depends", [])),
        interfaces=_string_map(
            path, f"{where}: interfaces", raw.get("interfaces", {})
        ),
    )
    _check_vocab(path, task_id, meta)
    return meta


def load_sidecar(spec_dir: Path) -> Sidecar:
    """Read ``<spec_dir>/crew.toml``; a missing file gives an empty sidecar."""
    spec_dir = Path(spec_dir)
    path = spec_dir / SIDECAR_NAME
    if not path.is_file():
        return Sidecar(spec_dir=str(spec_dir))
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise SidecarError(f"{path}: cannot read sidecar: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        where = f"line {line}: " if line else ""
        raise SidecarError(f"{path}: invalid TOML at {where}{exc}") from exc

    raw_tasks = data.get("tasks", {})
    if not isinstance(raw_tasks, dict):
        raise SidecarError(f"{path}: 'tasks' must be a table of tables")
    return Sidecar(
        spec_dir=_string(path, "spec_dir", data.get("spec_dir", str(spec_dir))),
        job=_string(path, "job", data.get("job", "")),
        global_constraints=_string_list(
            path, "global_constraints", data.get("global_constraints", [])
        ),
        tasks={
            str(task_id): _parse_task(path, str(task_id), raw)
            for task_id, raw in raw_tasks.items()
        },
    )


# -- writing -----------------------------------------------------------------


def _quote(value: str) -> str:
    """Return ``value`` as a TOML basic string (exact round trip)."""
    out = []
    for ch in value:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(name: str) -> str:
    return name if _BARE_KEY.fullmatch(name) else _quote(name)


def _list(values: list[str]) -> str:
    return "[" + ", ".join(_quote(v) for v in values) + "]"


def _inline_table(values: dict[str, str]) -> str:
    if not values:
        return "{}"
    return "{ " + ", ".join(f"{_key(k)} = {_quote(v)}" for k, v in values.items()) + " }"


def _format(sidecar: Sidecar) -> str:
    lines = [
        f"spec_dir = {_quote(sidecar.spec_dir)}",
        f"job = {_quote(sidecar.job)}",
        f"global_constraints = {_list(sidecar.global_constraints)}",
    ]
    for task_id in sorted(sidecar.tasks):
        meta = sidecar.tasks[task_id]
        lines += [
            "",
            f"[tasks.{_key(task_id)}]",
            f"type = {_quote(meta.type)}",
            f"risk = {_quote(meta.risk)}",
            f"accept = {_list(meta.accept)}",
            f"files = {_list(meta.files)}",
            f"proof = {_quote(meta.proof)}",
            f"expect = {_quote(meta.expect)}",
            f"depends = {_list(meta.depends)}",
            f"interfaces = {_inline_table(meta.interfaces)}",
        ]
    return "\n".join(lines) + "\n"


def save_sidecar(spec_dir: Path, sidecar: Sidecar) -> None:
    """Write ``<spec_dir>/crew.toml`` atomically; tasks sorted by id."""
    path = Path(spec_dir) / SIDECAR_NAME
    for task_id, meta in sidecar.tasks.items():
        _check_vocab(path, task_id, meta)
    write_atomic(path, _format(sidecar))
