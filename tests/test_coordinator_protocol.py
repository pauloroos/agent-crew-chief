"""Pure, synthetic round contracts; no models or real configuration."""
import copy
import json
from uuid import uuid4

import pytest

from crew_chief.coordinator_protocol import (
    MAX_PROMPT_BYTES, MAX_RESULT_BYTES, ProtocolError,
    parse_round, ready_tasks, render_round,
)


def result(**changes):
    value = dict(state="idle", active_job_id="20261021-0000-demo",
                 job_ids=["20261021-0000-demo"], blocked_task_refs=[],
                 questions=[], answers=[], consumed_message_ids=[])
    value.update(changes)
    return value


def wire(value):
    return "ACC_ROUND\n" + json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("mode,phrase", [
    ("approved-spec", "approval digest"),
    ("direct-spec", "selected spec"),
    ("raw-request", "raw authorization"),
])
def test_mode_prompt_contains_bounded_work_and_existing_cycle(mode, phrase):
    record = dict(mode=mode, worktree="/synthetic/worktree", job_ids=[],
                  authorization={"approved": True, "authorize_raw": True}, secret="DO_NOT_FORWARD")
    prompt = render_round(record, "Implement the selected task", [], {})
    assert phrase in prompt
    for command in ("job new", "spec import", "brief render", "pick", "dispatch", "review", "grade", "accept", "close"):
        assert command in prompt
    assert "DO_NOT_FORWARD" not in prompt
    assert "never merge or push" in prompt
    assert "consumed_message_ids" in prompt
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES


def test_prompt_preserves_correlations_and_control_without_mutating_inputs():
    message = dict(id=str(uuid4()), kind="answer", reply_to=str(uuid4()),
                   task_refs=["T001"], body="Use the approved choice")
    record = dict(mode="approved-spec", authorization={"approved": True})
    checkpoint = {"control_state": "pausing", "summary": "one task remains"}
    before = copy.deepcopy((record, message, checkpoint))
    prompt = render_round(record, "request", [message], checkpoint)
    assert message["id"] in prompt and message["reply_to"] in prompt
    assert "pausing" in prompt
    assert (record, message, checkpoint) == before


@pytest.mark.parametrize("args", [
    ({"mode": "unknown"}, "request", [], {}),
    ({"mode": "raw-request"}, "request", [], {}),
    ({"mode": "approved-spec"}, "request", [], {}),
    ({"mode": "direct-spec"}, "é" * MAX_PROMPT_BYTES, [], {}),
    ({"mode": "direct-spec"}, "request", [{}] * 65, {}),
    ({"mode": "direct-spec"}, "request", [], {"summary": "x" * MAX_PROMPT_BYTES}),
])
def test_invalid_or_oversized_prompt_fails_visibly(args):
    with pytest.raises(ProtocolError):
        render_round(*args)


def test_round_result_preserves_question_answer_and_consumption_ids():
    question = dict(id=str(uuid4()), task_refs=["T001"], reason="Choose storage")
    answer = dict(id=str(uuid4()), reply_to=str(uuid4()), body="Use the approved option")
    value = result(state="waiting", blocked_task_refs=["T001"],
                   questions=[question], answers=[answer], consumed_message_ids=[str(uuid4())])
    assert parse_round(wire(value)) == value
    assert parse_round("\ufeff" + wire(value).replace("\n", "\r\n")) == value


@pytest.mark.parametrize("state", ["created", "starting"])
def test_empty_first_round_result_parses(state):
    value = result(state=state, active_job_id=None, job_ids=[])
    assert parse_round(wire(value)) == value


@pytest.mark.parametrize("key", list(result()))
def test_every_result_field_is_required(key):
    value = result()
    del value[key]
    with pytest.raises(ProtocolError):
        parse_round(wire(value))


@pytest.mark.parametrize("changes", [
    {"state": "magic"}, {"active_job_id": "../escape"},
    {"active_job_id": "another-job"}, {"job_ids": "not-a-list"},
    {"job_ids": ["../escape"]}, {"blocked_task_refs": ["../T001"]},
    {"questions": [{"id": "bad", "task_refs": ["T001"], "reason": "why"}]},
    {"questions": [{"id": str(uuid4()), "task_refs": [], "reason": "why"}]},
    {"answers": [{"id": str(uuid4()), "body": "missing correlation"}]},
    {"answers": [{"id": str(uuid4()), "reply_to": "bad", "body": "text"}]},
    {"consumed_message_ids": ["bad"]}, {"consumed_message_ids": [True]},
    {"unexpected": "field"},
])
def test_invalid_result_fields_are_rejected(changes):
    with pytest.raises(ProtocolError):
        parse_round(wire(result(**changes)))


def wire_with_constant(field, constant):
    value = result()
    original = json.dumps(value, ensure_ascii=False)
    serialized = f'"{field}": {json.dumps(value[field], ensure_ascii=False)}'
    assert serialized in original
    return "ACC_ROUND\n" + original.replace(serialized, f'"{field}": {constant}')


@pytest.mark.parametrize("field,constant", [
    ("state", "NaN"), ("active_job_id", "Infinity"), ("job_ids", "-Infinity"),
    ("blocked_task_refs", "NaN"), ("questions", "Infinity"),
    ("answers", "-Infinity"), ("consumed_message_ids", "NaN"),
])
def test_non_finite_constants_are_rejected_in_any_result_field(field, constant):
    with pytest.raises(ProtocolError, match="non-finite JSON constant"):
        parse_round(wire_with_constant(field, constant))


def test_nested_and_top_level_non_finite_constants_are_rejected():
    text = json.dumps(result(), ensure_ascii=False)
    nested = f'"job_ids": {json.dumps(result()["job_ids"])}'
    with pytest.raises(ProtocolError, match="non-finite JSON constant"):
        parse_round("ACC_ROUND\n" + text.replace(nested, '"job_ids": [Infinity]'))
    for constant in ("NaN", "-Infinity"):
        with pytest.raises(ProtocolError, match="non-finite JSON constant"):
            parse_round("ACC_ROUND\n" + constant)


def test_duplicate_effect_ids_and_duplicate_json_keys_are_rejected():
    identifier = str(uuid4())
    with pytest.raises(ProtocolError):
        parse_round(wire(result(consumed_message_ids=[identifier, identifier])))
    answer = dict(id=str(uuid4()), reply_to=str(uuid4()), body="one answer")
    with pytest.raises(ProtocolError):
        parse_round(wire(result(answers=[answer, {**answer, "id": str(uuid4())}])))
    with pytest.raises(ProtocolError):
        parse_round(wire(result())[:-1] + ', "state": "completed"}')


@pytest.mark.parametrize("wrap", [
    lambda text: "Tool output:\n" + text,
    lambda text: json.dumps({"role": "tool", "content": text}),
    lambda text: "```text\n" + text + "\n```",
    lambda text: text + "\nACC_ROUND\n{}",
])
def test_tool_transcripts_and_embedded_markers_cannot_be_results(wrap):
    with pytest.raises(ProtocolError):
        parse_round(wrap(wire(result())))


def test_oversized_result_is_rejected_before_json_decode():
    with pytest.raises(ProtocolError):
        parse_round("ACC_ROUND\n" + "x" * MAX_RESULT_BYTES)


def test_ready_tasks_block_only_question_and_dependent_work():
    tasks = [dict(ref="T001", depends=[]), dict(ref="T002", depends=["T001"]),
             dict(ref="T003", depends=[]), dict(ref="T004", depends=["T002"]),
             dict(ref="T005", depends=[], accepted=True),
             dict(ref="T006", depends=["T005"])]
    original = copy.deepcopy(tasks)
    assert ready_tasks(tasks, ["T001"]) == ["T003", "T006"]
    assert ready_tasks(tasks, []) == ["T001", "T003", "T006"]
    assert tasks == original


def test_prompt_names_ready_independent_work_before_waiting():
    checkpoint = {"tasks": [dict(ref="T001", depends=[]), dict(ref="T002", depends=[])],
                  "blocked_task_refs": ["T001"]}
    prompt = render_round({"mode": "direct-spec"}, "request", [], checkpoint)
    assert '"ready_task_refs": ["T002"]' in prompt
    assert "waiting only when no independent task is ready" in prompt


def test_ready_tasks_does_not_run_unknown_dependencies_or_cycles():
    tasks = [dict(ref="T001", depends=["T002"]), dict(ref="T002", depends=["T001"]),
             dict(ref="T003", depends=["T999"])]
    assert ready_tasks(tasks, []) == []
