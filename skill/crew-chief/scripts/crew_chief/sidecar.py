"""Sidecar ``crew.toml`` kept next to a spec-kit ``tasks.md``.

spec-kit has no place for the executor-facing facts the coordinator needs
(work type, risk, allowed files, proof command, structured dependencies), so
they live in ``<spec dir>/crew.toml``. The file is read with ``tomllib`` and
written by a small formatter of our own (stdlib only): top-level scalars and
a list, then one ``[tasks.<id>]`` table per task, sorted by task id.
:func:`update_sidecar` edits an existing file in place instead, so the user's
comments survive. Unknown keys are refused on load.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .records import RISKS, WORK_TYPES, write_atomic

SIDECAR_NAME = "crew.toml"

_BOM = chr(0xFEFF)

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
    #: The ``tasks.md`` text this entry was written for (``spec import`` fills
    #: it). When the task with this id now says something else, the entry
    #: belongs to another task: ``dispatch`` and ``brief render`` refuse it.
    #: Empty in files written before the field existed (never checked).
    task: str = ""


#: Keys a ``crew.toml`` may hold at the top level and in a ``[tasks.<id>]``.
TOP_KEYS = ("spec_dir", "job", "global_constraints", "tasks")
TASK_KEYS = (
    "task", "type", "risk", "accept", "files", "proof", "expect", "depends",
    "interfaces",
)


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
    for key in raw:
        if key not in TASK_KEYS:
            raise SidecarError(
                f"{path}: {where}: unknown key {key!r} "
                f"(expected one of {', '.join(TASK_KEYS)})"
            )
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
        task=_string(path, f"{where}: task", raw.get("task", "")),
    )
    _check_vocab(path, task_id, meta)
    return meta


def load_sidecar(spec_dir: Path) -> Sidecar:
    """Read ``<spec_dir>/crew.toml``; a missing file gives an empty sidecar."""
    spec_dir = Path(spec_dir)
    path = spec_dir / SIDECAR_NAME
    if not path.is_file():
        return Sidecar(spec_dir=str(spec_dir))
    _, _, data = _read(path)
    return _from_data(path, data, spec_dir)


def _read(path: Path) -> tuple[str, str, dict]:
    """``(bom, text, data)`` of the file: the text untranslated, minus its BOM."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SidecarError(f"{path}: cannot read sidecar: {exc}") from exc
    bom = _BOM if raw.startswith(_BOM) else ""
    text = raw[len(bom):]
    return bom, text, _loads(path, text)


def _loads(path: Path, text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line = getattr(exc, "lineno", None)
        where = f"line {line}: " if line else ""
        raise SidecarError(f"{path}: invalid TOML at {where}{exc}") from exc


def _from_data(path: Path, data: dict, spec_dir: Path) -> Sidecar:
    for key in data:
        if key not in TOP_KEYS:
            raise SidecarError(
                f"{path}: unknown key {key!r} (expected one of {', '.join(TOP_KEYS)})"
            )
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


def _top_value(sidecar: Sidecar, key: str) -> str:
    value = getattr(sidecar, key)
    return _list(value) if key == "global_constraints" else _quote(value)


def _task_value(meta: TaskMeta, key: str) -> str:
    value = getattr(meta, key)
    if key in ("accept", "files", "depends"):
        return _list(value)
    if key == "interfaces":
        return _inline_table(value)
    return _quote(value)


def _task_lines(task_id: str, meta: TaskMeta) -> list[str]:
    """``[tasks.<id>]`` and its keys; ``task`` only when the entry has one."""
    keys = [key for key in TASK_KEYS if key != "task" or meta.task]
    return [f"[tasks.{_key(task_id)}]"] + [
        f"{key} = {_task_value(meta, key)}" for key in keys
    ]


def _format(sidecar: Sidecar) -> str:
    lines = [
        f"{key} = {_top_value(sidecar, key)}"
        for key in ("spec_dir", "job", "global_constraints")
    ]
    for task_id in sorted(sidecar.tasks):
        lines += ["", *_task_lines(task_id, sidecar.tasks[task_id])]
    return "\n".join(lines) + "\n"


def save_sidecar(spec_dir: Path, sidecar: Sidecar) -> None:
    """Write ``<spec_dir>/crew.toml`` from scratch, atomically; tasks sorted by id.

    Comments in an existing file are lost: ``spec import`` uses
    :func:`update_sidecar` instead.
    """
    path = Path(spec_dir) / SIDECAR_NAME
    for task_id, meta in sidecar.tasks.items():
        _check_vocab(path, task_id, meta)
    write_atomic(path, _format(sidecar))


def update_sidecar(spec_dir: Path, sidecar: Sidecar) -> str:
    """Bring ``<spec_dir>/crew.toml`` in line with ``sidecar``, keeping the user's text.

    Returns what happened: ``"unchanged"``, ``"created"`` (no file before),
    ``"updated"`` or ``"rewritten"``.

    An existing file is edited, not regenerated: new ``[tasks.<id>]`` tables
    are appended at the end, a key missing from an existing table is added
    right under its header, a one-line key whose value changed is replaced, and
    ``spec_dir``/``job``/``global_constraints`` are only ever added when the
    file has none (a value already there wins over ``sidecar``). Comments,
    order and line endings survive. The edited text is parsed back and must
    equal what was asked for; when it does not (an unusual layout, such as
    dotted keys, or a task missing from ``sidecar``), the whole file is
    rewritten from ``sidecar`` and ``"rewritten"`` tells the caller that its
    comments are gone.
    """
    spec_dir = Path(spec_dir)
    path = spec_dir / SIDECAR_NAME
    for task_id, meta in sidecar.tasks.items():
        _check_vocab(path, task_id, meta)
    if not path.is_file():
        write_atomic(path, _format(sidecar))
        return "created"

    bom, text, data = _read(path)
    current = _from_data(path, data, spec_dir)
    wanted = Sidecar(
        spec_dir=current.spec_dir if "spec_dir" in data else sidecar.spec_dir,
        job=current.job if "job" in data else sidecar.job,
        global_constraints=(
            current.global_constraints
            if "global_constraints" in data
            else list(sidecar.global_constraints)
        ),
        tasks=dict(sidecar.tasks),
    )
    if wanted == current:
        return "unchanged"

    edited = _edit(text, data, current, wanted)
    if edited is not None:
        try:
            check = _from_data(path, tomllib.loads(edited), spec_dir)
        except (tomllib.TOMLDecodeError, SidecarError):
            check = None
        if check == wanted:
            write_atomic(path, bom + edited)
            return "updated"
    write_atomic(path, _format(wanted))
    return "rewritten"


def _split_lines(text: str) -> list[str]:
    """Lines of ``text``, each keeping its own line ending."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _header_path(line: str) -> tuple | None:
    """The key path of a ``[a.b]`` table header line, else ``None``."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    node: object = parsed
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
    return tuple(path) if node == {} else None


def _assigns(line: str, key: str) -> bool:
    """Whether ``line`` is a complete one-line ``key = value``."""
    try:
        return list(tomllib.loads(line)) == [key]
    except tomllib.TOMLDecodeError:
        return False


def _edit(text: str, data: dict, current: Sidecar, wanted: Sidecar) -> str | None:
    """``text`` edited in place to hold ``wanted``; ``None`` when it cannot be."""
    if any(task_id not in wanted.tasks for task_id in current.tasks):
        return None
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = _split_lines(text)
    headers = [
        (index, path)
        for index, line in enumerate(lines)
        if line.lstrip().startswith("[")
        and (path := _header_path(line)) is not None
    ]
    edits: list[tuple[int, int, list[str]]] = []  # (start, end, new lines)

    top = [
        f"{key} = {_top_value(wanted, key)}{eol}"
        for key in ("spec_dir", "job", "global_constraints")
        if getattr(current, key) != getattr(wanted, key)
    ]
    if top:
        first = headers[0][0] if headers else len(lines)
        edits.append((first, first, top + ([eol] if headers else [])))

    raw_tasks = data.get("tasks", {})
    for task_id, meta in wanted.tasks.items():
        old = current.tasks.get(task_id)
        if old is None or old == meta:
            continue
        found = [i for i, (_, path) in enumerate(headers) if path == ("tasks", task_id)]
        if len(found) != 1:
            return None
        start = headers[found[0]][0]
        end = headers[found[0] + 1][0] if found[0] + 1 < len(headers) else len(lines)
        added = []
        for key in TASK_KEYS:
            if getattr(old, key) == getattr(meta, key):
                continue
            new_line = f"{key} = {_task_value(meta, key)}{eol}"
            if key not in raw_tasks.get(task_id, {}):
                added.append(new_line)
                continue
            where = [i for i in range(start + 1, end) if _assigns(lines[i], key)]
            if len(where) != 1:
                return None
            edits.append((where[0], where[0] + 1, [new_line]))
        if added:
            edits.append((start + 1, start + 1, added))

    new_ids = sorted(set(wanted.tasks) - set(current.tasks))
    if new_ids:
        tail = []
        for task_id in new_ids:
            tail += [eol] + [f"{row}{eol}" for row in _task_lines(task_id, wanted.tasks[task_id])]
        if lines and not lines[-1].endswith("\n"):
            tail.insert(0, eol)
        edits.append((len(lines), len(lines), tail))

    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[start:end] = new
    return "".join(lines)
