"""Append-only event log with cursor pagination (T005, US1, FR-008)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class InvalidCursorError(Exception):
    """Raised when a replay cursor is invalid or out of range."""


@dataclass(frozen=True)
class Event:
    """A single event in the log."""

    sequence: int
    payload: dict[str, Any]


class EventLog:
    """In-memory append-only event log with strictly monotonic sequence numbers."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._next_seq: int = 1

    def append(self, payload: dict[str, Any]) -> int:
        """Append an event and return its sequence number."""
        seq = self._next_seq
        self._events.append(Event(sequence=seq, payload=payload))
        self._next_seq += 1
        return seq

    def replay(self, cursor: Any) -> list[Event]:
        """Return all events with sequence > cursor, in order.

        Raises InvalidCursorError for non-integer, negative, or out-of-range cursors.
        """
        if not isinstance(cursor, int) or isinstance(cursor, bool):
            raise InvalidCursorError(
                f"cursor must be a non-negative integer, got {type(cursor).__name__}"
            )
        if cursor < 0:
            raise InvalidCursorError(f"cursor must be non-negative, got {cursor}")
        if cursor > len(self._events):
            raise InvalidCursorError(
                f"cursor {cursor} beyond latest sequence {len(self._events)}"
            )
        return [e for e in self._events if e.sequence > cursor]