"""Tests for append-only event log with cursor pagination (T005, US1, FR-008)."""

import pytest


def test_monotonic_sequence_and_cursor_replay():
    """Events have strictly monotonic sequence numbers and cursor replay works."""
    from crew_chief.events import EventLog, InvalidCursorError

    log = EventLog()

    # Append events and verify strictly monotonic sequence numbers
    seq1 = log.append({"type": "tool_call", "name": "read"})
    seq2 = log.append({"type": "tool_result", "id": "abc"})
    seq3 = log.append({"type": "notification", "msg": "done"})

    assert seq1 == 1
    assert seq2 == 2
    assert seq3 == 3
    assert seq1 < seq2 < seq3

    # Cursor replay: given N, return events with sequence > N in order
    events_after_1 = log.replay(cursor=1)
    assert len(events_after_1) == 2
    assert events_after_1[0].sequence == 2
    assert events_after_1[1].sequence == 3

    events_after_0 = log.replay(cursor=0)
    assert len(events_after_0) == 3
    assert [e.sequence for e in events_after_0] == [1, 2, 3]

    events_after_3 = log.replay(cursor=3)
    assert events_after_3 == []

    # Forged or out-of-range cursors are rejected
    with pytest.raises(InvalidCursorError):
        log.replay(cursor=-1)

    with pytest.raises(InvalidCursorError):
        log.replay(cursor=999)

    with pytest.raises(InvalidCursorError):
        log.replay(cursor="not-an-int")

    with pytest.raises(InvalidCursorError):
        log.replay(cursor=1.5)