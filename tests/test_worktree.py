"""Tests for worktrees, containment snapshots and diff stats."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from crew_chief.worktree import (
    NotAGitRepo,
    accept,
    branch_exists,
    commit_worktree,
    create_worktree,
    diffstat,
    next_free_attempt,
    discard,
    outside_writes,
    snapshot,
)


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _make_repo(path: Path) -> Path:
    """A git repo with local user config and one commit, at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")
    return path


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to an empty temporary directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


# --- cases from the brief --------------------------------------------------


def test_not_a_git_repo_raises(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotAGitRepo):
        create_worktree(plain, "job1", 1, 1)


def test_create_worktree_with_space_in_project_path(
    tmp_path: Path, home_dir: Path
):
    repo = _make_repo(tmp_path / "My Project")

    worktree, branch = create_worktree(repo, "job1", 1, 1)

    assert worktree == tmp_path / "My Project.crew" / "job1-1-1"
    assert worktree.is_dir()
    assert branch == "crew/job1-1-1"
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch

    (worktree / ".crew-chief-brief.md").write_text("brief\n", encoding="utf-8")
    status = _git(worktree, "status", "--porcelain")
    assert ".crew-chief-brief.md" not in status


def test_create_worktree_leaves_a_complete_shared_exclude_alone(
    git_repo: Path, home_dir: Path
):
    # info/exclude is shared by every worktree of the repo: rewriting it while
    # a parallel attempt runs `git status` made git die with "cannot use
    # .git/info/exclude as an exclude file" on Windows.
    import os

    create_worktree(git_repo, "job1", 1, 1)
    exclude = git_repo / ".git" / "info" / "exclude"
    assert ".crew-chief-brief.md" in exclude.read_text(encoding="utf-8")
    os.utime(exclude, ns=(1_000_000_000, 1_000_000_000))

    create_worktree(git_repo, "job1", 1, 2)

    assert exclude.stat().st_mtime_ns == 1_000_000_000


def test_snapshot_ignores_records_other_crew_chief_processes_write(
    git_repo: Path, home_dir: Path
):
    # Parallel dispatches update the job record (tracked once committed), its
    # lock file and the registries while this attempt runs; none of that is
    # the executor writing outside its worktree.
    records = git_repo / "docs" / "crew-chief"
    (records / "jobs").mkdir(parents=True)
    (records / "jobs" / "job1.json").write_text("{}\n", encoding="utf-8")
    (records / "models-seen.json").write_text("{}\n", encoding="utf-8")
    _git(git_repo, "add", "docs")
    _git(git_repo, "commit", "-m", "records")
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (records / "jobs" / "job1.json").write_text('{"x": 1}\n', encoding="utf-8")
    (records / "jobs" / "job1.lock").write_text("123\n", encoding="utf-8")
    (records / "jobs" / "job1.md").write_text("# job\n", encoding="utf-8")
    (records / "models-seen.json").write_text('{"x": 1}\n', encoding="utf-8")
    (records / "scoreboard.md").write_text("# Scoreboard\n", encoding="utf-8")
    (records / "notes.md").write_text("not a record\n", encoding="utf-8")
    after = snapshot(git_repo, worktree)

    assert outside_writes(before, after) == ["repo: ?? docs/crew-chief/notes.md"]


def test_snapshot_retries_a_git_status_that_fails_for_a_moment(
    git_repo: Path, home_dir: Path, monkeypatch
):
    import crew_chief.worktree as worktree_module

    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    real_git = worktree_module._git
    calls = {"n": 0}

    def flaky_git(args, cwd, check=True):
        if args[0] == "status":
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError(
                    "git status failed: fatal: cannot use .git/info/exclude "
                    "as an exclude file"
                )
        return real_git(args, cwd, check)

    monkeypatch.setattr(worktree_module, "_git", flaky_git)

    taken = snapshot(git_repo, worktree)

    assert calls["n"] == 3
    assert taken["repo"] == set()


def test_create_worktree_refuses_existing_path(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)

    with pytest.raises(RuntimeError):
        create_worktree(git_repo, "job1", 1, 1)

    assert worktree.is_dir()


def test_preexisting_dirty_file_is_not_outside_write(git_repo: Path, home_dir: Path):
    (git_repo / "dirty.txt").write_text("already dirty\n", encoding="utf-8")
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)

    before = snapshot(git_repo, worktree)
    after = snapshot(git_repo, worktree)

    assert outside_writes(before, after) == []


def test_write_in_main_repo_is_outside_write(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (git_repo / "leak.txt").write_text("leak\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    writes = outside_writes(before, after)
    assert any(w.startswith("repo:") and "leak.txt" in w for w in writes), writes


def test_write_in_sibling_dir_is_outside_write(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (worktree.parent / "stray.txt").write_text("stray\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert "sibling: stray.txt" in outside_writes(before, after)


def test_diffstat_flags_mass_deletion(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    target = worktree / "big.txt"
    target.write_text("\n".join(f"line {i}" for i in range(40)) + "\n", encoding="utf-8")
    commit_worktree(worktree, "add big file")
    target.write_text("kept 0\nkept 1\n", encoding="utf-8")

    rows, destructive = diffstat(worktree)

    assert "big.txt" in destructive
    row = next(r for r in rows if r["file"] == "big.txt")
    assert (row["added"], row["deleted"]) == (2, 40)


def test_diffstat_does_not_flag_balanced_rewrite(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    target = worktree / "balanced.txt"
    target.write_text("\n".join(f"old {i}" for i in range(40)) + "\n", encoding="utf-8")
    commit_worktree(worktree, "add balanced file")
    target.write_text(
        "\n".join(f"new {i}" for i in range(20)) + "\n", encoding="utf-8"
    )

    rows, destructive = diffstat(worktree)

    row = next(r for r in rows if r["file"] == "balanced.txt")
    assert (row["added"], row["deleted"]) == (20, 40)
    assert destructive == []


def test_accept_merges_and_removes_worktree(git_repo: Path, home_dir: Path):
    worktree, branch = create_worktree(git_repo, "job1", 1, 1)
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")
    commit_worktree(worktree, "add feature")

    accept(git_repo, worktree, branch)

    assert not worktree.exists()
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "feature\n"
    assert _git(git_repo, "log", "-1", "--format=%s").strip() == f"merge {branch}"
    assert _git(git_repo, "branch", "--list", branch).strip() == ""


# --- snapshot and outside writes -------------------------------------------


def test_write_in_home_dir_is_outside_write(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (home_dir / "escaped.txt").write_text("escaped\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert "home: escaped.txt" in outside_writes(before, after)


def test_snapshot_ignores_the_worktree_itself(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)

    snap = snapshot(git_repo, worktree)

    assert worktree.name not in snap["sibling"]


def test_changed_sibling_entry_is_reported_once(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    sibling = worktree.parent / "sibling.txt"
    sibling.write_text("one\n", encoding="utf-8")
    before = snapshot(git_repo, worktree)

    sibling.write_text("two\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == ["sibling: sibling.txt"]


def test_outside_writes_is_sorted_and_labelled(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (git_repo / "zeta.txt").write_text("z\n", encoding="utf-8")
    (worktree.parent / "alpha.txt").write_text("a\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    writes = outside_writes(before, after)
    assert writes == sorted(writes)
    assert all(
        w.startswith(("repo: ", "sibling: ", "home: ")) for w in writes
    ), writes


# --- diffstat --------------------------------------------------------------


def test_diffstat_counts_untracked_files_as_added(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    (worktree / "new.txt").write_text("a\nb\nc\n", encoding="utf-8")

    rows, destructive = diffstat(worktree)

    row = next(r for r in rows if r["file"] == "new.txt")
    assert (row["added"], row["deleted"]) == (3, 0)
    assert destructive == []


def test_diffstat_needs_no_commit(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)

    rows, destructive = diffstat(worktree)

    assert rows == []
    assert destructive == []


def test_diffstat_flags_deleted_file_outright(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    commit_worktree(worktree, "nothing to do")
    (worktree / "README.md").unlink()

    _, destructive = diffstat(worktree)

    assert destructive == ["README.md"]


def test_diffstat_uses_base_commit(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    target = worktree / "file.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    start = commit_worktree(worktree, "three lines")
    target.write_text("first\nsecond\nthird\nfourth\n", encoding="utf-8")

    rows, _ = diffstat(worktree, base=start)

    row = next(r for r in rows if r["file"] == "file.txt")
    assert (row["added"], row["deleted"]) == (1, 0)


# --- commit, accept and discard -------------------------------------------


def test_commit_worktree_returns_sha(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    (worktree / "work.txt").write_text("work\n", encoding="utf-8")

    sha = commit_worktree(worktree, "the work")

    assert sha == _git(worktree, "rev-parse", "HEAD").strip()
    assert _git(worktree, "log", "-1", "--format=%s").strip() == "the work"


def test_commit_worktree_returns_none_without_changes(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)

    assert commit_worktree(worktree, "nothing") is None


def test_accept_conflict_raises_and_keeps_everything(git_repo: Path, home_dir: Path):
    worktree, branch = create_worktree(git_repo, "job1", 1, 1)
    (worktree / "README.md").write_text("# from worktree\n", encoding="utf-8")
    commit_worktree(worktree, "worktree change")
    (git_repo / "README.md").write_text("# from main\n", encoding="utf-8")
    _git(git_repo, "commit", "-am", "main change")

    with pytest.raises(RuntimeError):
        accept(git_repo, worktree, branch)

    assert worktree.is_dir()
    assert _git(git_repo, "branch", "--list", branch).strip() != ""
    assert _git(git_repo, "status", "--porcelain") == ""


def test_discard_removes_worktree_and_branch(git_repo: Path, home_dir: Path):
    worktree, branch = create_worktree(git_repo, "job1", 1, 1)
    (worktree / "unwanted.txt").write_text("unwanted\n", encoding="utf-8")

    discard(git_repo, worktree, branch)

    assert not worktree.exists()
    assert not (git_repo / "unwanted.txt").exists()
    assert _git(git_repo, "branch", "--list", branch).strip() == ""
    assert _git(git_repo, "status", "--porcelain") == ""


# --- parallel worktrees and agent home entries -----------------------------


def test_parallel_worktree_appearing_is_not_outside_write(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "20261019-1200-job", 1, 1)
    before = snapshot(git_repo, worktree)

    other, _ = create_worktree(git_repo, "20261019-1200-job", 2, 1)
    (other / "work.py").write_text("x = 1\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == []


def test_parallel_worktree_changing_is_not_outside_write(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "20261019-1200-job", 1, 1)
    other, _ = create_worktree(git_repo, "20261019-1200-job", 2, 1)
    before = snapshot(git_repo, worktree)

    (other / "new-file.py").write_text("x = 1\n", encoding="utf-8")
    (other / "README.md").unlink()

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == []


def test_registered_worktree_with_any_name_is_not_outside_write(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    custom = worktree.parent / "hand-made"
    _git(git_repo, "worktree", "add", "-b", "hand", str(custom), "HEAD")

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == []


def test_unknown_folder_in_crew_dir_is_outside_write(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "20261019-1200-job", 1, 1)
    before = snapshot(git_repo, worktree)

    (worktree.parent / "loot").mkdir()

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == ["sibling: loot"]


def test_agent_tool_entries_in_home_are_not_outside_write(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (home_dir / ".claude").mkdir()
    (home_dir / ".claude.json").write_text("{}", encoding="utf-8")
    (home_dir / ".claude.json.tmp.4242.abcdef").write_text("{}", encoding="utf-8")
    (home_dir / ".codex").mkdir()
    (home_dir / ".gemini").mkdir()
    (home_dir / ".local").mkdir()

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == []


def test_unknown_home_entry_is_still_outside_write(git_repo: Path, home_dir: Path):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree)

    (home_dir / ".claude").mkdir()
    (home_dir / "stolen.txt").write_text("x", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == ["home: stolen.txt"]


def test_extra_home_ignores_are_merged_with_the_defaults(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    before = snapshot(git_repo, worktree, ignore_home=[".my-agent*"])

    (home_dir / ".my-agent-cache").mkdir()
    (home_dir / ".claude").mkdir()
    (home_dir / ".other").mkdir()

    after = snapshot(git_repo, worktree, ignore_home=[".my-agent*"])
    assert outside_writes(before, after) == ["home: .other"]


def test_write_in_main_repo_is_flagged_even_with_parallel_worktrees(
    git_repo: Path, home_dir: Path
):
    worktree, _ = create_worktree(git_repo, "20261019-1200-job", 1, 1)
    before = snapshot(git_repo, worktree)

    create_worktree(git_repo, "20261019-1200-job", 2, 1)
    (git_repo / "leak.txt").write_text("leak\n", encoding="utf-8")

    after = snapshot(git_repo, worktree)
    assert outside_writes(before, after) == ["repo: ?? leak.txt"]


# --- orphan folders and the next free attempt ------------------------------


def test_create_worktree_replaces_an_orphan_folder(git_repo: Path, home_dir: Path):
    orphan = git_repo.parent / f"{git_repo.name}.crew" / "job1-1-1"
    orphan.mkdir(parents=True)
    (orphan / "leftover.txt").write_text("half removed\n", encoding="utf-8")
    (orphan / ".git").write_text("gitdir: /nowhere/at/all\n", encoding="utf-8")

    worktree, branch = create_worktree(git_repo, "job1", 1, 1)

    assert worktree == orphan
    assert not (worktree / "leftover.txt").exists()
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch


def test_create_worktree_prunes_a_registration_whose_folder_is_gone(
    git_repo: Path, home_dir: Path
):
    import shutil

    worktree, _ = create_worktree(git_repo, "job1", 1, 1)
    shutil.rmtree(worktree)
    _git(git_repo, "update-ref", "-d", "refs/heads/crew/job1-1-1")

    again, _ = create_worktree(git_repo, "job1", 1, 1)

    assert (again / "README.md").is_file()


def test_next_free_attempt_skips_existing_worktrees_and_branches(
    git_repo: Path, home_dir: Path
):
    create_worktree(git_repo, "job1", 1, 1)
    _git(git_repo, "branch", "crew/job1-1-2")
    orphan = git_repo.parent / f"{git_repo.name}.crew" / "job1-1-3"
    orphan.mkdir()

    assert next_free_attempt(git_repo, "job1", 1, 1) == 3
    assert next_free_attempt(git_repo, "job1", 1, 4) == 4
    assert next_free_attempt(git_repo, "job1", 2, 1) == 1


def test_create_worktree_starts_from_the_given_branch(
    git_repo: Path, home_dir: Path
):
    first, first_branch = create_worktree(git_repo, "job1", 1, 1)
    (first / "earlier.py").write_text("x = 1\n", encoding="utf-8")
    commit_worktree(first, "earlier work")

    second, branch = create_worktree(git_repo, "job1", 1, 2, start=first_branch)

    assert branch == "crew/job1-1-2"
    assert (second / "earlier.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (git_repo / "earlier.py").exists()


def test_branch_exists(git_repo: Path, home_dir: Path):
    _worktree, branch = create_worktree(git_repo, "job1", 1, 1)

    assert branch_exists(git_repo, branch)
    assert not branch_exists(git_repo, "crew/job1-1-9")
