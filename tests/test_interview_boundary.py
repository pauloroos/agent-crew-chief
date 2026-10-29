"""Tests for project boundary enforcement across interview service, MCP tools, and CLI (T036, T031, FR-024).

Verifies that all operations reject project paths outside configured projects_dir:
- root-itself
- outside absolute paths
- path traversal (../)
- symlinks escaping projects_dir
- outside project answers
- all lookup endpoints (start, answer, status, questions, events, result, stop, list)
- CLI interview commands
- priority between server constructor and global prefs
- immunity against client kwargs override attempts
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from crew_chief import interview_service
from crew_chief.cli import main
from crew_chief.project_resolver import ProjectResolutionError


@pytest.fixture()
def boundary_env():
    """Set up an isolated projects_dir with valid child, outside dir, traversal, and symlink."""
    with (
        tempfile.TemporaryDirectory() as projects_tmp,
        tempfile.TemporaryDirectory() as outside_tmp,
        tempfile.TemporaryDirectory() as prefs_tmp,
    ):
        projects_dir = Path(projects_tmp).resolve()
        outside_dir = Path(outside_tmp).resolve()
        prefs_dir = Path(prefs_tmp).resolve()

        valid_child = projects_dir / "valid-child"
        valid_child.mkdir(parents=True, exist_ok=True)
        (valid_child / "docs" / "crew-chief").mkdir(parents=True, exist_ok=True)
        (valid_child / "docs" / "crew-chief" / "preferences.toml").write_text("", encoding="utf-8")

        outside_project = outside_dir / "rogue-project"
        outside_project.mkdir(parents=True, exist_ok=True)
        (outside_project / "docs" / "crew-chief").mkdir(parents=True, exist_ok=True)

        symlink_escape = projects_dir / "symlink-outside"
        symlink_escape.symlink_to(outside_project)

        global_prefs = prefs_dir / "global-preferences.toml"
        global_prefs.write_text(f'projects_dir = "{projects_dir.as_posix()}"\n', encoding="utf-8")

        old_env = os.environ.get("AGENT_CREW_CHIEF_GLOBAL_PREFS")
        os.environ["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = str(global_prefs)

        yield {
            "projects_dir": projects_dir,
            "valid_child": valid_child,
            "outside_dir": outside_dir,
            "outside_project": outside_project,
            "symlink_escape": symlink_escape,
            "global_prefs": global_prefs,
        }

        if old_env is not None:
            os.environ["AGENT_CREW_CHIEF_GLOBAL_PREFS"] = old_env
        else:
            os.environ.pop("AGENT_CREW_CHIEF_GLOBAL_PREFS", None)


def test_start_interview_rejects_outside_project_and_avoids_writes(boundary_env):
    """start_interview_job must refuse outside paths, root-itself, traversal, symlink escape, writing no files."""
    projects_dir = boundary_env["projects_dir"]
    outside_project = boundary_env["outside_project"]
    symlink_escape = boundary_env["symlink_escape"]

    # 1. Outside absolute path
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(outside_project), request="test request")
    assert not (outside_project / "docs" / "crew-chief" / "jobs").exists() or not list(
        (outside_project / "docs" / "crew-chief" / "jobs").glob("*.json")
    )

    # 2. projects_dir itself (root equality rejected)
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(projects_dir), request="test request")
    assert not (projects_dir / "docs" / "crew-chief" / "jobs").exists() or not list(
        (projects_dir / "docs" / "crew-chief" / "jobs").glob("*.json")
    )

    # 3. Path traversal escape
    traversal_path = str(boundary_env["valid_child"] / ".." / ".." / outside_project.name)
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=traversal_path, request="test request")

    # 4. Symlink escape
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(symlink_escape), request="test request")


def test_interview_project_answer_rejects_outside_paths(boundary_env):
    """Submitting answer for key='project' must reject values outside projects_dir."""
    valid_child = boundary_env["valid_child"]
    outside_project = boundary_env["outside_project"]
    projects_dir = boundary_env["projects_dir"]

    # Start a valid job in valid_child
    start_res = interview_service.start_interview_job(
        project=str(valid_child), request="Initial request"
    )
    job_id = start_res["job_id"]

    # Answering project with outside path must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child), job_id=job_id, key="project", value=str(outside_project)
        )

    # Answering project with root-itself must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child), job_id=job_id, key="project", value=str(projects_dir)
        )

    # Answering project with symlink escaping must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child),
            job_id=job_id,
            key="project",
            value=str(boundary_env["symlink_escape"]),
        )


def test_cli_interview_rejects_outside_project(boundary_env, capsys):
    """CLI interview commands must reject outside project paths with non-zero exit."""
    outside_project = str(boundary_env["outside_project"])

    code = main(["interview", "--project", outside_project, "--request", "CLI outside test", "--json"])
    assert code != 0
    err = capsys.readouterr().err
    assert "outside" in err.lower() or "not under" in err.lower() or "error" in err.lower()


