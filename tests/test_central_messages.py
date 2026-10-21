"""Tests for durable coordinator inboxes and outboxes (T004, FR-007/008/022/024)."""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crew_chief import central_messages
from crew_chief.central_messages import (
    BODY_MAX_BYTES,
    ENVELOPE_MAX_BYTES,
    ENVELOPE_READ_MAX_BYTES,
    MessageError,
    acknowledge,
    pending,
    questions,
    send,
)


def _root(tmp_path: Path) -> Path:
    return tmp_path / "central"


def _coord(tmp_path: Path) -> str:
    return str(uuid.uuid4())


def _envelope(coordinator_id: str, **overrides) -> dict:
    base = {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "coordinator_id": coordinator_id,
        "direction": "in",
        "kind": "message",
        "created_at": "2026-01-01T00:00:00+00:00",
        "reply_to": None,
        "task_refs": [],
        "body": "hello",
    }
    base.update(overrides)
    return base


def test_send_returns_id_and_pending_exposes_it(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    mid = send(root, cid, env)
    assert mid == env["id"]
    rows = pending(root, cid, "in")
    assert [row["id"] for row in rows] == [mid]
    assert rows[0]["body"] == "hello"


def test_acknowledge_removes_from_pending(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    mid = send(root, cid, env)
    rid = str(uuid.uuid4())
    acknowledge(root, cid, mid, rid)
    assert pending(root, cid, "in") == []


def test_crash_before_ack_keeps_pending(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    mid = send(root, cid, env)
    # No acknowledge: message remains pending (crash/retry).
    assert [row["id"] for row in pending(root, cid, "in")] == [mid]


def test_replay_same_id_is_idempotent(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    mid = send(root, cid, env)
    assert send(root, cid, env) == mid
    assert len(pending(root, cid, "in")) == 1


def test_replay_conflicting_envelope_rejected(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    send(root, cid, env)
    with pytest.raises(MessageError):
        send(root, cid, {**env, "body": "different"})


def test_concurrent_sends_preserve_all(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)

    def _worker(i):
        env = _envelope(cid, body=f"msg-{i}")
        return send(root, cid, env)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(_worker, range(8)))
    assert len(set(ids)) == 8
    assert len(pending(root, cid, "in")) == 8


def test_question_correlation_and_independent_survival(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    qid = str(uuid.uuid4())
    qenv = _envelope(cid, id=qid, direction="out", kind="question",
                     task_refs=["T002"], body="choose")
    send(root, cid, qenv)
    assert [q["id"] for q in questions(root)] == [qid]
    # An answer correlated to the question resolves it.
    aenv = _envelope(cid, direction="in", kind="answer", reply_to=qid,
                     task_refs=["T002"], body="use X")
    send(root, cid, aenv)
    assert questions(root) == []


def test_malformed_oversized_body_rejected(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    with pytest.raises(MessageError):
        send(root, cid, _envelope(cid, body="x" * (BODY_MAX_BYTES + 1)))
    with pytest.raises(MessageError):
        send(root, cid, _envelope(cid, body="x" * 10, schema_version=2))
    with pytest.raises(MessageError):
        send(root, cid, {"not": "an envelope"})


def test_oversized_envelope_rejected(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    # A single very long task ref pushes the envelope over its cap.
    huge_ref = "T" + "x" * (ENVELOPE_MAX_BYTES + 1)
    with pytest.raises(MessageError):
        send(root, cid, _envelope(cid, task_refs=[huge_ref]))


def test_traversal_coordinator_rejected(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(MessageError):
        send(root, "../evil", _envelope("../evil"))
    with pytest.raises(MessageError):
        send(root, "not-a-uuid", _envelope("not-a-uuid"))


def test_invalid_direction_rejected(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    with pytest.raises(MessageError):
        pending(root, cid, "sideways")
    with pytest.raises(MessageError):
        send(root, cid, _envelope(cid, direction="sideways"))


def test_missing_root_questions_empty(tmp_path):
    assert questions(_root(tmp_path)) == []


def test_bom_crlf_read(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    mid = str(uuid.uuid4())
    coord = root / "coordinators" / cid
    (coord / "inbox").mkdir(parents=True)
    env = {
        "schema_version": 1,
        "id": mid,
        "coordinator_id": cid,
        "direction": "in",
        "kind": "message",
        "created_at": "x",
        "reply_to": None,
        "task_refs": [],
        "body": "hi",
    }
    import json
    text = json.dumps(env, indent=2) + "\n"
    (coord / "inbox" / f"{mid}.json").write_bytes(
        b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8")
    )
    rows = pending(root, cid, "in")
    assert [r["id"] for r in rows] == [mid]


def test_ack_file_maps_round_id(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    env = _envelope(cid)
    mid = send(root, cid, env)
    rid = str(uuid.uuid4())
    acknowledge(root, cid, mid, rid)
    ack_path = root / "coordinators" / cid / "acknowledgments" / f"{mid}.json"
    data = json.loads(ack_path.read_text(encoding="utf-8"))
    assert data["round_id"] == rid or data["consumed_round_id"] == rid


def test_corrupt_record_isolated(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    good = _envelope(cid, body="good")
    gmid = send(root, cid, good)
    # Write a corrupt sibling file.
    inbox = root / "coordinators" / cid / "inbox"
    (inbox / "corrupt.json").write_text("{not json", encoding="utf-8")
    # A valid UUID-named corrupt file? Use a UUID name to pass filename check.
    bad_id = str(uuid.uuid4())
    (inbox / f"{bad_id}.json").write_text("{not json", encoding="utf-8")
    rows = pending(root, cid, "in")
    assert [r["id"] for r in rows] == [gmid]


def test_read_cap(tmp_path):
    root = _root(tmp_path)
    cid = _coord(tmp_path)
    inbox = root / "coordinators" / cid / "inbox"
    inbox.mkdir(parents=True)
    mid = str(uuid.uuid4())
    (inbox / f"{mid}.json").write_bytes(b"x" * (ENVELOPE_READ_MAX_BYTES + 1))
    # Should not crash; corrupt/oversized isolated.
    assert pending(root, cid, "in") == []

