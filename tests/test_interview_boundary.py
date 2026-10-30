"""Tests for project boundary enforcement across interview service and CLI (T036, T031, FR-024).

Verifies that all operations reject project paths outside the given projects_dir:
- root-itself
- outside absolute paths
- path traversal (../)
- symlinks escaping projects_dir
- outside project answers
- CLI interview commands
"""

from __future__ import annotations

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
    ):
        projects_dir = Path(projects_tmp).resolve()
        outside_dir = Path(outside_tmp).resolve()

        valid_child = projects_dir / "valid-child"
        valid_child.mkdir(parents=True, exist_ok=True)
        (valid_child / "docs" / "crew-chief").mkdir(parents=True, exist_ok=True)
        (valid_child / "docs" / "crew-chief" / "preferences.toml").write_text("", encoding="utf-8")

        outside_project = outside_dir / "rogue-project"
        outside_project.mkdir(parents=True, exist_ok=True)
        (outside_project / "docs" / "crew-chief").mkdir(parents=True, exist_ok=True)

        symlink_escape = projects_dir / "symlink-outside"
        symlink_escape.symlink_to(outside_project)

        yield {
            "projects_dir": projects_dir,
            "valid_child": valid_child,
            "outside_dir": outside_dir,
            "outside_project": outside_project,
            "symlink_escape": symlink_escape,
        }


def test_start_interview_rejects_outside_project_and_avoids_writes(boundary_env):
    """start_interview_job must refuse outside paths, root-itself, traversal, symlink escape, writing no files."""
    projects_dir = boundary_env["projects_dir"]
    outside_project = boundary_env["outside_project"]
    symlink_escape = boundary_env["symlink_escape"]

    # 1. Outside absolute path
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(outside_project), request="test request", projects_dir=projects_dir)
    assert not (outside_project / "docs" / "crew-chief" / "jobs").exists() or not list(
        (outside_project / "docs" / "crew-chief" / "jobs").glob("*.json")
    )

    # 2. projects_dir itself (root equality rejected)
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(projects_dir), request="test request", projects_dir=projects_dir)
    assert not (projects_dir / "docs" / "crew-chief" / "jobs").exists() or not list(
        (projects_dir / "docs" / "crew-chief" / "jobs").glob("*.json")
    )

    # 3. Path traversal escape
    traversal_path = str(boundary_env["valid_child"] / ".." / ".." / outside_project.name)
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=traversal_path, request="test request", projects_dir=projects_dir)

    # 4. Symlink escape
    with pytest.raises(ProjectResolutionError):
        interview_service.start_interview_job(project=str(symlink_escape), request="test request", projects_dir=projects_dir)


def test_interview_project_answer_rejects_outside_paths(boundary_env):
    """Submitting answer for key='project' must reject values outside projects_dir."""
    valid_child = boundary_env["valid_child"]
    outside_project = boundary_env["outside_project"]
    projects_dir = boundary_env["projects_dir"]

    # Start a valid job in valid_child
    start_res = interview_service.start_interview_job(
        project=str(valid_child), request="Initial request", projects_dir=projects_dir
    )
    job_id = start_res["job_id"]

    # Answering project with outside path must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child), job_id=job_id, key="project", value=str(outside_project),
            projects_dir=projects_dir,
        )

    # Answering project with root-itself must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child), job_id=job_id, key="project", value=str(projects_dir),
            projects_dir=projects_dir,
        )

    # Answering project with symlink escaping must be rejected
    with pytest.raises((ProjectResolutionError, ValueError)):
        interview_service.answer_interview_job(
            project=str(valid_child),
            job_id=job_id,
            key="project",
            value=str(boundary_env["symlink_escape"]),
            projects_dir=projects_dir,
        )


def test_cli_interview_rejects_outside_project(boundary_env, capsys):
    """CLI interview commands must reject outside project paths with non-zero exit."""
    outside_project = str(boundary_env["outside_project"])

    code = main([
        "interview", "--projects-dir", str(boundary_env["projects_dir"]),
        "--project", outside_project, "--request", "CLI outside test", "--json",
    ])
    assert code != 0
    err = capsys.readouterr().err
    assert "outside" in err.lower() or "not under" in err.lower() or "error" in err.lower()


