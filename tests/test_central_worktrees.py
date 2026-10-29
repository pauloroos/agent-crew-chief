"""Tests for isolated coordinator worktrees (T007, FR-002/021, SC-001, US1/AC1).

Every case runs against temporary git repositories and only records git argv;
no merge, push or automatic removal is allowed anywhere in the module.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from crew_chief import central_worktrees
from crew_chief.central_worktrees import WorktreeError, create


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


def _canonical_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def recorded_git(monkeypatch) -> list[list[str]]:
    """Record every argv passed to ``subprocess.run`` by the module."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        calls.append([str(item) for item in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(central_worktrees.subprocess, "run", recording_run)
    return calls


def _assert_no_merge_push_or_removal(calls: list[list[str]]) -> None:
    """No recorded argv may merge, push, delete or remove anything."""
    for argv in calls:
        words = argv[1:]
        assert "merge" not in words, argv
        assert "push" not in words, argv
        assert "-D" not in argv, argv
        assert "remove" not in words, argv
        assert "prune" not in words, argv


def test_two_repositories_get_distinct_worktrees(tmp_path: Path, recorded_git):
    first = _make_repo(tmp_path / "repo-a")
    second = _make_repo(tmp_path / "repo-b")
    first_id, second_id = _canonical_id(), _canonical_id()

    one = create(first, first_id)
    two = create(second, second_id)

    assert one["coordinator_id"] == first_id
    assert two["coordinator_id"] == second_id
    assert one["worktree"] != two["worktree"]
    assert Path(one["worktree"]) == tmp_path / "repo-a.crew" / f"central-{first_id}"
    assert Path(two["worktree"]) == tmp_path / "repo-b.crew" / f"central-{second_id}"
    assert one["branch"] == f"central/{first_id}"
    assert two["branch"] == f"central/{second_id}"
    for result in (one, two):
        worktree = Path(result["worktree"])
        assert worktree.is_dir()
        assert (worktree / "README.md").read_text(encoding="utf-8") == "# repo\n"
        assert (
            _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()
            == result["branch"]
        )
        assert _git(worktree, "rev-parse", "HEAD").strip() == result["base_commit"]
    _assert_no_merge_push_or_removal(recorded_git)


def test_two_coordinators_in_one_repository_stay_separate(
    tmp_path: Path, recorded_git
):
    repo = _make_repo(tmp_path / "repo")
    ids = [_canonical_id(), _canonical_id()]

    results = [create(repo, coordinator_id) for coordinator_id in ids]

    assert len({result["branch"] for result in results}) == 2
    assert len({result["worktree"] for result in results}) == 2
    for result in results:
        assert Path(result["worktree"]).is_dir()
    branches = {
        line.strip().lstrip("*+ ").strip()
        for line in _git(repo, "branch", "--list", "central/*").splitlines()
    }
    assert branches == {f"central/{coordinator_id}" for coordinator_id in ids}
    _assert_no_merge_push_or_removal(recorded_git)


def test_dirty_source_checkout_is_preserved(tmp_path: Path, recorded_git):
    repo = _make_repo(tmp_path / "repo")
    (repo / "README.md").write_text("# dirty\n", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked\n", encoding="utf-8")
    status_before = _git(repo, "status", "--porcelain")

    result = create(repo, _canonical_id())

    assert (repo / "README.md").read_text(encoding="utf-8") == "# dirty\n"
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "untracked\n"
    assert _git(repo, "status", "--porcelain") == status_before
    worktree = Path(result["worktree"])
    assert (worktree / "README.md").read_text(encoding="utf-8") == "# repo\n"
    assert not (worktree / "notes.txt").exists()
    _assert_no_merge_push_or_removal(recorded_git)


def test_explicit_base_is_used(tmp_path: Path, recorded_git):
    repo = _make_repo(tmp_path / "repo")
    first_commit = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "README.md").write_text("# second\n", encoding="utf-8")
    _git(repo, "commit", "-am", "second commit")

    result = create(repo, _canonical_id(), base=first_commit)

    assert result["base_commit"] == first_commit
    worktree = Path(result["worktree"])
    assert (worktree / "README.md").read_text(encoding="utf-8") == "# repo\n"
    _assert_no_merge_push_or_removal(recorded_git)


def test_branch_collision_is_rejected_without_removal(tmp_path: Path, recorded_git):
    repo = _make_repo(tmp_path / "repo")
    coordinator_id = _canonical_id()
    create(repo, coordinator_id)
    expected = tmp_path / "repo.crew" / f"central-{coordinator_id}"

    with pytest.raises(WorktreeError, match="already exists"):
        create(repo, coordinator_id)

    assert (expected / "README.md").is_file()
    _assert_no_merge_push_or_removal(recorded_git)


def test_existing_worktree_folder_is_not_deleted(tmp_path: Path, recorded_git):
    repo = _make_repo(tmp_path / "repo")
    coordinator_id = _canonical_id()
    orphan = tmp_path / "repo.crew" / f"central-{coordinator_id}"
    orphan.mkdir(parents=True)
    (orphan / "keep.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="already exists"):
        create(repo, coordinator_id)

    assert (orphan / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    _assert_no_merge_push_or_removal(recorded_git)


def test_unborn_head_is_rejected(tmp_path: Path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init")

    with pytest.raises(WorktreeError, match="HEAD"):
        create(repo, _canonical_id())

    assert not (tmp_path / "empty.crew").exists()


def test_path_with_spaces_is_supported(tmp_path: Path, recorded_git):
    repo = _make_repo(tmp_path / "My Project")
    coordinator_id = _canonical_id()

    result = create(repo, coordinator_id)

    assert Path(result["worktree"]) == (
        tmp_path / "My Project.crew" / f"central-{coordinator_id}"
    )
    assert (Path(result["worktree"]) / "README.md").is_file()
    _assert_no_merge_push_or_removal(recorded_git)


def test_non_canonical_coordinator_id_is_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")
    for bad in ("", "not-a-uuid", str(uuid.uuid4()).upper(), "..", "a/b"):
        with pytest.raises(WorktreeError, match="UUID"):
            create(repo, bad)
    assert not (tmp_path / "repo.crew").exists()


def test_not_a_git_repository_is_rejected(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorktreeError, match="git repository"):
        create(plain, _canonical_id())


def test_missing_base_is_rejected(tmp_path: Path):
    repo = _make_repo(tmp_path / "repo")

    with pytest.raises(WorktreeError):
        create(repo, _canonical_id(), base="no-such-revision")
