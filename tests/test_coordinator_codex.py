"""Codex coordinator adapter: argv, events, usage and failures (T009).

Every CLI is a local stub installed by ``fake_bin``; no model is ever called.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from crew_chief.coordinator_codex import (
    CodexError,
    SessionMismatchError,
    headless,
    interactive,
    parse_events,
    probe,
)

BYPASS = "--dangerously-bypass-approvals-and-sandbox"

STUB = r'''
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_CODEX_LOG"]).open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "stdin": sys.stdin.read(), "cwd": os.getcwd()}) + "\n")

if os.environ.get("FAKE_CODEX_HANG") == "1":
    time.sleep(float(os.environ.get("FAKE_CODEX_HANG_S", "30")))

if args == ["--version"]:
    print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.50.0"))
    raise SystemExit(0)
if args == ["--help"]:
    print(os.environ.get(
        "FAKE_CODEX_TOP_HELP",
        "Commands:\n  exec    Run Codex non-interactively\n  resume  Resume a previous session",
    ))
    raise SystemExit(0)
if args[:2] == ["exec", "--help"]:
    print(os.environ.get(
        "FAKE_CODEX_EXEC_HELP",
        "--json  Print events as JSONL\n"
        "  resume  Resume a previous session\n"
        "  --dangerously-bypass-approvals-and-sandbox  Skip approvals",
    ))
    raise SystemExit(0)

for event in json.loads(os.environ.get("FAKE_CODEX_EVENTS", "[]")):
    print(json.dumps(event))
stderr = os.environ.get("FAKE_CODEX_STDERR", "")
if stderr:
    print(stderr, file=sys.stderr)
raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''


@pytest.fixture
def codex(tmp_path, fake_bin, monkeypatch):
    """Install the stub CLI and return helpers to script one round."""
    fake_bin("codex", STUB)
    log = tmp_path / "codex.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def script(*events, stderr="", exit_code=0):
        monkeypatch.setenv("FAKE_CODEX_EVENTS", json.dumps(list(events)))
        monkeypatch.setenv("FAKE_CODEX_STDERR", stderr)
        monkeypatch.setenv("FAKE_CODEX_EXIT", str(exit_code))

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    script()
    return type("Codex", (), {"script": script, "calls": calls, "log": log})


def record(tmp_path, **changes):
    value = {
        "cli": "codex",
        "worktree": str(tmp_path),
        "session_id": None,
        "authorization": {
            "settings": {
                "coordinator_unrestricted": True,
                "central_round_timeout_s": 5,
            }
        },
    }
    value.update(changes)
    return value


SESSION = {"type": "thread.started", "thread_id": "thread-1"}
ANSWER = {
    "type": "item.completed",
    "item": {"id": "item_1", "type": "agent_message", "text": "hello"},
}
USAGE = {
    "type": "turn.completed",
    "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30},
}
NORMALIZED = {
    "session_id",
    "text",
    "tokens_in",
    "tokens_out",
    "tokens_total",
    "estimated",
    "model",
    "quota_family",
    "error",
}


def test_first_round_runs_exec_json_with_the_prompt_on_stdin(tmp_path, codex):
    codex.script(SESSION, ANSWER, USAGE)
    prompt = "Do the round $(touch pwned)"

    lines = headless(record(tmp_path), prompt)

    call = codex.calls()[-1]
    assert call["argv"][-4:] == ["exec", "--json", BYPASS, "-"]
    assert call["stdin"] == prompt
    assert prompt not in call["argv"]
    assert Path(call["cwd"]).resolve() == tmp_path.resolve()
    assert not (tmp_path / "pwned").exists()
    assert any("thread.started" in line for line in lines)


def test_resume_round_targets_the_persisted_session(tmp_path, codex):
    codex.script(SESSION, ANSWER, USAGE)

    headless(record(tmp_path, session_id="thread-1"), "continue")

    call = codex.calls()[-1]
    assert call["argv"][-6:] == [
        "exec", "resume", "--json", BYPASS, "thread-1", "-",
    ]


def test_interactive_resume_uses_the_exact_session(tmp_path, codex):
    argv = interactive(record(tmp_path, session_id="thread-1"))

    assert "resume" in argv
    assert argv[-2:] == [BYPASS, "thread-1"]
    assert codex.calls() == []


def test_restricted_round_omits_bypass_flags(tmp_path, codex):
    codex.script(SESSION, ANSWER, USAGE)
    settings = {"coordinator_unrestricted": False, "central_round_timeout_s": 5}

    headless(record(tmp_path, authorization={"settings": settings}), "work")

    argv = codex.calls()[-1]["argv"]
    assert BYPASS not in argv
    assert not any("approval" in arg or "dangerously" in arg for arg in argv)


def test_restricted_round_times_out_instead_of_waiting_for_approval(
    tmp_path, codex, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_HANG", "1")
    monkeypatch.setenv("FAKE_CODEX_HANG_S", "30")
    settings = {"coordinator_unrestricted": False, "central_round_timeout_s": 1}

    started = time.monotonic()
    with pytest.raises(CodexError, match="timed out"):
        headless(record(tmp_path, authorization={"settings": settings}), "work")

    assert time.monotonic() - started < 10
    assert BYPASS not in codex.calls()[-1]["argv"]


def test_parse_events_normalizes_reported_usage_and_session():
    stdout = "\n".join(json.dumps(event) for event in (SESSION, ANSWER, USAGE))

    parsed = parse_events(stdout, "")

    assert set(parsed) == NORMALIZED
    assert parsed["session_id"] == "thread-1"
    assert parsed["text"] == "hello"
    assert (parsed["tokens_in"], parsed["tokens_out"], parsed["tokens_total"]) == (
        120, 30, 150,
    )
    assert parsed["estimated"] is False
    assert parsed["model"] is None
    assert parsed["quota_family"] is None
    assert parsed["error"] == ""


def test_missing_usage_is_marked_as_an_estimate():
    stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "x" * 400},
    })

    parsed = parse_events(stdout, "")

    assert parsed["estimated"] is True
    assert parsed["tokens_in"] is None
    assert parsed["tokens_out"] == 100
    assert parsed["tokens_total"] == 100


def test_unrecognized_output_is_a_visible_error():
    parsed = parse_events("plain progress noise\n", "backend said no")

    assert parsed["text"] == ""
    assert parsed["error"]


def test_quota_failure_is_attributed_to_the_codex_subscription():
    stdout = json.dumps({
        "type": "error",
        "message": "429 Too Many Requests: usage limit reached",
    })

    parsed = parse_events(stdout, "")

    assert parsed["quota_family"] == "sub:codex"
    assert "usage limit reached" in parsed["error"]


def test_session_mismatch_is_rejected_without_a_second_run(tmp_path, codex):
    codex.script(
        {"type": "thread.started", "thread_id": "someone-else"}, ANSWER, USAGE
    )

    with pytest.raises(SessionMismatchError) as caught:
        headless(record(tmp_path, session_id="thread-1"), "continue")

    assert "thread-1" in str(caught.value)
    assert "someone-else" in str(caught.value)
    assert len(codex.calls()) == 1


def test_nonzero_exit_retains_bounded_diagnostics(tmp_path, codex):
    codex.script(stderr="boom: " + "y" * 10000, exit_code=3)

    with pytest.raises(CodexError) as caught:
        headless(record(tmp_path), "work")

    error = caught.value
    assert error.returncode == 3
    assert "exit code 3" in str(error)
    assert "boom" in str(error)
    assert error.stderr.startswith("boom")
    assert len(error.stderr) < 3000
    assert isinstance(error.stdout, list)


def test_interactive_requires_a_persisted_session(tmp_path):
    with pytest.raises(CodexError, match="session"):
        interactive(record(tmp_path))


def test_headless_requires_a_worktree(codex, tmp_path):
    codex.script(SESSION, ANSWER, USAGE)

    with pytest.raises(CodexError, match="worktree"):
        headless(record(tmp_path, worktree=""), "work")


def test_probe_reports_the_installed_capabilities(codex):
    result = probe()

    assert result["available"] is True
    assert result["version"] == "codex-cli 0.50.0"
    assert result["supported"] is True
    assert result["missing"] == []
    assert result["capabilities"]["exec_resume"] is True


def test_probe_reports_a_missing_resume_capability(codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_HELP", "--json  Print events as JSONL")

    result = probe()

    assert result["supported"] is False
    assert "exec_resume" in result["missing"]


def test_probe_does_not_require_bypass_in_restricted_mode(codex, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CODEX_EXEC_HELP", "--json\n  resume  Resume a session")
    settings = {"coordinator_unrestricted": False}

    result = probe(record(tmp_path, authorization={"settings": settings}))

    assert result["supported"] is True
    assert result["capabilities"]["bypass"] is False


def test_probe_reports_a_missing_executable(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = probe()

    assert result["available"] is False
    assert result["supported"] is False
    assert "codex" in result["error"]
