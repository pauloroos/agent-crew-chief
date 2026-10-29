"""Project path resolution with directory restriction (T031, FR-024).

This module ensures that project paths are resolved within an allowed
``projects_dir``, preventing operations on arbitrary filesystem locations.
"""

from __future__ import annotations

import os
from pathlib import Path


class ProjectResolutionError(ValueError):
    """Raised when a project path cannot be resolved within projects_dir."""


def is_path_within(path: str | Path, container: str | Path) -> bool:
    """Return ``True`` when resolved *path* is strictly inside resolved *container*.

    Symlinks are resolved before comparison. The check is case-aware on
    POSIX and case-insensitive on Windows (via ``os.path.normcase``), with
    an ``os.path.commonpath`` fallback for cross-platform robustness.
    Missing paths resolve lexically; existence is not required.
    """
    try:
        container_resolved = Path(container).resolve()
        path_resolved = Path(path).resolve()
    except Exception:
        return False
    if path_resolved == container_resolved or os.path.normcase(
        str(path_resolved)
    ) == os.path.normcase(str(container_resolved)):
        return False
    try:
        rel = path_resolved.relative_to(container_resolved)
        if rel != Path(".") and len(rel.parts) > 0 and not str(rel).startswith(".."):
            return True
    except ValueError:
        pass
    try:
        common = os.path.commonpath([str(container_resolved), str(path_resolved)])
        if os.path.normcase(common) == os.path.normcase(
            str(container_resolved)
        ) and os.path.normcase(str(path_resolved)) != os.path.normcase(
            str(container_resolved)
        ):
            return True
    except (ValueError, Exception):
        pass
    return False


def resolve_contained_file(file_path: str | Path, container_dir: str | Path) -> Path:
    """Resolve *file_path* ensuring it is strictly inside *container_dir*.

    Both paths are resolved to absolute form (following symlinks) before
    checking containment. The container must exist and be a directory.

    Returns the resolved absolute ``Path`` for the file (existence as a
    file is checked by the caller).

    Raises:
        ProjectResolutionError: If the container does not exist, or the
            resolved file path is not strictly inside the container.
    """
    container_resolved = Path(container_dir).resolve()
    if not container_resolved.is_dir():
        raise ProjectResolutionError(
            f"projects_dir does not exist or is not a directory: {container_resolved}"
        )
    try:
        file_resolved = Path(file_path).expanduser().resolve()
    except Exception as exc:
        raise ProjectResolutionError(f"cannot resolve file path: {exc}")
    if not is_path_within(file_resolved, container_resolved):
        raise ProjectResolutionError(
            f"file path is outside allowed directory: {file_resolved} "
            f"(must be under {container_resolved})"
        )
    return file_resolved


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