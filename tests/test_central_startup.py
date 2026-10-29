"""T072 — regression: no bare crew/ directory is created under the project root."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path


def test_no_root_crew_dir_after_minimal_lifecycle(tmp_path: Path):
    """A minimal coordinator/piece lifecycle must never create a bare
    ``crew/`` directory under the project root. Worktrees live in
    ``<project>.crew/`` (sibling), and central state lives under the
    configured central dir; ``crew/`` at the project root is always
    accidental."""
    # Seed a tiny git repo so worktree.create_worktree can run.
    project = tmp_path / "demo"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=project, check=True)

    # Import after seeding so the module resolves against the dev branch.
    from crew_chief import worktree

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    wt, _branch = worktree.create_worktree(project, job_id, piece=1, attempt=1)
    assert wt.exists(), "worktree must be created"

    # The worktree must live under <project>.crew/, never under project/crew/.
    assert ".crew" in str(wt.parent.name), f"unexpected worktree parent: {wt.parent}"
    bare_crew = project / "crew"
    assert not bare_crew.exists(), (
        f"bare crew/ directory must not exist under project root; found {bare_crew}"
    )

    # Cleanup: remove the worktree so the temp dir can be reclaimed.
    try:
        worktree.remove_worktree(project, wt, force=True)
    except Exception:
        shutil.rmtree(wt, ignore_errors=True)