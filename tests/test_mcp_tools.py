"""Tests for MCP tool handlers delegating to central CLI/supervisor/messages (T003)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def test_start_job_delegates_to_launch(tmp_path):
    """start_job must delegate to central_cli.handle_launch without reimplementing logic."""
    from crew_chief import mcp_tools

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    request_file = repo / "req.json"
    request_file.write_text("{}", encoding="utf-8")

    fake_args = MagicMock()
    with patch("crew_chief.mcp_tools._build_launch_args", return_value=fake_args) as mock_build, \
         patch("crew_chief.central_cli.handle_launch", return_value=0) as mock_launch:
        result = mcp_tools.start_job(
            repo=str(repo),
            request_file=str(request_file),
            mode="raw-request",
            authorize_raw=True,
        )

    mock_build.assert_called_once()
    mock_launch.assert_called_once_with(fake_args)
    assert result["ok"] is True
    assert result["exit_code"] == 0


def test_answer_delegates_to_message():
    """answer must delegate to central_cli.handle_message."""
    from crew_chief import mcp_tools

    fake_args = MagicMock()
    with patch("crew_chief.mcp_tools._build_message_args", return_value=fake_args) as mock_build, \
         patch("crew_chief.central_cli.handle_message", return_value=0) as mock_msg:
        result = mcp_tools.answer(coordinator_id="123e4567-e89b-12d3-a456-426614174000", text="hello")

    mock_build.assert_called_once()
    mock_msg.assert_called_once_with(fake_args)
    assert result["ok"] is True


def test_status_delegates_to_central_screen():
    """status must delegate to central_cli.handle_central (once/plain/json snapshot)."""
    from crew_chief import mcp_tools

    fake_args = MagicMock()
    with patch("crew_chief.mcp_tools._build_central_args", return_value=fake_args) as mock_build, \
         patch("crew_chief.central_cli.handle_central", return_value=0) as mock_central:
        result = mcp_tools.status()

    mock_build.assert_called_once()
    mock_central.assert_called_once_with(fake_args)
    assert result["ok"] is True


def test_events_delegates_to_supervisor_record():
    """events must read supervisor.json via central_supervisor helpers, not re-parse."""
    from crew_chief import mcp_tools

    with patch("crew_chief.mcp_tools._read_supervisor_events", return_value={"events": []}) as mock_read:
        result = mcp_tools.events(coordinator_id="123e4567-e89b-12d3-a456-426614174000")

    mock_read.assert_called_once()
    assert result["ok"] is True
    assert result["events"] == []


def test_questions_delegates_to_central_messages():
    """questions must delegate to central_messages.questions."""
    from crew_chief import mcp_tools

    with patch("crew_chief.central_messages.questions", return_value=[]) as mock_q:
        result = mcp_tools.questions()

    mock_q.assert_called_once()
    assert result["ok"] is True
    assert result["questions"] == []


def test_result_delegates_to_pending_outbox():
    """result must delegate to central_messages.pending for outbox results."""
    from crew_chief import mcp_tools

    with patch("crew_chief.central_messages.pending", return_value=[]) as mock_p:
        result = mcp_tools.result(coordinator_id="123e4567-e89b-12d3-a456-426614174000")

    mock_p.assert_called_once()
    assert result["ok"] is True


def test_result_returns_pr_url_summary_tests():
    """result must extract pr_url, summary, and tests from outbox rows (T010)."""
    from crew_chief import mcp_tools

    rows_with_fields = [
        {"pr_url": "https://github.com/example/repo/pull/42", "body": "done"},
        {"summary": "Implemented feature X", "tests": {"passed": 10, "failed": 2}},
    ]
    with patch("crew_chief.central_messages.pending", return_value=rows_with_fields) as mock_p:
        result = mcp_tools.result(coordinator_id="123e4567-e89b-12d3-a456-426614174000")

    mock_p.assert_called_once()
    assert result["ok"] is True
    assert result["pr_url"] == "https://github.com/example/repo/pull/42"
    assert result["summary"] == "Implemented feature X"
    assert result["tests"] == {"passed": 10, "failed": 2}
    # Backward compatibility: raw rows still present.
    assert result["results"] == rows_with_fields

    # When rows have no structured fields, values should be null.
    with patch("crew_chief.central_messages.pending", return_value=[{"body": "just text"}]):
        empty_result = mcp_tools.result(coordinator_id="123e4567-e89b-12d3-a456-426614174000")

    assert empty_result["ok"] is True
    assert empty_result["pr_url"] is None
    assert empty_result["summary"] is None
    assert empty_result["tests"] is None


def test_stop_delegates_to_control():
    """stop must delegate to central_cli.handle_control with action=stop."""
    from crew_chief import mcp_tools

    fake_args = MagicMock()
    with patch("crew_chief.mcp_tools._build_control_args", return_value=fake_args) as mock_build, \
         patch("crew_chief.central_cli.handle_control", return_value=0) as mock_ctrl:
        result = mcp_tools.stop(coordinator_id="123e4567-e89b-12d3-a456-426614174000")

    mock_build.assert_called_once()
    mock_ctrl.assert_called_once_with(fake_args)
    assert result["ok"] is True


def test_list_jobs_delegates_to_registry():
    """list_jobs must delegate to central_registry.load_registry, not re-scan disk."""
    from crew_chief import mcp_tools

    fake_registry = {"coordinators": {}, "errors": []}
    with patch("crew_chief.central_registry.load_registry", return_value=fake_registry) as mock_reg:
        result = mcp_tools.list_jobs()

    mock_reg.assert_called_once()
    assert result["ok"] is True
    assert isinstance(result["jobs"], list)


def test_result_and_questions_reject_outside_job_id(tmp_path):
    """result and questions MCP tools must reject absolute or traversal job_id."""
    import json
    from crew_chief import mcp_tools

    project = tmp_path / "project"
    (project / "docs" / "crew-chief" / "jobs").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "outside", "title": "secret", "summary": "outside summary"}))

    # Absolute job_id
    abs_job_id = str(outside.with_suffix(""))
    res_abs = mcp_tools.result(project=str(project), job_id=abs_job_id)
    assert res_abs["ok"] is False
    assert res_abs["isError"] is True

    q_abs = mcp_tools.questions(project=str(project), job_id=abs_job_id)
    assert q_abs["ok"] is False
    assert q_abs["isError"] is True

    # Relative traversal job_id
    res_rel = mcp_tools.result(project=str(project), job_id="../../../../outside")
    assert res_rel["ok"] is False
    assert res_rel["isError"] is True

    q_rel = mcp_tools.questions(project=str(project), job_id="../../../../outside")
    assert q_rel["ok"] is False
    assert q_rel["isError"] is True

    # Other MCP tools with invalid job_id
    stop_res = mcp_tools.stop(project=str(project), job_id=abs_job_id)
    assert stop_res["ok"] is False
    assert stop_res["isError"] is True

    status_res = mcp_tools.status(project=str(project), job_id=abs_job_id)
    assert status_res["ok"] is False
    assert status_res["isError"] is True

    events_res = mcp_tools.events(project=str(project), job_id=abs_job_id)
    assert events_res["ok"] is False
    assert events_res["isError"] is True

    ans_res = mcp_tools.answer(project=str(project), job_id=abs_job_id, key="coordinator", value="test")
    assert ans_res["ok"] is False
    assert ans_res["isError"] is True

