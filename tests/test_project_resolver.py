"""Tests for project_resolver: directory restriction to projects_dir (T031, FR-024)."""

import tempfile
from pathlib import Path

import pytest

from crew_chief.project_resolver import (
    ProjectResolutionError,
    resolve_project_path,
)


def test_rejects_paths_outside_projects_dir():
    """Project paths outside projects_dir must be rejected (FR-024)."""
    with tempfile.TemporaryDirectory() as projects_tmp, tempfile.TemporaryDirectory() as outside_tmp:
        projects_dir = Path(projects_tmp)
        outside_path = Path(outside_tmp) / "some-project"
        outside_path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ProjectResolutionError) as exc_info:
            resolve_project_path(str(outside_path), str(projects_dir))

        assert "outside" in str(exc_info.value).lower() or "not under" in str(exc_info.value).lower()


def test_accepts_child_directory_of_projects_dir():
    """Project paths that are child directories of projects_dir must be accepted."""
    with tempfile.TemporaryDirectory() as projects_tmp:
        projects_dir = Path(projects_tmp)
        project_path = projects_dir / "my-project"
        project_path.mkdir(parents=True, exist_ok=True)

        resolved = resolve_project_path(str(project_path), str(projects_dir))
        assert resolved.resolve() == project_path.resolve()


def test_resolves_symlinks_before_check():
    """Symlinks must be resolved before enforcing the child-directory restriction."""
    with tempfile.TemporaryDirectory() as projects_tmp, tempfile.TemporaryDirectory() as real_tmp:
        projects_dir = Path(projects_tmp)
        real_project = Path(real_tmp) / "real-project"
        real_project.mkdir(parents=True, exist_ok=True)

        # Create a symlink inside projects_dir pointing to the real project outside
        symlink_path = projects_dir / "linked-project"
        symlink_path.symlink_to(real_project)

        # The symlink target is outside projects_dir, so it should be rejected
        with pytest.raises(ProjectResolutionError):
            resolve_project_path(str(symlink_path), str(projects_dir))


def test_rejects_traversal_attempts():
    """Path traversal attempts (e.g., ../) must be rejected."""
    with tempfile.TemporaryDirectory() as projects_tmp, tempfile.TemporaryDirectory() as other_tmp:
        projects_dir = Path(projects_tmp)
        other_dir = Path(other_tmp)

        # Attempt to escape via traversal
        traversal_path = projects_dir / ".." / other_dir.name
        traversal_path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ProjectResolutionError):
            resolve_project_path(str(traversal_path), str(projects_dir))


def test_projects_dir_must_exist():
    """If projects_dir does not exist, resolution must fail."""
    with tempfile.TemporaryDirectory() as tmp:
        projects_dir = Path(tmp) / "nonexistent"

        with pytest.raises(ProjectResolutionError) as exc_info:
            resolve_project_path("/some/path", str(projects_dir))

        assert "does not exist" in str(exc_info.value).lower() or "not found" in str(exc_info.value).lower()


def test_project_path_must_exist():
    """If the project path does not exist, resolution must fail."""
    with tempfile.TemporaryDirectory() as tmp:
        projects_dir = Path(tmp)
        nonexistent_project = projects_dir / "nonexistent-project"

        with pytest.raises(ProjectResolutionError) as exc_info:
            resolve_project_path(str(nonexistent_project), str(projects_dir))

        assert "does not exist" in str(exc_info.value).lower() or "not found" in str(exc_info.value).lower()


def test_rejects_projects_dir_equality():
    """Project path equal to projects_dir must be rejected (must be strict child)."""
    with tempfile.TemporaryDirectory() as projects_tmp:
        projects_dir = Path(projects_tmp)
        with pytest.raises(ProjectResolutionError) as exc_info:
            resolve_project_path(str(projects_dir), str(projects_dir))

        assert "child" in str(exc_info.value).lower() or "itself" in str(exc_info.value).lower()


def test_drive_letter_case_robustness():
    """Verify that drive-letter casing differences (e.g., C: vs c:) on Windows do not break containment checks (FR-024)."""
    import ntpath
    import os
    from unittest.mock import patch

    class MockResolvedPath:
        def __init__(self, path_str: str) -> None:
            self._str = path_str

        def resolve(self):
            return self

        def is_dir(self) -> bool:
            return True

        def exists(self) -> bool:
            return True

        def relative_to(self, other: object) -> Path:
            if self._str[0] != other._str[0]:
                raise ValueError(f"{self._str} does not start with {other._str}")
            rel = self._str[len(other._str):].lstrip("/\\")
            return Path(rel)

        def __str__(self) -> str:
            return self._str

        def __eq__(self, other: object) -> bool:
            return self._str == getattr(other, "_str", str(other))

    with (
        patch("crew_chief.project_resolver.Path", side_effect=lambda p: MockResolvedPath(str(p))),
        patch("crew_chief.project_resolver.os.path.commonpath", side_effect=ntpath.commonpath),
        patch("crew_chief.project_resolver.os.path.normcase", side_effect=ntpath.normcase),
    ):
        # Child with lowercase c: under projects_dir with uppercase C:
        resolved = resolve_project_path("c:\\projects\\child", "C:\\projects")
        assert str(resolved) == "c:\\projects\\child"

        # Child with uppercase C: under projects_dir with lowercase c:
        resolved2 = resolve_project_path("C:\\projects\\child", "c:\\projects")
        assert str(resolved2) == "C:\\projects\\child"

        # Root equality with different drive case must be rejected
        with pytest.raises(ProjectResolutionError):
            resolve_project_path("c:\\projects", "C:\\projects")

        # Different drive entirely (d: vs c:) must be rejected
        with pytest.raises(ProjectResolutionError):
            resolve_project_path("d:\\other\\child", "C:\\projects")

    # If running on actual Windows, test with real paths on disk
    if os.name == "nt":
        with tempfile.TemporaryDirectory() as projects_tmp:
            pdir = Path(projects_tmp).resolve()
            child = pdir / "real-child"
            child.mkdir()
            pdir_str = str(pdir)
            child_str = str(child.resolve())
            if len(pdir_str) >= 2 and pdir_str[1] == ":":
                swapped_child = (
                    child_str[0].lower() if child_str[0].isupper() else child_str[0].upper()
                ) + child_str[1:]
                res = resolve_project_path(swapped_child, pdir)
                assert os.path.normcase(os.path.realpath(str(res))) == os.path.normcase(os.path.realpath(child_str))

