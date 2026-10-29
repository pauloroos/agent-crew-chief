"""Live round contract: closed ACC_ROUND prompt, strict parse, adapter survival (T036).

Pure, synthetic regression tests; no models or real configuration.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_central_rounds as base

from crew_chief import coordinator_claude as cc
from crew_chief.coordinator_protocol import (
    ProtocolError, _STATES, parse_round, render_round,
)

SESSION = "5f0d63d2-5c6f-4f4e-9c43-2f0f3f2a1b7c"


def base_result(**changes):
    value = dict(state="idle", active_job_id=None, job_ids=[],
                 blocked_task_refs=[], questions=[], answers=[],
                 consumed_message_ids=[])
    value.update(changes)
    return value


def wire(value):
    return "ACC_ROUND " + json.dumps(value, ensure_ascii=False)


def test_extra_summary_field_is_refused_naming_summary():
    value = base_result()
    value["summary"] = "one extra field"
    with pytest.raises(ProtocolError, match="unknown ACC_ROUND fields: summary"):
        parse_round(wire(value))


def test_unknown_fields_are_sorted_and_named():
    value = base_result()
    value["notes"] = "x"
    value["summary"] = "y"
    with pytest.raises(ProtocolError) as caught:
        parse_round(wire(value))
    assert "unknown ACC_ROUND fields: notes, summary" in str(caught.value)


def test_missing_field_is_named():
    value = base_result()
    del value["state"]
    with pytest.raises(ProtocolError, match="missing ACC_ROUND fields: state"):
        parse_round(wire(value))


def test_prompt_shows_closed_example_states_and_ban():
    prompt = render_round({"mode": "direct-spec"}, "request", [], {})
    assert "No other fields: no summary, no notes" in prompt
    assert f"Allowed states: {', '.join(sorted(_STATES))}." in prompt
    marker = "Example result: ACC_ROUND "
    assert marker in prompt
    example_json = prompt.split(marker, 1)[1].splitlines()[0]
    assert parse_round("ACC_ROUND " + example_json) == json.loads(example_json)


def test_claude_transcript_keeps_text_session_and_usage():
    answer = wire(base_result())
    stdout = "\n".join(json.dumps(event) for event in [
        {"type": "assistant", "session_id": SESSION,
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": answer}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "session_id": SESSION, "result": answer, "num_turns": 1,
         "usage": {"input_tokens": 10, "output_tokens": 4}},
        {"type": "system", "subtype": "ui_invalidate", "session_id": SESSION},
    ]) + "\n"

    result = cc.parse_events(stdout, "")

    assert result["text"] == answer
    assert result["session_id"] == SESSION
    assert result["tokens_in"] == 10
    assert result["tokens_out"] == 4
    assert result["tokens_total"] == 14
    assert result["estimated"] is False
    assert result["error"] is None


def test_failed_round_with_extra_summary_keeps_usage_and_does_not_redispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DIRECTIVE", str(tmp_path / "directive.json"))
    monkeypatch.setenv("FAKE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_SENTINEL", "sentinel")
    root, record, coord = base.prepared(tmp_path)
    pending = base.send_in(root, record)
    result = base.frame()
    payload = json.loads(result.removeprefix("ACC_ROUND "))
    payload["summary"] = "extra"
    text = "ACC_ROUND " + json.dumps(payload, ensure_ascii=False)
    adapter = base.FakeAdapter(base.events(text, session_id="sess-1"), ["fake-cli"])
    spawn = base.FakeSpawn(
        tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv
    )

    outcome = base.run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "unknown ACC_ROUND fields: summary" in outcome["error"]
    assert len(spawn.calls) == 1
    meta = base.round_file(coord, outcome["round_id"])
    assert meta["state"] == "failed"
    assert meta["usage"]["tokens_in"] == 11
    assert meta["usage"]["tokens_out"] == 7
    assert meta["usage"]["tokens_total"] == 18
    assert meta["acknowledged_message_ids"] == []
    assert not base.ack_file(coord, pending["id"]).exists()
    stored = base.central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored["state"] == "failed"


def _context(prompt: str) -> dict:
    marker = "CONTEXT_JSON\n"
    assert marker in prompt
    return json.loads(prompt.split(marker, 1)[1])


def test_prompt_states_answer_reply_to_is_stored_question_only():
    prompt = render_round({"mode": "direct-spec"}, "request", [], {})
    assert "answer.reply_to" in prompt
    assert "stored question" in prompt
    assert "never the incoming" in prompt
    assert "consumed_message_ids" in prompt
    assert "needs no outgoing" in prompt or "no outgoing answers" in prompt


def test_prompt_context_exposes_only_inbox_question_ids_as_answerable():
    import uuid as _uuid_mod

    question_id = str(_uuid_mod.uuid4())
    prior_question = str(_uuid_mod.uuid4())
    owner_answer_id = str(_uuid_mod.uuid4())
    inbox = [
        {"id": question_id, "kind": "question", "reply_to": None,
         "task_refs": ["T001"], "body": "owner asks which task?"},
        {"id": owner_answer_id, "kind": "answer", "reply_to": prior_question,
         "body": "approved"},
    ]
    prompt = render_round({"mode": "direct-spec"}, "request", inbox, {})
    context = _context(prompt)
    assert context["answerable_question_ids"] == [question_id]


def test_wrong_owner_answer_correlation_fails_delivery_but_recovery_preserves_work(
    tmp_path, monkeypatch,
):
    import uuid as _uuid_mod

    monkeypatch.setenv("FAKE_DIRECTIVE", str(tmp_path / "directive.json"))
    monkeypatch.setenv("FAKE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_SENTINEL", "sentinel")
    root, record, coord = base.prepared(tmp_path)
    # Coordinator previously asked Q_out; owner replied A_in; owner also asked Q_in.
    q_out = base.send_in(
        root, record, direction="out", kind="question", reply_to=None,
        task_refs=["T001"], body="confirm scope",
    )
    a_in = base.send_in(
        root, record, direction="in", kind="answer",
        reply_to=q_out["id"], body="approved",
    )
    q_in = base.send_in(
        root, record, direction="in", kind="question",
        task_refs=["T001"], body="which task first?",
    )
    # Buggy coordinator behavior from the real lab run: reply_to points
    # at the owner-answer message id instead of a stored question.
    bad_answer_id = str(_uuid_mod.uuid4())
    bad = base.frame(
        state="idle", job_ids=["job-1"], active_job_id="job-1",
        consumed_message_ids=[a_in["id"]],
        answers=[{"id": bad_answer_id, "reply_to": a_in["id"], "body": "ack"}],
    )
    adapter = base.FakeAdapter(base.events(bad, session_id="sess-1"), ["fake-cli"])
    spawn = base.FakeSpawn(
        tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv
    )
    outcome = base.run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is False
    assert "stored question" in outcome["error"]
    assert len(spawn.calls) == 1
    meta = base.round_file(coord, outcome["round_id"])
    assert meta["state"] == "failed"
    assert meta["usage"]["tokens_in"] == 11
    assert meta["usage"]["tokens_out"] == 7
    assert meta["acknowledged_message_ids"] == []
    assert not base.ack_file(coord, a_in["id"]).exists()
    # The prompt must already steer the next round away from the same mistake.
    assert "stored question" in adapter.prompts[0]
    assert "never the incoming" in adapter.prompts[0]
    context = _context(adapter.prompts[0])
    assert q_in["id"] in context["answerable_question_ids"]
    assert a_in["id"] not in context["answerable_question_ids"]

    # Recovery: consume the owner reply, answer only the open owner question.
    record = base.central_registry.load_registry(root)["coordinators"][record["id"]]
    good_answer_id = str(_uuid_mod.uuid4())
    good = base.frame(
        state="idle", job_ids=["job-1"], active_job_id="job-1",
        consumed_message_ids=[a_in["id"]],
        answers=[{"id": good_answer_id, "reply_to": q_in["id"], "body": "T001"}],
    )
    adapter2 = base.FakeAdapter(base.events(good, session_id="sess-1"), ["fake-cli"])
    spawn2 = base.FakeSpawn(
        tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter2.argv
    )
    outcome2 = base.run_round(record, root, adapter2, spawn2, timeout_s=30)

    assert outcome2["ok"] is True, outcome2["error"]
    assert len(spawn2.calls) == 1
    assert base.ack_file(coord, a_in["id"]).exists()
    meta2 = base.round_file(coord, outcome2["round_id"])
    assert meta2["job_ids"] == ["job-1"]
    assert meta2["active_job_id"] == "job-1"


def test_owner_answer_consumed_without_outgoing_answer_preserves_work(
    tmp_path, monkeypatch,
):
    from crew_chief import central_messages

    monkeypatch.setenv("FAKE_DIRECTIVE", str(tmp_path / "directive.json"))
    monkeypatch.setenv("FAKE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_SENTINEL", "sentinel")
    root, record, coord = base.prepared(
        tmp_path, job_ids=["job-1"], active_job_id="job-1",
    )
    # Stored outbound question, then a real owner reply via owner_message.
    q_out = base.send_in(
        root, record, direction="out", kind="question", reply_to=None,
        task_refs=["T001"], body="confirm scope",
    )
    owner_id = central_messages.send(
        root, record["id"],
        central_messages.owner_message(record["id"], "approved", reply_to=q_out["id"]),
    )
    stored = central_messages.pending(root, record["id"], "in")
    assert [item["id"] for item in stored] == [owner_id]
    assert stored[0]["kind"] == "answer"

    # Correct coordinator behavior: no outgoing answers entry, only consumption.
    good = base.frame(
        state="completed", job_ids=["job-1"], active_job_id="job-1",
        consumed_message_ids=[owner_id], answers=[],
    )
    adapter = base.FakeAdapter(base.events(good, session_id="sess-1"), ["fake-cli"])
    spawn = base.FakeSpawn(
        tmp_path, {"stdout": "{}"}, Path(record["worktree"]), adapter.argv
    )
    outcome = base.run_round(record, root, adapter, spawn, timeout_s=30)

    assert outcome["ok"] is True, outcome["error"]
    assert len(spawn.calls) == 1
    context = _context(adapter.prompts[0])
    assert context["answerable_question_ids"] == []
    assert "answers must be []" in adapter.prompts[0]
    assert base.ack_file(coord, owner_id).exists()
    meta = base.round_file(coord, outcome["round_id"])
    assert meta["state"] == "completed"
    assert meta["job_ids"] == ["job-1"]
    assert meta["active_job_id"] == "job-1"
    assert meta["checkpoint"]["job_ids"] == ["job-1"]
    assert meta["acknowledged_message_ids"] == [owner_id]
    assert meta["usage"]["tokens_total"] == 18
    assert central_messages.pending(root, record["id"], "in") == []
    outbox_answers = [
        item for item in central_messages.pending(root, record["id"], "out")
        if item["kind"] == "answer"
    ]
    assert outbox_answers == []
    stored_record = base.central_registry.load_registry(root)["coordinators"][record["id"]]
    assert stored_record["state"] == "completed"
    assert stored_record["job_ids"] == ["job-1"]
