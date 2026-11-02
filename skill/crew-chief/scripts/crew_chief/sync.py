"""Keep the ledgers of several machines in sync through a private git repo.

Standard library plus the ``git`` CLI only. ACC never asks for, stores or
creates credentials; it uses the git access the machine already has.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import ledger

BRANCH = "main"
REMOTE = "origin"
GIT_IDENTITY = ["-c", "user.name=crew-chief", "-c", "user.email=crew-chief@localhost"]
ATTRIBUTES = "ledger.jsonl merge=union text eol=lf\n"
IGNORE = "*\n!ledger.jsonl\n!.gitattributes\n!.gitignore\n"
TRACKED = ("ledger.jsonl", ".gitattributes", ".gitignore")
NOT_SET_UP = "sync is not set up: ACC sync --init <url of a private repo>"

# shortcut: an `ACC record` that runs at the same moment as `ACC sync` can race
# with the pull or the dedupe rewrite; upgrade to a file lock around both when
# that stops being a corner case.


def _git(home: Path, *args: str, commit_like: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *(GIT_IDENTITY if commit_like else []), *args]
    try:
        return subprocess.run(cmd, cwd=home, capture_output=True, text=True)
    except FileNotFoundError:
        print("git not found: ACC sync needs the git CLI", file=sys.stderr)
        raise SystemExit(1)


def _fail(result: subprocess.CompletedProcess[str], home: Path) -> int:
    if (home / ".git" / "MERGE_HEAD").is_file():
        _git(home, "merge", "--abort")
    output = (result.stderr or result.stdout).strip()
    if output:
        print(output, file=sys.stderr)
    return 1


def _origin(home: Path) -> str | None:
    result = _git(home, "remote", "get-url", REMOTE)
    return result.stdout.strip() if result.returncode == 0 else None


def _commit_tracked(home: Path, message: str) -> bool | None:
    _git(home, "add", *TRACKED)
    diff = _git(home, "diff", "--cached", "--quiet", "--", *TRACKED)
    if diff.returncode == 0:
        return False
    result = _git(home, "commit", "-m", message, "--no-edit", commit_like=True)
    if result.returncode != 0:
        _fail(result, home)
        return None
    return True


def _has_remote_branch(home: Path, url: str) -> bool | None:
    result = _git(home, "ls-remote", "--heads", REMOTE, BRANCH)
    if result.returncode != 0:
        _fail(result, home)
        return None
    return bool(result.stdout.strip())


def _dedupe(home: Path) -> bool:
    path = home / "ledger.jsonl"
    raw = path.read_bytes().splitlines() if path.is_file() else []
    seen: set[bytes] = set()
    kept = [line for line in raw if line not in seen and not seen.add(line)]
    wanted = b"\n".join(kept) + b"\n" if kept or raw else b""
    if path.is_file() and path.read_bytes() == wanted:
        return False
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".ledger.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(wanted)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _push(home: Path, url: str) -> int:
    result = _git(home, "push", "-u", REMOTE, BRANCH)
    if result.returncode != 0:
        return _fail(result, home)
    print(f"synced {len(ledger.read())} grades with {url}")
    return 0


def run_sync(home: Path, url: str) -> int:
    if _commit_tracked(home, "sync: local changes") is None:
        return 1
    branch = _has_remote_branch(home, url)
    if branch is None:
        return 1
    if branch:
        pull = _git(home, "pull", "--no-rebase", "--no-edit", "--allow-unrelated-histories",
                     REMOTE, BRANCH, commit_like=True)
        if pull.returncode != 0:
            return _fail(pull, home)
    if _dedupe(home) and _commit_tracked(home, "sync: drop duplicate lines") is None:
        return 1
    return _push(home, url)


def main(init_url: str | None) -> int:
    home = ledger.ledger_path().parent
    if init_url is None:
        if not (home / ".git").is_dir() or _origin(home) is None:
            print(NOT_SET_UP)
            return 0
        return run_sync(home, _origin(home) or "")
    current = _origin(home) if (home / ".git").is_dir() else None
    if current is not None and current != init_url:
        print(f"sync is already set up with {current}", file=sys.stderr)
        return 2
    if current is not None:
        return run_sync(home, current)
    home.mkdir(parents=True, exist_ok=True)
    (home / "ledger.jsonl").touch(exist_ok=True)
    initialized = _git(home, "init")
    if initialized.returncode != 0:
        return _fail(initialized, home)
    _git(home, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    (home / ".gitattributes").write_text(ATTRIBUTES, encoding="utf-8")
    (home / ".gitignore").write_text(IGNORE, encoding="utf-8")
    add = _git(home, "remote", "add", REMOTE, init_url)
    if add.returncode != 0:
        return _fail(add, home)
    _commit_tracked(home, "sync: track the ledger")
    return run_sync(home, init_url)
