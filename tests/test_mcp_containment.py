"""MCP containment: request_file, legacy starts, and unknown args (P1-1).

Uses real temporary directories only; no mocks of the containment logic.
"""
from __future__ import annotations

from pathlib import Path


def _make_project(parent: Path, name: str = "project") -> Path:
    project = parent / name
    project.mkdir(parents=True, exist_ok=True)
    prefs_dir = project / "docs" / "crew-chief"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "preferences.toml").write_text("", encoding="utf-8")
    return project


def test_mcp_request_file_outside_project_rejected(tmp_path):
    """A request_file outside the allowed project must be rejected, not exfiltrated."""
    from crew_chief import mcp_tools

    allowed = tmp_path / "allowed"
    project = _make_project(allowed)
    outside = tmp_path / "outside-secret.txt"
    secret = "SYNTHETIC_PRIVATE_OUTSIDE_ALLOWED_PROJECTS"
    outside.write_text(secret, encoding="utf-8")

    res = mcp_tools.start_job(
        project=str(project),
        request_file=str(outside),
        projects_dir=str(allowed),
    )
    assert res.get("ok") is False, f"expected rejection, got: {res!r}"
    assert secret not in str(res), "secret file content must never be embedded in the result"


def test_mcp_request_file_inside_project_allowed(tmp_path):
    """A request_file inside the project remains allowed (inline text stays allowed)."""
    from crew_chief import mcp_tools

    allowed = tmp_path / "allowed"
    project = _make_project(allowed)
    inside = project / "request.md"
    inside.write_text("Implement a documented feature.", encoding="utf-8")

    res = mcp_tools.start_job(
        project=str(project),
        request_file=str(inside),
        projects_dir=str(allowed),
    )
    assert res.get("ok") is True, f"expected inside-project file to be accepted, got: {res!r}"
    assert res.get("job_id"), f"expected a job_id, got: {res!r}"


def test_mcp_legacy_start_outside_projects_dir_rejected(tmp_path):
    """Legacy (repo/mode/request_file) starts outside projects_dir must be rejected."""
    from unittest.mock import patch

    from crew_chief import mcp_tools

    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)
    outside_repo = tmp_path / "outside-repo"
    outside_repo.mkdir(parents=True, exist_ok=True)
    request_file = outside_repo / "request.md"
    request_file.write_text("Add a documented feature with tests.", encoding="utf-8")

    with patch(
        "crew_chief.central_cli.handle_launch", return_value=0
    ) as mock_launch:
        res = mcp_tools.start_job(
            repo=str(outside_repo),
            mode="raw-request",
            request_file=str(request_file),
            authorize_raw=True,
            projects_dir=str(allowed),
        )
    assert res.get("ok") is False, f"expected containment rejection, got: {res!r}"
    assert res.get("isError") is True
    # Containment must trigger before any launch: the launch backend is
    # stubbed to succeed, so ok=False proves the rejection came from the
    # containment check, not from a missing CLI backend.
    mock_launch.assert_not_called()
    err_text = str(res.get("error", "")).lower()
    assert (
        "outside" in err_text
        or "projects_dir" in err_text
        or "child" in err_text
        or "allowed" in err_text
    ), f"expected a containment error message, got: {res!r}"


def test_mcp_legacy_central_paths_blocked_when_contained(tmp_path):
    """Legacy central-only paths must not bypass projects_dir containment."""
    from crew_chief import mcp_tools

    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)
    central = tmp_path / "central"
    central.mkdir(parents=True, exist_ok=True)

    res = mcp_tools.status(central_dir=str(central), projects_dir=str(allowed))
    assert res.get("ok") is False, f"expected legacy status to be blocked, got: {res!r}"

    res2 = mcp_tools.answer(
        coordinator_id="dummy",
        text="hello",
        central_dir=str(central),
        projects_dir=str(allowed),
    )
    assert res2.get("ok") is False, f"expected legacy answer to be blocked, got: {res2!r}"


def test_mcp_server_rejects_unknown_arguments(tmp_path):
    """tools/call must reject arguments outside the tool input schema (not forward them)."""
    from crew_chief.mcp_server import MCPServer

    allowed = tmp_path / "allowed"
    project = _make_project(allowed)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("SYNTHETIC_PRIVATE_OUTSIDE_ALLOWED_PROJECTS", encoding="utf-8")

    server = MCPServer(projects_dir=str(allowed))
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "start_job",
                "arguments": {
                    "project": str(project),
                    "request_file": str(outside),
                    "repo": str(project),
                    "mode": "raw-request",
                    "authorize_raw": True,
                },
            },
        }
    )
    result = response["result"]
    assert result.get("ok") is False, f"expected unknown-arg rejection, got: {result!r}"
    assert "Unknown argument" in str(result.get("error", "")), f"got: {result!r}"


def test_mcp_server_allows_schema_arguments(tmp_path):
    """Schema-declared arguments plus server context must still be accepted."""
    from crew_chief.mcp_server import MCPServer

    allowed = tmp_path / "allowed"
    project = _make_project(allowed)

    server = MCPServer(projects_dir=str(allowed))
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "start_job",
                "arguments": {
                    "project": str(project),
                    "request": "Implement a documented feature.",
                },
            },
        }
    )
    result = response["result"]
    assert result.get("ok") is True, f"expected schema args to be accepted, got: {result!r}"
    assert result.get("job_id"), f"expected a job_id, got: {result!r}"


def test_mcp_server_rejects_non_tool_names(tmp_path):
    """tools/call must only dispatch TOOLS_DEFINITIONS names, never helpers."""
    import json

    from crew_chief.mcp_server import MCPServer

    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)
    outside_central = tmp_path / "outside-central"
    coord_id = "123e4567-e89b-12d3-a456-426614174000"
    coord_dir = outside_central / "coordinators" / coord_id
    coord_dir.mkdir(parents=True, exist_ok=True)
    marker = "SYNTHETIC_PRIVATE_SUPERVISOR_MARKER"
    (coord_dir / "supervisor.json").write_text(
        json.dumps({"events": [{"marker": marker}], "state": "x"}),
        encoding="utf-8",
    )

    server = MCPServer()
    response = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "_read_supervisor_events",
                "arguments": {
                    "coordinator_id": coord_id,
                    "central_dir": str(outside_central),
                },
            },
        }
    )
    result = response["result"]
    assert result.get("ok") is False, f"expected helper-name rejection, got: {result!r}"
    assert result.get("isError") is True, f"expected isError, got: {result!r}"
    assert marker not in str(result), "private helper output must never leak"

    response2 = server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "no_such_tool_xyz", "arguments": {}},
        }
    )
    result2 = response2["result"]
    assert result2.get("ok") is False, f"expected unknown-tool rejection, got: {result2!r}"
    assert result2.get("isError") is True, f"expected isError, got: {result2!r}"
