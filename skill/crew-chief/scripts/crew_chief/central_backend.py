"""Execution backend over the central core (T036, FR-017).

A backend is the small contract the central commands use to create, start,
message, attach and inspect one coordinator. The default backend delegates the
central core and is the only one; the choice is stored per launch.

Public API
----------
``Backend``
    The protocol every backend implements.
``default_backend(root)``
    The default backend bound to one central root.
``DefaultBackend``
    The accepted core exposed as a backend.
``BackendError``
    A backend operation failed; the message keeps the core diagnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import (
    central_attach,
    central_launch,
    central_messages,
    central_registry,
    central_supervisor,
)

__all__ = [
    "ATTACH_TIMEOUT_KEY",
    "ATTACH_TIMEOUT_S",
    "Backend",
    "BackendError",
    "DEFAULT_BACKEND",
    "DefaultBackend",
    "ON_OPEN_KEY",
    "TERMINAL_KEY",
    "default_backend",
]

#: The name of the backend that delegates the accepted core.
DEFAULT_BACKEND = "default"

#: Transient record keys a caller may add for :meth:`Backend.attach`. They are
#: never persisted: a registry row refuses unknown fields.
TERMINAL_KEY = "terminal"
ATTACH_TIMEOUT_KEY = "attach_timeout_s"
#: A callable told when the interactive session has started (the browser attach).
ON_OPEN_KEY = "on_open"

#: Default bounded wait for the supervisor to yield ownership.
ATTACH_TIMEOUT_S = 30.0


class BackendError(Exception):
    """A backend operation failed; the message keeps the core diagnostic."""


@runtime_checkable
class Backend(Protocol):
    """The launch/message/state/attach contract every backend implements."""

    def probe(self) -> dict[str, Any]:
        """Report whether this backend can run here."""

    def create(self, repo: Path, options: dict[str, Any]) -> dict[str, Any]:
        """Create one coordinator for ``repo``.

        ``options`` carries the launch options accepted by
        :func:`central_launch.prepare_launch` plus the required
        ``request_file``. The result is the launch result: ``ok``, ``error``,
        ``id``, ``record`` and ``adapter``.
        """

    def start(self, record: dict[str, Any]) -> dict[str, Any]:
        """Start the supervisor of an existing coordinator record."""

    def send(self, record: dict[str, Any], envelope: dict[str, Any]) -> str:
        """Store one owner message and return its message ID."""

    def attach(self, record: dict[str, Any]) -> dict[str, Any]:
        """Yield exclusive ownership of a coordinator to a terminal.

        The record may carry the transient keys :data:`TERMINAL_KEY`,
        :data:`ATTACH_TIMEOUT_KEY` and :data:`ON_OPEN_KEY`; they are never persisted.
        """

    def state(self, record: dict[str, Any]) -> dict[str, Any]:
        """Report the live state of one coordinator."""


def _coordinator_id(record: Any) -> str:
    """The canonical coordinator ID of ``record``, or a visible failure."""
    if not isinstance(record, dict):
        raise BackendError("record must be a dict")
    canonical = central_messages._canonical_uuid(record.get("id"))
    if canonical is None:
        raise BackendError(
            f"coordinator id must be a canonical UUID string, got {record.get('id')!r}"
        )
    return canonical


def _state(coordinator_id: str, row: Any, error: str | None = None) -> dict[str, Any]:
    """One coordinator's state, with the same shape with or without an error."""
    row = row if isinstance(row, dict) else {}
    return {
        "ok": error is None,
        "id": coordinator_id,
        "state": row.get("state"),
        "session_id": row.get("session_id"),
        "backend": row.get("backend"),
        "cli": row.get("cli"),
        "worktree": row.get("worktree"),
        "last_error": row.get("last_error"),
        "error": error,
    }


class DefaultBackend:
    """The accepted central core, exposed as a backend."""

    name = DEFAULT_BACKEND

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def probe(self) -> dict[str, Any]:
        """The core needs no optional executable, so it is always available."""
        return {
            "ok": True,
            "backend": self.name,
            "supported": True,
            "root": str(self.root),
        }

    def create(self, repo: Path, options: dict[str, Any]) -> dict[str, Any]:
        """Create one coordinator through the accepted launch preparation."""
        options = options if isinstance(options, dict) else {}
        request_file = options.get("request_file")
        if request_file is None:
            return {
                "ok": False,
                "error": "request_file is required: a launch needs one request file",
                "id": None,
                "record": None,
                "adapter": None,
            }
        return central_launch.prepare_launch(
            Path(repo), Path(request_file), options, self.root
        )

    def start(self, record: dict[str, Any]) -> dict[str, Any]:
        """Start the detached supervisor of one coordinator."""
        return central_supervisor.start_supervisor(record, self.root)

    def send(self, record: dict[str, Any], envelope: dict[str, Any]) -> str:
        """Store one owner message in the coordinator's inbox."""
        coordinator_id = _coordinator_id(record)
        try:
            return central_messages.send(self.root, coordinator_id, envelope)
        except central_messages.MessageError as exc:
            raise BackendError(str(exc)) from exc

    def attach(self, record: dict[str, Any]) -> dict[str, Any]:
        """Yield exclusive ownership of a coordinator to a terminal."""
        coordinator_id = _coordinator_id(record)
        terminal = record.get(TERMINAL_KEY)
        timeout_s = record.get(ATTACH_TIMEOUT_KEY)
        if timeout_s is None:
            timeout_s = ATTACH_TIMEOUT_S
        return central_attach.attach(self.root, coordinator_id, terminal, timeout_s,
                                     record.get(ON_OPEN_KEY))

    def state(self, record: dict[str, Any]) -> dict[str, Any]:
        """Report the stored state of one coordinator."""
        coordinator_id = _coordinator_id(record)
        try:
            registry = central_registry.load_registry(self.root)
        except central_registry.RegistryError as exc:
            return _state(coordinator_id, None, error=str(exc))
        rows = registry.get("coordinators")
        row = rows.get(coordinator_id) if isinstance(rows, dict) else None
        if not isinstance(row, dict):
            return _state(
                coordinator_id,
                None,
                error=f"no coordinator {coordinator_id!r} registered",
            )
        return _state(coordinator_id, row)


def default_backend(root: Path) -> Backend:
    """The backend that delegates the accepted core, bound to ``root``.

    No optional executable is resolved or invoked here.
    """
    return DefaultBackend(root)
