"""Declared models: the ``models.toml`` files the user writes, machine-wide and per project."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from . import ledger

#: Fields one ``[[model]]`` entry may set; anything else is rejected (never read secrets).
FIELDS = ("agent", "model", "cost", "effort", "kinds", "enabled", "note")


class ModelsError(Exception):
    """One bad models.toml; the message is always ``<path>: <problem>``."""


def global_path() -> Path:
    """The machine-wide file, next to the ledger so it follows ``CREW_CHIEF_HOME``."""
    return ledger.ledger_path().parent / "models.toml"


def repo_root() -> Path | None:
    """The main repository root, the same from any worktree; None outside git."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(common).parent


def project_path() -> Path | None:
    """The repository's file; None outside git (there is no project file then)."""
    root = repo_root()
    return root / ".crew-chief" / "models.toml" if root is not None else None


def _tag(index: int, entry: object) -> str:
    tag = f"model #{index + 1}"
    if isinstance(entry, dict):
        agent = entry.get("agent")
        model = entry.get("model")
        agent = agent if isinstance(agent, str) and agent else "?"
        model = model if isinstance(model, str) and model else "?"
        tag += f" ({agent}:{model})"
    return tag


def _read(path: Path, project: bool) -> tuple[list[tuple[dict, set[str]]], bool]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ModelsError(f"{path}: broken TOML: {exc}") from exc
    allowed = {"model", "inherit"} if project else {"model"}
    for key in data:
        if key not in allowed:
            hint = " (it goes in the project file only)" if key == "inherit" else ""
            raise ModelsError(f"{path}: unknown key '{key}'{hint}")
    inherit = data.get("inherit", True)
    if not isinstance(inherit, bool):
        raise ModelsError(f"{path}: 'inherit' must be true or false")
    raw = data.get("model", [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ModelsError(f"{path}: 'model' must be a list of [[model]] tables")
    entries = []
    seen = set()
    for index, item in enumerate(raw):
        tag = _tag(index, item)
        for key in item:
            if key not in FIELDS:
                raise ModelsError(f"{path}: {tag}: unknown key '{key}'")
        agent, model = item.get("agent"), item.get("model")
        if not isinstance(agent, str) or not agent or not isinstance(model, str) or not model:
            raise ModelsError(f"{path}: {tag}: 'agent' and 'model' must be non-empty strings")
        if (agent, model) in seen:
            raise ModelsError(f"{path}: {tag}: duplicate agent+model in this file")
        seen.add((agent, model))
        cost = item.get("cost")
        if cost is not None and cost not in ledger.COSTS:
            raise ModelsError(f"{path}: {tag}: 'cost' must be one of {', '.join(ledger.COSTS)}")
        kinds = item.get("kinds")
        if kinds is not None and (
            not isinstance(kinds, list) or not kinds or any(k not in ledger.KINDS for k in kinds)
        ):
            raise ModelsError(f"{path}: {tag}: 'kinds' must be a non-empty list of {', '.join(ledger.KINDS)}")
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ModelsError(f"{path}: {tag}: 'enabled' must be true or false")
        for field in ("effort", "note"):
            if item.get(field) is not None and not isinstance(item[field], str):
                raise ModelsError(f"{path}: {tag}: '{field}' must be a string")
        entries.append(({
            "agent": agent, "model": model, "cost": cost,
            "effort": item.get("effort"), "kinds": list(kinds) if kinds is not None else None,
            "enabled": item.get("enabled", True), "note": item.get("note"),
        }, set(item)))
    return entries, inherit


def load() -> tuple[list[dict], list[Path]]:
    """The effective models (each with ``source``) and the files they came from."""
    gpath, ppath = global_path(), project_path()
    gentries, _ = _read(gpath, False) if gpath.is_file() else ([], True)
    if ppath is not None and ppath.is_file():
        pentries, inherit = _read(ppath, True)
    else:
        pentries, inherit = [], True
    merged: dict[tuple[str, str], dict] = {}
    if inherit:
        for entry, _ in gentries:
            merged[(entry["agent"], entry["model"])] = {**entry, "source": "global"}
    for entry, present in pentries:
        key = (entry["agent"], entry["model"])
        if key in merged:
            merged[key].update({field: entry[field] for field in present})
            merged[key]["source"] = "project"
        else:
            merged[key] = {**entry, "source": "project"}
    found = [p for p in (gpath, ppath) if p is not None and p.is_file()]
    effective = []
    for entry in merged.values():
        if not entry["enabled"]:
            continue
        if entry["cost"] is None:
            origin = ppath if entry["source"] == "project" else gpath
            raise ModelsError(
                f"{origin}: {entry['agent']}:{entry['model']}: "
                "missing 'cost' (set it here or in the global file)"
            )
        effective.append({k: entry[k] for k in ("agent", "model", "cost", "effort", "kinds", "note", "source")})
    return effective, found
