"""Codex coordinator adapter: argv, events, usage and failures (T009).

Every CLI is a local stub installed by ``fake_bin``; no model is ever called.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from crew_chief.coordinator_codex import (
    CodexError,
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
        "Commands:\n  exec    Run Codex non-interactively\n  resume  Resume a previous session\n"
        "--ask-for-approval never\n--sandbox read-only workspace-write",
    ))
    raise SystemExit(0)
if args[:2] == ["exec", "--help"]:
    print(os.environ.get(
        "FAKE_CODEX_EXEC_HELP",
        "--json  Print events as JSONL\n"
        "  resume  Resume a previous session\n"
        "  --dangerously-bypass-approvals-and-sandbox  Skip approvals\n"
        "  --sandbox  Select sandbox policy\n"
        "  workspace-write  Allow workspace writes\n"
        "  read-only  Read only",
    ))
    raise SystemExit(0)
if args[:3] == ["exec", "resume", "--help"]:
    print(os.environ.get("FAKE_CODEX_EXEC_RESUME_HELP", "--json --dangerously-bypass-approvals-and-sandbox"))
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


def test_first_round_returns_exec_json_argv_with_prompt_on_stdin(tmp_path, codex):
    argv = headless(record(tmp_path), "Do the round $(touch pwned)")

    assert argv[-4:] == ["exec", "--json", BYPASS, "-"]
    assert "Do the round $(touch pwned)" not in argv
    assert not (tmp_path / "pwned").exists()


def test_resume_round_targets_the_persisted_session(tmp_path, codex):
    argv = headless(record(tmp_path, session_id="thread-1"), "continue")

    assert argv[-6:] == ["exec", "resume", "--json", BYPASS, "thread-1", "-"]


def test_interactive_resume_uses_the_exact_session(tmp_path, codex):
    argv = interactive(record(tmp_path, session_id="thread-1"))

    assert "resume" in argv
    assert argv[-1] == "thread-1"
    assert BYPASS in argv


def test_restricted_round_omits_bypass_flags(tmp_path, codex):
    settings = {"coordinator_unrestricted": False, "central_round_timeout_s": 5}

    argv = headless(record(tmp_path, authorization={"settings": settings}), "work")

    assert BYPASS not in argv
    assert "--sandbox" in argv
    assert not any("approval" in arg or "dangerously" in arg for arg in argv)


def test_restricted_interactive_uses_no_prompt_policy(tmp_path, codex):
    settings = {"coordinator_unrestricted": False}
    argv = interactive(record(tmp_path, session_id="thread-1", authorization={"settings": settings}))
    assert BYPASS not in argv
    assert "-a" in argv or "--sandbox" in argv


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


def test_conflicting_sessions_are_an_error():
    stdout = "\n".join(json.dumps(event) for event in (
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "thread.started", "thread_id": "thread-2"},
    ))
    parsed = parse_events(stdout, "")
    assert "conflicting" in parsed["error"]


def test_numeric_usage_rejects_invalid_values():
    stdout = json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": True,
            "output_tokens": float("nan"),
            "total_tokens": -5,
        },
    })
    parsed = parse_events(stdout, "")
    # All invalid reported usage falls back to an estimate only if text exists.
    assert parsed["tokens_in"] is None
    assert parsed["tokens_out"] is None or parsed["estimated"] is True


def test_usage_zero_is_not_invented_estimate():
    stdout = json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    })
    parsed = parse_events(stdout, "")
    assert parsed["estimated"] is False
    assert parsed["tokens_total"] == 0


def test_bom_crlf_event_stream_keeps_text_and_measured_zero_usage():
    events = (ANSWER, SESSION, {
        "type": "turn.completed",
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    })
    stdout = "\ufeff" + "\r\n".join(json.dumps(event) for event in events) + "\r\n"

    parsed = parse_events(stdout, "")

    assert parsed["text"] == ANSWER["item"]["text"]
    assert parsed["session_id"] == "thread-1"
    assert parsed["estimated"] is False
    assert (parsed["tokens_in"], parsed["tokens_out"], parsed["tokens_total"]) == (0, 0, 0)


def test_interactive_requires_a_persisted_session(tmp_path):
    with pytest.raises(CodexError, match="session"):
        interactive(record(tmp_path))


def test_headless_requires_a_worktree(codex, tmp_path):
    with pytest.raises(CodexError, match="worktree"):
        headless(record(tmp_path, worktree=""), "work")


def test_headless_requires_a_prompt(codex, tmp_path):
    with pytest.raises(CodexError, match="prompt"):
        headless(record(tmp_path), "")


def test_headless_validates_session_id(tmp_path):
    with pytest.raises(CodexError, match="session"):
        headless(record(tmp_path, session_id="--evil"), "work")


def test_interactive_validates_session_id(tmp_path):
    with pytest.raises(CodexError, match="session"):
        interactive(record(tmp_path, session_id="--evil"))


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
    monkeypatch.setenv(
        "FAKE_CODEX_EXEC_HELP",
        "--json\n  resume  Resume a session\n  --sandbox  Select sandbox",
    )
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


def test_headless_capability_error_when_probe_missing(tmp_path, codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_HELP", "no json here")
    with pytest.raises(CodexError, match="capabilities"):
        headless(record(tmp_path), "work")


def test_interactive_capability_error_when_probe_missing(tmp_path, codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_HELP", "no json here")
    with pytest.raises(CodexError, match="capabilities"):
        interactive(record(tmp_path, session_id="thread-1"))


def test_restricted_headless_explicitly_denies_approvals(tmp_path, codex):
    argv = headless(record(tmp_path, authorization={"settings": {
        "coordinator_unrestricted": False}}), "synthetic")
    assert argv[argv.index("-a") + 1] == "never"
    assert argv.index("-a") < argv.index("exec")


def test_restricted_missing_no_prompt_capability_refuses(tmp_path, codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_TOP_HELP", "resume\n--sandbox read-only")
    with pytest.raises(CodexError, match="capabilit"):
        headless(record(tmp_path, authorization={"settings": {
            "coordinator_unrestricted": False}}), "synthetic")


def test_resume_requires_its_own_json_capability(tmp_path, codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXEC_RESUME_HELP", "resume without structured output")
    with pytest.raises(CodexError, match="capabilit"):
        headless(record(tmp_path, session_id="thread-1"), "synthetic")


def test_huge_integer_usage_is_exact_and_bounded_input_is_visible():
    huge = 10 ** 400
    parsed = parse_events(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": huge, "output_tokens": 2}}), "")
    assert parsed["tokens_total"] == huge + 2
    assert parsed["estimated"] is False
    assert parse_events("x" * (11 * 1024 * 1024), "")["error"]


def test_fake_shim_execution_preserves_stdin_and_exact_session(tmp_path, codex, monkeypatch):
    from crew_chief import coordinator_codex as adapter
    # An actual fake executable/shim in a path containing spaces and '&'.
    found = Path(adapter._executable())
    special = tmp_path / "fake cli & tools"
    shutil.copytree(found.parent, special)
    monkeypatch.setenv("PATH", str(special) + os.pathsep + os.environ.get("PATH", ""))
    prompt = 'synthetic & echo BAD | %PATH% ^ "quoted" $(unused)'
    argv = headless(record(tmp_path, session_id="thread-1"), prompt)
    result = subprocess.run(argv, input=prompt, cwd=tmp_path, shell=False,
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    call = codex.calls()[-1]
    assert call["stdin"] == prompt
    assert call["argv"][-2:] == ["thread-1", "-"]
    assert call["cwd"] == str(tmp_path)
