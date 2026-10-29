"""Tests for acc mcp stdio transport (JSON-RPC 2.0)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skill" / "crew-chief" / "scripts"
ENTRY_SCRIPT = SCRIPTS_DIR / "crew-chief.py"


def _spawn_mcp_process(extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONPATH": str(SCRIPTS_DIR)
        + (os.pathsep + os.environ["PYTHONPATH"] if "PYTHONPATH" in os.environ else ""),
    }
    if extra_env:
        env.update(extra_env)
    for k in ("ORCA_CLI_COMMAND", "ORCA_WORKTREE_ID", "ORCA_WORKSPACE_ID"):
        env.pop(k, None)
    return subprocess.Popen(
        [sys.executable, str(ENTRY_SCRIPT), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_stdio_jsonrpc_handshake():
    """Verify JSON-RPC 2.0 initialize/initialized exchange completes over stdin/stdout."""
    proc = _spawn_mcp_process()
    try:
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0",
                },
            },
        }

        assert proc.stdin is not None
        proc.stdin.write(json.dumps(init_request) + "\n")
        proc.stdin.flush()

        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, f"Server closed stdout without response. stderr: {proc.stderr.read() if proc.stderr else ''}"

        response = json.loads(line)
        assert response.get("jsonrpc") == "2.0"
        assert response.get("id") == 1
        assert "result" in response
        result = response["result"]
        assert result.get("protocolVersion") == "2024-11-05"
        assert "capabilities" in result
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "agent-crew-chief"

        # Send notifications/initialized
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        # Close stdin to trigger clean EOF shutdown
        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stdio_malformed_json_error():
    """Server responds with JSON-RPC Parse error (-32700) on malformed input."""
    proc = _spawn_mcp_process()
    try:
        assert proc.stdin is not None
        proc.stdin.write("this is not valid json\n")
        proc.stdin.flush()

        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line
        response = json.loads(line)
        assert response.get("jsonrpc") == "2.0"
        assert response.get("id") is None
        assert "error" in response
        assert response["error"]["code"] == -32700

        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stdio_invalid_request_and_method_not_found():
    """Server handles invalid requests and unknown methods per JSON-RPC 2.0."""
    proc = _spawn_mcp_process()
    try:
        assert proc.stdin is not None
        # Invalid request (missing jsonrpc)
        proc.stdin.write(json.dumps({"id": 10, "method": "test"}) + "\n")
        proc.stdin.flush()

        assert proc.stdout is not None
        line = proc.stdout.readline()
        resp1 = json.loads(line)
        assert resp1.get("id") == 10
        assert resp1.get("error", {}).get("code") == -32600

        # Method not found
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 11, "method": "unknown_tool"}) + "\n"
        )
        proc.stdin.flush()

        line2 = proc.stdout.readline()
        resp2 = json.loads(line2)
        assert resp2.get("id") == 11
        assert resp2.get("error", {}).get("code") == -32601

        # Ping
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 12, "method": "ping"}) + "\n"
        )
        proc.stdin.flush()

        line3 = proc.stdout.readline()
        resp3 = json.loads(line3)
        assert resp3.get("id") == 12
        assert resp3.get("result") == {}

        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_mcp_server_in_process_streams():
    """Unit test MCPServer and serve_stdio directly with in-memory streams."""
    from crew_chief.mcp_server import MCPServer, serve_stdio

    server = MCPServer()
    input_data = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "abc-1",
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
    )
    stdin = io.StringIO(input_data)
    stdout = io.StringIO()

    exit_code = serve_stdio(server, stdin=stdin, stdout=stdout)
    assert exit_code == 0

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    resp = json.loads(lines[0])
    assert resp["id"] == "abc-1"
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_stdio_tools_list_and_call_lifecycle(tmp_path):
    """Verify tools/list and tools/call over stdio for interview lifecycle (SC-001, FR-004, FR-036)."""
    repo = tmp_path / "stdio-repo"
    repo.mkdir()
    (repo / "docs" / "crew-chief").mkdir(parents=True)
    (repo / "docs" / "crew-chief" / "preferences.toml").write_text("", encoding="utf-8")

    proc = _spawn_mcp_process()
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        # 1. Initialize
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            + "\n"
        )
        proc.stdin.flush()
        init_resp = json.loads(proc.stdout.readline())
        assert init_resp.get("id") == 1
        assert "capabilities" in init_resp["result"]

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        # 2. tools/list
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            + "\n"
        )
        proc.stdin.flush()
        tools_resp = json.loads(proc.stdout.readline())
        assert tools_resp.get("id") == 2
        tools = tools_resp.get("result", {}).get("tools", [])
        tool_names = {t["name"] for t in tools}
        expected_tools = {
            "start_job",
            "questions",
            "answer",
            "status",
            "events",
            "result",
            "stop",
            "list_jobs",
        }
        assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"

        # 3. tools/call: start_job
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "start_job",
                    "arguments": {
                        "project": str(repo),
                        "request": "Implement stdio test feature",
                    },
                },
            })
            + "\n"
        )
        proc.stdin.flush()
        start_resp = json.loads(proc.stdout.readline())
        assert start_resp.get("id") == 3
        res = start_resp.get("result", {})
        assert res.get("ok") is True
        job_id = res.get("job_id")
        assert job_id
        assert res.get("state") == "interview"
        pending = res.get("pending", [])
        assert "request" not in pending
        assert "project" not in pending
        assert "coordinator" in pending

        # 4. tools/call: answer with invalid value
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "answer",
                    "arguments": {
                        "job_id": job_id,
                        "project": str(repo),
                        "key": "executors",
                        "value": "invalid-mode",
                    },
                },
            })
            + "\n"
        )
        proc.stdin.flush()
        ans_err = json.loads(proc.stdout.readline())
        assert ans_err.get("id") == 4
        err_res = ans_err.get("result", {})
        assert err_res.get("isError") is True or err_res.get("ok") is False

        # 5. Answer remaining questions: coordinator, executors, end_mode
        for q_key, q_val in [
            ("coordinator", "claude-sonnet"),
            ("executors", "own"),
            ("end_mode", "pr"),
        ]:
            proc.stdin.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "answer",
                        "arguments": {
                            "job_id": job_id,
                            "project": str(repo),
                            "key": q_key,
                            "value": q_val,
                        },
                    },
                })
                + "\n"
            )
            proc.stdin.flush()
            ans_ok = json.loads(proc.stdout.readline())
            assert ans_ok.get("id") == 5

        # 6. tools/call: status -> state is now queued, running, or failed (never ready)
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "status",
                    "arguments": {"job_id": job_id, "project": str(repo)},
                },
            })
            + "\n"
        )
        proc.stdin.flush()
        status_resp = json.loads(proc.stdout.readline())
        assert status_resp.get("id") == 6
        st_res = status_resp.get("result", {})
        assert st_res.get("state") in ("queued", "running", "failed")
        assert st_res.get("state") != "ready"
        assert st_res.get("pending") == []

        # 7. tools/call: events cursor replay
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "events",
                    "arguments": {"job_id": job_id, "project": str(repo), "cursor": 0},
                },
            })
            + "\n"
        )
        proc.stdin.flush()
        ev_resp = json.loads(proc.stdout.readline())
        assert ev_resp.get("id") == 7
        ev_res = ev_resp.get("result", {})
        assert ev_res.get("ok") is True
        events_list = ev_res.get("events", [])
        assert len(events_list) >= 4  # started + 3 answers

        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_handler_stdout_never_corrupts_stdio_stream(tmp_path: Path):
    """Verify tool handlers printing to sys.stdout in stdio mode never corrupt the JSON-RPC stream (T048, FR-004)."""
    import uuid
    from crew_chief import central_registry, records

    repo = tmp_path / "repo"
    repo.mkdir()
    docs_dir = repo / "docs" / "crew-chief"
    docs_dir.mkdir(parents=True)
    (docs_dir / "preferences.toml").write_text("", encoding="utf-8")

    central_dir = tmp_path / "central"
    central_dir.mkdir(parents=True)
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True)

    cid = str(uuid.uuid4())
    record = {
        "schema_version": 1,
        "id": cid,
        "repo": str(repo),
        "worktree": str(repo),
        "branch": "main",
        "base_commit": "0" * 40,
        "backend": "default",
        "cli": "claude",
        "cli_version": "",
        "acc_version": "",
        "session_id": None,
        "mode": "raw-request",
        "authorization": {
            "mode": "raw-request",
            "approved": True,
            "authorize_raw": True,
            "settings": {},
        },
        "job_ids": ["job-corrupt-test"],
        "active_job_id": "job-corrupt-test",
        "state": "running",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "last_error": None,
        "supervisor_nonce": None,
    }
    central_registry.register(central_dir, record)

    job_rec = {
        "id": "job-corrupt-test",
        "job_id": "job-corrupt-test",
        "project": str(repo),
        "state": "running",
        "coordinator_id": cid,
        "central_dir": str(central_dir),
        "request": "Test request for stdio isolation",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    records.save_job(repo, job_rec)

    extra_env = {
        "AGENT_CREW_CHIEF_CENTRAL_DIR": str(central_dir),
        "HOME": str(home_dir),
    }
    proc = _spawn_mcp_process(extra_env=extra_env)
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        # 1. Initialize handshake
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            })
            + "\n"
        )
        proc.stdin.flush()
        line1 = proc.stdout.readline()
        resp1 = json.loads(line1)
        assert resp1.get("jsonrpc") == "2.0"
        assert resp1.get("id") == 1

        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        # 2. Call stop for the job; this delegates to central_cli.handle_control which prints to stdout
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "stop",
                    "arguments": {
                        "job_id": "job-corrupt-test",
                        "project": str(repo),
                    },
                },
            })
            + "\n"
        )
        proc.stdin.flush()

        # The next line on stdout MUST be exactly the JSON-RPC response message with id=2
        line2 = proc.stdout.readline()
        resp2 = json.loads(line2)
        assert resp2.get("jsonrpc") == "2.0"
        assert resp2.get("id") == 2
        assert "result" in resp2
        assert resp2["result"].get("ok") is True

        # 3. Call status without job_id (central snapshot); this delegates to central_cli.handle_central which renders to stdout
        proc.stdin.write(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "status",
                    "arguments": {
                        "central_dir": str(central_dir),
                    },
                },
            })
            + "\n"
        )
        proc.stdin.flush()

        line3 = proc.stdout.readline()
        resp3 = json.loads(line3)
        assert resp3.get("jsonrpc") == "2.0"
        assert resp3.get("id") == 3
        assert "result" in resp3

        proc.stdin.close()
        proc.wait(timeout=5)
        assert proc.returncode == 0

        # No remaining corrupting bytes on stdout
        remaining_stdout = proc.stdout.read()
        assert remaining_stdout == "", f"Unexpected remaining stdout: {remaining_stdout!r}"

        # Stderr captured the redirected handler prints
        stderr_content = proc.stderr.read() if proc.stderr else ""
        assert "stop" in stderr_content or "control_id" in stderr_content
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

