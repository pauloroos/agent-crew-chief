"""Project path resolution with directory restriction (T031, FR-024).

This module ensures that project paths are resolved within an allowed
``projects_dir``, preventing operations on arbitrary filesystem locations.
"""

from __future__ import annotations

import os
from pathlib import Path


class ProjectResolutionError(ValueError):
    """Raised when a project path cannot be resolved within projects_dir."""


def resolve_project_path(project_path: str | Path, projects_dir: str | Path) -> Path:
    """Resolve ``project_path`` ensuring it is under ``projects_dir``.

    Both paths are resolved to absolute form (following symlinks) before
    checking the containment relationship. The project path must exist and
    be a child of ``projects_dir``.

    Args:
        project_path: The requested project directory path.
        projects_dir: The allowed parent directory for all projects.

    Returns:
        The resolved absolute ``Path`` for the project.

    Raises:
        ProjectResolutionError: If either path does not exist, or if the
            resolved project path is not a child of ``projects_dir``.
    """
    projects_dir = Path(projects_dir).resolve()
    project_path = Path(project_path).resolve()

    if not projects_dir.is_dir():
        raise ProjectResolutionError(
            f"projects_dir does not exist or is not a directory: {projects_dir}"
        )

    if not project_path.exists():
        raise ProjectResolutionError(
            f"project path does not exist: {project_path}"
        )

    if not project_path.is_dir():
        raise ProjectResolutionError(
            f"project path is not a directory: {project_path}"
        )

    # Reject projects_dir equality (root equality check)
    if project_path == projects_dir or os.path.normcase(str(project_path)) == os.path.normcase(str(projects_dir)):
        raise ProjectResolutionError(
            f"project path must be a child directory of projects_dir: {project_path} "
            f"(cannot be projects_dir itself)"
        )

    is_child = False
    try:
        rel = project_path.relative_to(projects_dir)
        if rel != Path(".") and len(rel.parts) > 0 and not str(rel).startswith(".."):
            is_child = True
    except ValueError:
        pass

    # Fallback with commonpath for cross-platform / case-insensitive robustness
    if not is_child:
        try:
            pdir_str = str(projects_dir)
            ppath_str = str(project_path)
            common = os.path.commonpath([pdir_str, ppath_str])
            if (
                os.path.normcase(common) == os.path.normcase(pdir_str)
                and os.path.normcase(ppath_str) != os.path.normcase(pdir_str)
            ):
                is_child = True
        except (ValueError, Exception):
            pass

    if not is_child:
        raise ProjectResolutionError(
            f"project path is outside projects_dir: {project_path} "
            f"(must be under {projects_dir})"
        )

    return project_path