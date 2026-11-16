"""Approved specs: ``ACC spec-import`` copies an approved spec byte for byte and records its SHA-256.

The crew chief never rewrites an imported ``spec.md``; ``ACC summary`` checks the committed copy
against ``approved.json`` and flags ``spec_divergente`` when they differ.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

APPROVED = "approved.json"
# Left in the run's logs folder so ``ACC summary`` finds this run's spec without a flag.
POINTER = "spec-import.json"
# Specs folders a project may already have; the first one found wins, else ``specs/``.
FOLDERS = ("specs", "docs/specs")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
NUMBERED = re.compile(r"^(\d{3})-")


class SpecError(Exception):
    """Nothing was written; the message says why."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checkout() -> Path:
    """This checkout's root (a worktree's own, where the spec gets committed); the cwd outside git."""
    try:
        return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                   capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


def specs_root(root: Path) -> Path:
    return next((root / name for name in FOLDERS if (root / name).is_dir()), root / "specs")


def _folder(specs: Path, slug: str) -> Path:
    """The folder this slug already has, else ``<NNN>-<slug>`` with the next free number."""
    taken = [p for p in specs.iterdir() if p.is_dir()] if specs.is_dir() else []
    for path in sorted(taken):
        match = NUMBERED.match(path.name)
        if match and path.name == f"{match.group(1)}-{slug}":
            return path
    numbers = [int(m.group(1)) for m in (NUMBERED.match(p.name) for p in taken) if m]
    return specs / f"{max(numbers, default=0) + 1:03d}-{slug}"


def import_spec(source: Path, attachments: list[Path], slug: str, root: Path) -> Path:
    """Copy ``source`` to ``<specs>/<NNN>-<slug>/spec.md`` and ``attachments`` to ``anexos/``.

    Refuses (writes nothing) when a file there already differs: an approved spec is never overwritten.
    """
    if not SLUG.match(slug):
        raise SpecError(f"bad slug {slug!r}: letters, digits, '.', '_' and '-' only")
    for path in (source, *attachments):
        if path.is_symlink() or not path.is_file():
            raise SpecError(f"{path}: not a regular file")
    names = [p.name for p in attachments]
    if len(set(names)) != len(names):
        raise SpecError("two attachments with the same name")
    folder = _folder(specs_root(root), slug)
    targets = [(source, folder / "spec.md"), *((p, folder / "anexos" / p.name) for p in attachments)]
    for src, dst in targets:
        if dst.exists() and sha256(dst) != sha256(src):
            raise SpecError(f"{dst} differs from {src}: not overwritten")
    for src, dst in targets:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    # git must store these bytes as they are (core.autocrlf would rewrite the line ends)
    keep = folder / ".gitattributes"
    if not keep.exists():
        keep.write_bytes(b"* -text\n")
    digest = sha256(source)
    if sha256(folder / "spec.md") != digest:
        raise SpecError(f"{folder / 'spec.md'}: copy does not match {source}")
    approved = folder / APPROVED
    if _approved_sha(folder) != digest:  # a second import of the same spec keeps the first record
        approved.write_text(json.dumps({
            "sha256": digest, "fonte": str(source),
            "importado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return folder


def _approved_sha(folder: Path) -> str | None:
    try:
        return json.loads((folder / APPROVED).read_text(encoding="utf-8"))["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check(folder: Path) -> tuple[str | None, bool]:
    """``(approved sha256, divergent)``: divergent when the ``spec.md`` committed at HEAD is not the approved one."""
    approved = _approved_sha(folder)
    if approved is None:
        return None, False
    try:
        committed = subprocess.run(["git", "-C", str(folder), "show", "HEAD:./spec.md"],
                                   capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return approved, True  # not committed: the delivery does not carry the approved spec
    return approved, hashlib.sha256(committed).hexdigest() != approved
