"""Tests for kit_install.py: superpowers and ponytail installation.

Temporary projects and homes only. Covers the copies, every opt-out, both
installers and dry-run.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from crew_chief.kit_install import (
    _load_manifest,
    install_kit,
    install_ponytail,
    install_superpowers,
    sync_tree,
)

MANIFEST = SCRIPTS_DIR / "crew_chief" / "kit_manifest.json"
VENDOR_SUPERPOWERS = Path(__file__).resolve().parents[1] / "vendor" / "superpowers"
VENDOR_PONYTAIL = Path(__file__).resolve().parents[1] / "vendor" / "ponytail"


# --- Platform detection ------------------------------------------------------


# --- Checksum verification ---------------------------------------------------


# --- RTK installation --------------------------------------------------------


# --- Superpowers installation ------------------------------------------------


def test_install_superpowers_copies_skills(tmp_path):
    """Superpowers installation should copy selected skills."""
    dest = tmp_path / "superpowers"
    report = install_superpowers(dest, source_dir=VENDOR_SUPERPOWERS)

    assert any("installed superpowers skill" in line for line in report)
    assert (dest / "test-driven-development" / "SKILL.md").exists()
    assert (dest / "systematic-debugging" / "SKILL.md").exists()
    assert (dest / "verification-before-completion" / "SKILL.md").exists()
    assert (dest / "LICENSE").exists()
    assert (dest / "VERSION").exists()


def test_install_superpowers_dry_run(tmp_path):
    """Dry run should report without copying."""
    dest = tmp_path / "superpowers"
    report = install_superpowers(dest, dry_run=True, source_dir=VENDOR_SUPERPOWERS)

    assert any("would install" in line for line in report)
    assert not dest.exists()


def test_install_superpowers_missing_source(tmp_path):
    """Missing source directory should skip."""
    dest = tmp_path / "superpowers"
    report = install_superpowers(dest, source_dir=tmp_path / "nonexistent")

    assert any("skipped" in line for line in report)


# --- Ponytail installation ---------------------------------------------------


def test_install_ponytail_copies_skills(tmp_path):
    """Ponytail installation should copy skills."""
    dest = tmp_path / "ponytail"
    report = install_ponytail(dest, source_dir=VENDOR_PONYTAIL)

    assert any("installed ponytail skill" in line for line in report)
    assert (dest / "ponytail" / "SKILL.md").exists()
    assert (dest / "LICENSE").exists()


def test_install_ponytail_dry_run(tmp_path):
    """Dry run should report without copying."""
    dest = tmp_path / "ponytail"
    report = install_ponytail(dest, dry_run=True, source_dir=VENDOR_PONYTAIL)

    assert any("would install" in line for line in report)
    assert not dest.exists()


# --- Full kit installation ---------------------------------------------------


def test_install_kit_opt_out_superpowers(tmp_path):
    """Opting out of superpowers should skip superpowers installation."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, do_install_superpowers=False)

    assert any("--no-superpowers" in line for line in report)


def test_install_kit_opt_out_ponytail(tmp_path):
    """Opting out of ponytail should skip ponytail installation."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, do_install_ponytail=False)

    assert any("--no-ponytail" in line for line in report)


def test_install_kit_dry_run(tmp_path, monkeypatch):
    """Dry run should report without installing."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, dry_run=True)

    assert any("would" in line.lower() for line in report)


# --- Installer CLI integration -----------------------------------------------


INSTALL_PY = Path(__file__).resolve().parents[1] / "install.py"
INSTALL_MW_PY = SCRIPTS_DIR / "install_machine_wide.py"


def _run_install(project: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run install.py on project and capture output."""
    env = {**os.environ, "HOME": str(project)}
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(INSTALL_PY), str(project), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )


def test_install_py_accepts_no_superpowers_flag(tmp_path):
    """install.py should accept --no-superpowers flag."""
    project = tmp_path / "project"
    project.mkdir()

    result = _run_install(project, "--no-superpowers")

    assert result.returncode == 0, result.stderr


def test_install_machine_wide_accepts_opt_outs(tmp_path):
    """install_machine_wide.py should accept all opt-out flags."""
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, "-I", str(INSTALL_MW_PY), "--dry-run", "--home", str(home),
         "--no-superpowers", "--no-ponytail"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


def test_sync_tree_keeps_crlf_copies_and_drops_stale_files(tmp_path):
    """A CRLF checkout of the same text is left alone; real changes and removals land."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    (source / "sub").mkdir(parents=True)
    (source / "same.md").write_bytes(b"a\nb\n")
    (source / "sub" / "new.md").write_bytes(b"new\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "x.pyc").write_bytes(b"0")
    (dest / "old").mkdir(parents=True)
    (dest / "same.md").write_bytes(b"a\r\nb\r\n")
    (dest / "old" / "gone.md").write_bytes(b"x")
    sync_tree(source, dest)
    assert (dest / "same.md").read_bytes() == b"a\r\nb\r\n"
    assert (dest / "sub" / "new.md").read_bytes() == b"new\n"
    assert not (dest / "old").exists() and not (dest / "__pycache__").exists()
    (source / "same.md").write_bytes(b"changed\n")
    sync_tree(source, dest)
    assert (dest / "same.md").read_bytes() == b"changed\n"
