"""Tests for kit_install.py — rtk, superpowers, and ponytail installation.

Every test runs with fake network fixtures and temporary homes; no real
network calls or real tools on PATH. Covers valid download/installation with
checksum verification, missing/mismatched release entries, network failure
warnings, existing rtk untouched, every opt-out, both installers, dry-run,
and cross-platform paths.
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
    _fetch_checksums,
    _get_platform_key,
    _load_manifest,
    _sha256_file,
    _user_bin_dir,
    _verify_archive,
    install_kit,
    install_ponytail,
    install_rtk,
    install_superpowers,
)

MANIFEST = SCRIPTS_DIR / "crew_chief" / "kit_manifest.json"
VENDOR_SUPERPOWERS = Path(__file__).resolve().parents[1] / "vendor" / "superpowers"
VENDOR_PONYTAIL = Path(__file__).resolve().parents[1] / "vendor" / "ponytail"


def _fake_checksums_text(manifest_data: dict) -> str:
    """Generate fake checksums.txt content from manifest artifacts."""
    lines = []
    for platform_key, artifact in manifest_data.get("rtk", {}).get("artifacts", {}).items():
        filename = artifact["filename"]
        sha256 = artifact["sha256"]
        lines.append(f"{sha256}  {filename}")
    return "\n".join(lines) + "\n"


def _create_fake_archive(dest: Path, filename: str, archive_format: str) -> bytes:
    """Create a fake archive file and return its actual SHA-256."""
    if archive_format == "tar.gz":
        with tarfile.open(dest, "w:gz") as tf:
            # Add a fake rtk binary
            import io
            data = b"#!/bin/sh\necho fake rtk\n"
            info = tarfile.TarInfo(name="rtk")
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    elif archive_format == "zip":
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("rtk.exe", b"@echo off\necho fake rtk\n")
    return _sha256_file(dest)


class FakeURLOpener:
    """Fake URL opener that serves predefined content."""

    def __init__(self, checksums_text: str = "", archive_data: bytes = b""):
        self.checksums_text = checksums_text
        self.archive_data = archive_data
        self.requests = []

    def __call__(self, req, timeout=30):
        self.requests.append(req)
        from urllib.response import addinfourl
        import io

        if "checksums.txt" in req.full_url:
            return addinfourl(io.BytesIO(self.checksums_text.encode()), None, req.full_url, 200)
        else:
            return addinfourl(io.BytesIO(self.archive_data), None, req.full_url, 200)


# --- Platform detection ------------------------------------------------------


def test_get_platform_key_linux_x64(monkeypatch):
    """Linux x86_64 should return x86_64-unknown-linux-musl."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
    assert ki._get_platform_key() == "x86_64-unknown-linux-musl"


def test_get_platform_key_linux_arm64(monkeypatch):
    """Linux ARM64 should return aarch64-unknown-linux-gnu."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "aarch64")
    assert ki._get_platform_key() == "aarch64-unknown-linux-gnu"


def test_get_platform_key_macos_arm64(monkeypatch):
    """macOS ARM64 should return aarch64-apple-darwin."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "darwin")
    monkeypatch.setattr(ki._platform, "machine", lambda: "arm64")
    assert ki._get_platform_key() == "aarch64-apple-darwin"


def test_get_platform_key_macos_x64(monkeypatch):
    """macOS x86_64 should return x86_64-apple-darwin."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "darwin")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
    assert ki._get_platform_key() == "x86_64-apple-darwin"


def test_get_platform_key_windows_x64(monkeypatch):
    """Windows x86_64 should return x86_64-pc-windows-msvc."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "win32")
    monkeypatch.setattr(ki._platform, "machine", lambda: "AMD64")
    assert ki._get_platform_key() == "x86_64-pc-windows-msvc"


# --- Checksum verification ---------------------------------------------------


def test_verify_archive_matches_both(tmp_path):
    """Archive must match both manifest pin AND release checksums.txt."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"test data")
    actual_sha = _sha256_file(archive)

    checksums = {"test.tar.gz": actual_sha}
    assert _verify_archive(archive, actual_sha, "test.tar.gz", checksums) is True


def test_verify_archive_manifest_mismatch(tmp_path):
    """Manifest mismatch should fail verification."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"test data")

    checksums = {"test.tar.gz": "wrong"}
    with pytest.warns(UserWarning, match="checksum mismatch"):
        assert _verify_archive(archive, "wrong", "test.tar.gz", checksums) is False


def test_verify_archive_missing_from_release(tmp_path):
    """Filename not in release checksums.txt should fail."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"test data")
    actual_sha = _sha256_file(archive)

    checksums = {"other.tar.gz": actual_sha}
    with pytest.warns(UserWarning, match="not found in release"):
        assert _verify_archive(archive, actual_sha, "test.tar.gz", checksums) is False


def test_verify_archive_release_mismatch(tmp_path):
    """Release checksums.txt mismatch should fail."""
    archive = tmp_path / "test.tar.gz"
    archive.write_bytes(b"test data")
    actual_sha = _sha256_file(archive)

    checksums = {"test.tar.gz": "different_hash"}
    with pytest.warns(UserWarning, match="Release checksums.txt mismatch"):
        assert _verify_archive(archive, actual_sha, "test.tar.gz", checksums) is False


# --- RTK installation --------------------------------------------------------


def test_install_rtk_existing_on_path_untouched(monkeypatch):
    """If rtk is already on PATH, leave it alone."""
    with patch("shutil.which", return_value="/usr/local/bin/rtk"):
        report = install_rtk()
    assert any("already on PATH" in line for line in report)
    assert len(report) == 1


def test_install_rtk_dry_run(tmp_path, monkeypatch):
    """Dry run should report what would be done without downloading."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

    manifest = _load_manifest(MANIFEST)
    report = install_rtk(dry_run=True, manifest=manifest)

    assert any("would download" in line for line in report)
    assert any("would install" in line for line in report)


def test_install_rtk_no_manifest_warning(monkeypatch, tmp_path):
    """Missing manifest should warn and skip."""
    with pytest.warns(UserWarning):
        report = install_rtk(manifest={})
    assert any("skipped" in line for line in report)


def test_install_rtk_network_failure_warning(tmp_path, monkeypatch):
    """Network failure should warn but not fail installation."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

    manifest = _load_manifest(MANIFEST)
    platform_key = ki._get_platform_key()
    filename = manifest["rtk"]["artifacts"][platform_key]["filename"]

    def failing_fetch(url):
        raise ConnectionError("Network unreachable")

    def failing_download(url, dest):
        raise ConnectionError("Network unreachable")

    with pytest.warns(UserWarning):
        report = install_rtk(
            manifest=manifest,
            fetch_checksums_fn=failing_fetch,
            download_fn=failing_download,
        )

    assert any("skipped" in line for line in report)
    assert any("cannot fetch release checksums" in line or "download failed" in line for line in report)


def test_install_rtk_checksum_mismatch_skips(tmp_path, monkeypatch):
    """Checksum mismatch should skip installation."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

    manifest = _load_manifest(MANIFEST)
    platform_key = ki._get_platform_key()
    artifact = manifest["rtk"]["artifacts"][platform_key]
    filename = artifact["filename"]

    # Create fake archive with wrong hash
    fake_archive = tmp_path / filename
    fake_archive.write_bytes(b"wrong content")
    wrong_sha = _sha256_file(fake_archive)

    # Update manifest with wrong hash
    manifest["rtk"]["artifacts"][platform_key]["sha256"] = wrong_sha

    def fake_fetch(url):
        return {filename: "completely_different_hash"}

    def fake_download(url, dest):
        shutil.copy2(fake_archive, dest)

    with pytest.warns(UserWarning):
        report = install_rtk(
            manifest=manifest,
            fetch_checksums_fn=fake_fetch,
            download_fn=fake_download,
        )

    assert any("checksum verification failed" in line for line in report)


def test_install_rtk_caller_local_manifest_not_used(tmp_path, monkeypatch):
    """Caller local named manifest with different sha256 must NOT be picked up by install_rtk."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

    real_manifest = _load_manifest(MANIFEST)
    platform_key = ki._get_platform_key()
    artifact = real_manifest["rtk"]["artifacts"][platform_key]
    filename = artifact["filename"]

    # Define caller local variable named `manifest` with a different sha256
    manifest = _load_manifest(MANIFEST)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_archive = tmp_path / filename
    _create_fake_archive(fake_archive, filename, artifact["archive_format"])
    fake_sha = _sha256_file(fake_archive)
    assert fake_sha != artifact["sha256"], "Fake archive must have different hash than pinned"
    manifest["rtk"]["artifacts"][platform_key]["sha256"] = fake_sha

    def fake_fetch(url):
        return {filename: fake_sha}

    def fake_download(url, dest):
        shutil.copy2(fake_archive, dest)

    # Note: caller defines `manifest` in local scope, but does NOT pass it to install_rtk.
    # install_rtk must refuse the archive that matches only the caller's fake hash.
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", UserWarning)
        report = install_rtk(
            bin_dir=bin_dir,
            fetch_checksums_fn=fake_fetch,
            download_fn=fake_download,
        )

    assert any("checksum verification failed" in line for line in report), (
        f"install_rtk should have refused archive matching caller's fake manifest, got: {report}"
    )
    assert not (bin_dir / "rtk").exists(), "Binary must not be installed when checksum verification fails"


def test_install_rtk_successful_installation(tmp_path, monkeypatch):
    """Successful download and verification should install rtk."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

    manifest = _load_manifest(MANIFEST)
    platform_key = ki._get_platform_key()
    artifact = manifest["rtk"]["artifacts"][platform_key]
    filename = artifact["filename"]
    expected_sha = artifact["sha256"]

    # Create fake archive with correct content
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_archive = tmp_path / filename
    _create_fake_archive(fake_archive, filename, artifact["archive_format"])
    actual_sha = _sha256_file(fake_archive)

    # Update manifest with actual hash
    manifest["rtk"]["artifacts"][platform_key]["sha256"] = actual_sha

    def fake_fetch(url):
        return {filename: actual_sha}

    def fake_download(url, dest):
        shutil.copy2(fake_archive, dest)

    report = install_rtk(
        bin_dir=bin_dir,
        manifest=manifest,
        fetch_checksums_fn=fake_fetch,
        download_fn=fake_download,
    )

    assert any("installed rtk-oss" in line for line in report)
    assert (bin_dir / "rtk").exists()


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


def test_install_kit_all_components(tmp_path, monkeypatch):
    """Full kit installation should install all components."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(shutil, "which", lambda x: None)

    project = tmp_path / "project"
    project.mkdir()

    manifest = _load_manifest(MANIFEST)
    platform_key = ki._get_platform_key()
    artifact = manifest["rtk"]["artifacts"][platform_key]
    filename = artifact["filename"]

    # Create fake archive
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_archive = tmp_path / filename
    _create_fake_archive(fake_archive, filename, artifact["archive_format"])
    actual_sha = _sha256_file(fake_archive)
    manifest["rtk"]["artifacts"][platform_key]["sha256"] = actual_sha

    def fake_fetch(url):
        return {filename: actual_sha}

    def fake_download(url, dest):
        shutil.copy2(fake_archive, dest)

    report = install_kit(
        project_dir=project,
        do_install_rtk=True,
        do_install_superpowers=True,
        do_install_ponytail=True,
        bin_dir=bin_dir,
        manifest=manifest,
        fetch_checksums_fn=fake_fetch,
        download_fn=fake_download,
    )

    # Should have reports from all components
    assert any("rtk" in line.lower() for line in report)
    assert any("superpowers" in line.lower() for line in report)


def test_install_kit_opt_out_rtk(tmp_path, monkeypatch):
    """Opting out of rtk should skip rtk installation."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, do_install_rtk=False)

    assert any("--with-rtk" in line for line in report)


def test_install_kit_opt_out_superpowers(tmp_path):
    """Opting out of superpowers should skip superpowers installation."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, do_install_superpowers=False, do_install_rtk=False)

    assert any("--no-superpowers" in line for line in report)


def test_install_kit_opt_out_ponytail(tmp_path):
    """Opting out of ponytail should skip ponytail installation."""
    project = tmp_path / "project"
    project.mkdir()

    report = install_kit(project_dir=project, do_install_ponytail=False, do_install_rtk=False)

    assert any("--no-ponytail" in line for line in report)


def test_install_kit_dry_run(tmp_path, monkeypatch):
    """Dry run should report without installing."""
    import crew_chief.kit_install as ki
    monkeypatch.setattr(ki.sys, "platform", "linux")
    monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(shutil, "which", lambda x: None)

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


def test_install_py_accepts_no_rtk_flag(tmp_path):
    """install.py should accept --no-rtk flag."""
    project = tmp_path / "project"
    project.mkdir()

    result = _run_install(project, "--no-rtk")

    assert result.returncode == 0, result.stderr


def test_install_py_accepts_no_superpowers_flag(tmp_path):
    """install.py should accept --no-superpowers flag."""
    project = tmp_path / "project"
    project.mkdir()

    result = _run_install(project, "--no-superpowers", "--no-rtk")

    assert result.returncode == 0, result.stderr


def test_install_machine_wide_accepts_opt_outs(tmp_path):
    """install_machine_wide.py should accept all opt-out flags."""
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, "-I", str(INSTALL_MW_PY), "--dry-run", "--home", str(home),
         "--no-rtk", "--no-superpowers", "--no-ponytail"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr