"""Tests for durable coordinator inboxes and outboxes (T004, FR-007/008/022/024)."""

from __future__ import annotations

import json
import os
import uuid
import threading
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
    with pytest.warns(UserWarning, match="invalid JSON"):
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
    with pytest.warns(UserWarning, match="cap"):
        assert pending(root, cid, "in") == []


def test_symbolic_message_and_box_cannot_escape(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    coord = root / "coordinators" / cid
    coord.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"untouched")
    box = coord / "inbox"
    try:
        box.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("native symlink capability unavailable")
    with pytest.raises(MessageError, match="link|escape"):
        send(root, cid, _envelope(cid))
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"untouched"
    box.unlink()
    env = _envelope(cid)
    send(root, cid, env)
    path = box / f'{env["id"]}.json'
    path.unlink()
    path.symlink_to(sentinel)
    with pytest.raises(MessageError, match="link"):
        send(root, cid, env)
    with pytest.warns(UserWarning, match="link"):
        assert pending(root, cid, "in") == []
    assert sentinel.read_bytes() == b"untouched"


def test_question_filename_mismatch_is_reported_and_preserved(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    env = _envelope(cid, direction="out", kind="question", task_refs=["T001"])
    identifier = send(root, cid, env)
    path = root / "coordinators" / cid / "outbox" / f"{identifier}.json"
    changed = {**env, "id": str(uuid.uuid4())}
    raw = json.dumps(changed).encode()
    path.write_bytes(raw)
    with pytest.warns(UserWarning, match="filename"):
        assert questions(root) == []
    assert path.read_bytes() == raw


def test_empty_ack_warns_and_redelivers(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    mid = send(root, cid, _envelope(cid))
    path = root / "coordinators" / cid / "acknowledgments" / f"{mid}.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.warns(UserWarning, match="ack"):
        assert [row["id"] for row in pending(root, cid, "in")] == [mid]
    before = path.read_bytes()
    with pytest.raises(MessageError):
        acknowledge(root, cid, mid, str(uuid.uuid4()))
    assert path.read_bytes() == before


def test_deeply_malformed_json_does_not_block_healthy_message(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    mid = send(root, cid, _envelope(cid))
    path = root / "coordinators" / cid / "inbox" / f"{uuid.uuid4()}.json"
    path.write_text("[" * 20000 + "0" + "]" * 20000, encoding="utf-8")
    with pytest.warns(UserWarning, match="JSON"):
        assert [row["id"] for row in pending(root, cid, "in")] == [mid]


def test_equivalent_reply_replay_returns_original_id(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    qid = send(root, cid, _envelope(cid, direction="out", kind="question", task_refs=["T001"]))
    first = send(root, cid, _envelope(cid, kind="answer", reply_to=qid, body="choice"))
    second = send(root, cid, _envelope(cid, kind="answer", reply_to=qid, body="choice"))
    assert first == second
    assert len(pending(root, cid, "in")) == 1
    with pytest.raises(MessageError):
        send(root, cid, _envelope(cid, kind="answer", reply_to=qid, body="different choice"))


def test_answer_requires_an_existing_question(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    for reply in (None, str(uuid.uuid4())):
        with pytest.raises(MessageError):
            send(root, cid, _envelope(cid, kind="answer", reply_to=reply))


def test_ack_replay_preserves_first_consumed_round(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    mid = send(root, cid, _envelope(cid))
    acknowledge(root, cid, mid, str(uuid.uuid4()))
    path = root / "coordinators" / cid / "acknowledgments" / f"{mid}.json"
    before = path.read_bytes()
    acknowledge(root, cid, mid, str(uuid.uuid4()))
    assert path.read_bytes() == before


def test_same_id_conflicting_concurrent_sends_have_one_winner(tmp_path, monkeypatch):
    root, cid = _root(tmp_path), _coord(tmp_path)
    env = _envelope(cid)
    start, second_write = threading.Barrier(2), threading.Event()
    guard, writes = threading.Lock(), []
    original = central_messages.write_atomic

    def delayed_write(path, payload):
        with guard:
            writes.append(path)
            if len(writes) == 2:
                second_write.set()
        second_write.wait(timeout=0.3)
        return original(path, payload)

    monkeypatch.setattr(central_messages, "write_atomic", delayed_write)

    def contender(body):
        start.wait(timeout=5)
        try:
            return send(root, cid, {**env, "body": body})
        except MessageError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(contender, "one"), pool.submit(contender, "two")
        winners = [value for value in (a.result(timeout=5), b.result(timeout=5)) if value]
    assert winners == [env["id"]]
    assert len(pending(root, cid, "in")) == 1


def test_id_replay_without_timestamp_is_stable(tmp_path):
    root, cid = _root(tmp_path), _coord(tmp_path)
    env = _envelope(cid)
    del env["created_at"]
    assert send(root, cid, env) == send(root, cid, env)
