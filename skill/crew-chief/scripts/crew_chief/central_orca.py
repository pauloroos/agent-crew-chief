"""Optional Orca worktree transport; lifecycle and model sessions stay in core.

Probe the CLI's local command registry. Never start an interactive agent beside
the bounded supervisor or confuse a terminal handle with a model session ID.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import central_launch, central_worktrees
from .central_backend import Backend, BackendError, DefaultBackend

_LIMIT = 1024 * 1024
_FLAGS = {"repo", "name", "setup", "base-branch", "no-parent", "json"}


def _invoke(executable: str, args: list[str]) -> str:
    """Bound child lifetime and disk-backed output, without a shell."""
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run([executable, *args], shell=False, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, timeout=30)
            out.seek(0)
            err.seek(0)
            raw, error = out.read(_LIMIT + 1), err.read(_LIMIT + 1)
        if len(raw) > _LIMIT or len(error) > _LIMIT:
            raise BackendError("Orca output exceeds 1 MiB")
        if result.returncode:
            raise BackendError("Orca command failed: " + error.decode("utf-8", "replace")[:500])
        return raw.decode("utf-8-sig")
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise BackendError(f"Orca command unavailable: {exc}") from exc


class OrcaBackend(DefaultBackend):
    """Use Orca for checkout creation, with the accepted core lifecycle."""

    name = "orca"

    def __init__(self, root: Path, executable: str) -> None:
        super().__init__(root)
        self.executable = executable

    def probe(self) -> dict:
        try:
            version = _invoke(self.executable, ["--version"]).strip()
            help_text = _invoke(self.executable, ["--help"])
            schema = json.loads(_invoke(self.executable, ["agent-context", "--json"]))
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version):
                raise BackendError("unsupported Orca version")
            if "agent-context" not in help_text or not isinstance(schema, dict):
                raise BackendError("unsupported Orca command registry")
            if type(schema.get("schemaVersion")) is not int or schema["schemaVersion"] != 1:
                raise BackendError("unsupported Orca schema version")
            commands = schema.get("commands")
            if not isinstance(commands, list):
                raise BackendError("invalid Orca command registry")
            flags = next((c.get("flags") for c in commands if isinstance(c, dict)
                          and c.get("command") == "worktree create"), None)
            if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags) or not _FLAGS <= set(flags):
                raise BackendError("unsupported Orca worktree create capabilities")
            return {"ok": True, "supported": True, "backend": self.name, "version": version}
        except (BackendError, ValueError, RecursionError) as exc:
            return {"ok": False, "supported": False, "backend": self.name, "error": str(exc)[:700]}

    def _require(self) -> None:
        result = self.probe()
        if not result["ok"]:
            raise BackendError(result["error"])

    def _create_worktree(self, repo: Path, coordinator_id: str, base: str) -> dict:
        root = central_worktrees._repo_root(repo)
        commit = central_worktrees._resolve_commit(root, base)
        try:
            self._require()
            payload = json.loads(_invoke(self.executable, [
                "worktree", "create", "--repo", f"path:{root}",
                "--name", f"central-{coordinator_id}", "--setup", "skip",
                "--base-branch", commit, "--no-parent", "--json",
            ]))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise BackendError("Orca reported an unsuccessful creation")
            # The documented full ID is repo-id::absolute-worktree-path. Obtain
            # Git identity from Git itself, not guessed Orca response fields.
            result = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(result, dict) and "worktree" in result:
                result = result["worktree"]
            full_id = result.get("id") if isinstance(result, dict) else None
            if not isinstance(full_id, str) or "::" not in full_id:
                raise BackendError("Orca reply lacks the full worktree id")
            repo_id, path = full_id.split("::", 1)
            worktree = Path(path)
            if not repo_id or not worktree.is_absolute() or not worktree.is_dir():
                raise BackendError("Orca reply has an invalid worktree id/path")
            if worktree.resolve() == root.resolve():
                raise BackendError("Orca returned the source checkout as a new worktree")
            common = lambda p: (p / central_worktrees._git(p, "rev-parse", "--git-common-dir").strip()).resolve()
            if common(root) != common(worktree):
                raise BackendError("Orca worktree belongs to another repository")
            head = central_worktrees._git(worktree, "rev-parse", "HEAD").strip()
            if head != commit:
                raise BackendError("Orca worktree does not match the requested base")
            return {"coordinator_id": coordinator_id, "repo": str(root),
                    "worktree": str(worktree), "base_commit": head,
                    "branch": central_worktrees._git(worktree, "symbolic-ref", "--short", "HEAD").strip()}
        except (BackendError, ValueError, RecursionError, central_worktrees.WorktreeError) as exc:
            raise central_worktrees.WorktreeError(
                f"Orca creation failed; any partial worktree is preserved, no automatic retry or fallback: {exc}"
            ) from exc

    def create(self, repo: Path, options: dict) -> dict:
        if not isinstance(options, dict):
            return central_launch._failure("options must be a dict")
        if options.get("request_file") is None:
            return central_launch._failure("request_file is required")
        if any(options.get(k) not in (None, self.name) for k in ("backend", "central_backend")):
            return central_launch._failure("explicit backend does not match Orca")
        # Accepted preparation owns validation, registration and authorization.
        # Its injected creator cannot start a model and is called only once.
        return central_launch.prepare_launch(repo, options.get("request_file"),
                                             {**options, "backend": self.name}, self.root,
                                             worktree_creator=self._create_worktree)

    # The lifecycle below is the core's and never calls Orca, so a removed or
    # upgraded Orca cannot block an existing coordinator (FR-011).
    def _owns(self, record: dict) -> None:
        if not isinstance(record, dict) or record.get("backend") != self.name:
            raise BackendError("registered backend does not match Orca")

    def start(self, record: dict) -> dict:
        self._owns(record)
        return super().start(record)

    def send(self, record: dict, envelope: dict) -> str:
        self._owns(record)
        return super().send(record, envelope)

    def attach(self, record: dict) -> dict:
        self._owns(record)
        return super().attach(record)

    def state(self, record: dict) -> dict:
        self._owns(record)
        return super().state(record)


def orca_backend(root: Path, executable: str) -> Backend:
    return OrcaBackend(root, executable)
