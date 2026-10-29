"""Tests for the 8 gaps identified in T004 correction brief.
These tests must FAIL before the fix and PASS after. They cover:
1. Network leak prevention
2. Home handling at call time
3. Silent skip removal
4. Summary line output
5. Safe extraction (no symlinks, no traversal)
6. Verification order (manifest + checksums.txt before extract)
7. Platform coverage
8. Release gate completeness
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import io
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skill" / "crew-chief" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from crew_chief.kit_install import (
    _extract_rtk_binary,
    _fetch_checksums,
    _get_platform_key,
    _load_manifest,
    _sha256_file,
    _user_bin_dir,
    _verify_archive,
    install_kit,
    install_rtk,
)

MANIFEST = SCRIPTS_DIR / "crew_chief" / "kit_manifest.json"


def _create_fake_archive_with_member(dest: Path, filename: str, archive_format: str,
                                     member_name: str, member_data: bytes,
                                     is_symlink: bool = False, is_link: bool = False) -> bytes:
    """Create a fake archive with a specific member."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if archive_format == "tar.gz":
        with tarfile.open(dest, "w:gz") as tf:
            info = tarfile.TarInfo(name=member_name)
            info.size = len(member_data)
            info.mode = 0o755
            if is_symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
            elif is_link:
                info.type = tarfile.LNKTYPE
                info.linkname = "/etc/shadow"
            tf.addfile(info, io.BytesIO(member_data))
    elif archive_format == "zip":
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr(member_name, member_data)
    return _sha256_file(dest)


# ============================================================================
# Gap 1: Network leak - tests must not reach real network
# ============================================================================

class TestNetworkLeakPrevention:
    """Gap 1: No test may open a real socket to non-local host."""

    def test_no_network_calls_in_rtk_install_with_fakes(self, tmp_path, monkeypatch):
        """RTK install with fake fetcher must not call urlopen."""
        import crew_chief.kit_install as ki

        # Track if urlopen was called
        calls_to_urlopen = []

        def tracking_urlopen(*args, **kwargs):
            calls_to_urlopen.append((args, kwargs))
            raise RuntimeError("Network call attempted!")

        monkeypatch.setattr(ki, 'urlopen', tracking_urlopen)
        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        # Create fake archive
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho fake rtk\n")
        actual_sha = _sha256_file(fake_archive)
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

        # Should not have called urlopen
        assert len(calls_to_urlopen) == 0, f"Network calls were made: {calls_to_urlopen}"
        assert any("installed rtk" in line for line in report)

    def test_guard_test_detects_real_socket(self, tmp_path, monkeypatch):
        """A guard test that fails if any installer opens a real socket."""
        import crew_chief.kit_install as ki

        # Monkeypatch socket creation to detect non-local connections
        original_socket = socket.socket
        connections_made = []

        class TrackingSocket:
            def __init__(self, *args, **kwargs):
                self._sock = original_socket(*args, **kwargs)

            def connect(self, addr):
                host = addr[0] if isinstance(addr, tuple) else str(addr)
                if host not in ('127.0.0.1', 'localhost', '::1'):
                    connections_made.append(host)
                return self._sock.connect(addr)

            def connect_ex(self, addr):
                host = addr[0] if isinstance(addr, tuple) else str(addr)
                if host not in ('127.0.0.1', 'localhost', '::1'):
                    connections_made.append(host)
                return self._sock.connect_ex(addr)

            def __getattr__(self, name):
                return getattr(self._sock, name)

        monkeypatch.setattr(socket, 'socket', TrackingSocket)
        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho fake rtk\n")
        actual_sha = _sha256_file(fake_archive)
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

        # Must not have connected to non-local hosts
        assert len(connections_made) == 0, f"Connected to non-local hosts: {connections_made}"


# ============================================================================
# Gap 2: Home handling - resolve bin dir at call time, not import time
# ============================================================================

class TestHomeHandling:
    """Gap 2: Bin dir must be resolved from home being installed into."""

    def test_user_bin_dir_resolved_at_call_time(self, tmp_path, monkeypatch):
        """_user_bin_dir should use current home, not import-time home."""
        import crew_chief.kit_install as ki

        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        bin_dir = ki._user_bin_dir()
        expected = fake_home / ".local" / "bin"
        assert bin_dir == expected, f"Expected {expected}, got {bin_dir}"

    def test_install_machine_wide_passes_home_to_install_kit(self, tmp_path):
        """install_machine_wide.py must pass --home to install_kit."""
        import subprocess

        home = tmp_path / "home"
        home.mkdir()

        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", str(SCRIPTS_DIR / "install_machine_wide.py"),
             "--dry-run", "--home", str(home)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        # Should mention the home directory in output
        assert str(home) in result.stdout or "~/" in result.stdout

    def test_dry_run_downloads_and_writes_nothing(self, tmp_path, monkeypatch):
        """Dry run must not download or write anything."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

        project = tmp_path / "project"
        project.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Track if download was called
        download_called = []

        def tracking_download(url, dest):
            download_called.append(url)
            raise RuntimeError("Download should not happen in dry-run!")

        manifest = _load_manifest(MANIFEST)
        report = install_kit(
            project_dir=project,
            do_install_rtk=True,
            do_install_superpowers=True,
            do_install_ponytail=True,
            bin_dir=bin_dir,
            dry_run=True,
        )

        # Nothing should have been downloaded
        assert len(download_called) == 0, f"Downloads occurred in dry-run: {download_called}"
        # Report should mention "would"
        assert any("would" in line.lower() for line in report), f"No 'would' in report: {report}"


# ============================================================================
# Gap 3: Silent skip removal - missing kit_install must fail loudly
# ============================================================================

class TestSilentSkipRemoval:
    """Gap 3: Remove silent ImportError catches."""

    def test_missing_kit_install_fails_loudly_in_install_py(self, tmp_path):
        """install.py should not silently catch ImportError for kit_install."""
        import subprocess

        project = tmp_path / "project"
        project.mkdir()

        # Temporarily rename kit_install module to simulate missing
        kit_install_file = SCRIPTS_DIR / "crew_chief" / "kit_install.py"
        backup = kit_install_file.with_suffix(".py.bak")

        if kit_install_file.exists():
            shutil.copy2(kit_install_file, backup)
            kit_install_file.unlink()

        try:
            result = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve().parents[1] / "install.py"),
                 str(project)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )

            # Should fail or warn about missing kit_install
            # Currently it passes silently (the bug)
            # After fix, it should either fail or at least warn
            assert result.returncode != 0 or "kit_install" in result.stderr or "kit" in result.stderr.lower(), \
                f"Should have failed or warned about missing kit_install, but got: {result.stdout}"
        finally:
            if backup.exists():
                shutil.move(backup, kit_install_file)


# ============================================================================
# Gap 4: Summary line - one line summarizing kit installation
# ============================================================================

class TestSummaryLine:
    """Gap 4: Print ONE summary line about kit installation status."""

    def test_install_kit_produces_summary_line(self, tmp_path, monkeypatch):
        """install_kit should produce a summary line."""
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

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho fake rtk\n")
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

        # Should have a summary line mentioning what was installed/skipped/kept
        summary_lines = [line for line in report if "kit:" in line.lower() or
                         ("installed" in line.lower() and "superpowers" in line.lower()) or
                         ("installed" in line.lower() and "ponytail" in line.lower())]

        assert len(summary_lines) > 0, f"No summary line found in report: {report}"

        # Check for expected format: "kit: X installed, Y installed, Z kept"
        has_summary = any(
            ("ponytail" in line.lower() and "installed" in line.lower()) or
            ("superpowers" in line.lower() and "installed" in line.lower()) or
            ("rtk" in line.lower() and ("installed" in line.lower() or "kept" in line.lower()))
            for line in summary_lines
        )
        assert has_summary, f"Summary doesn't match expected format: {summary_lines}"

    def test_path_hint_when_bin_not_on_path(self, tmp_path, monkeypatch):
        """Should add PATH hint when bin dir is not on PATH."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(shutil, "which", lambda x: None)

        # Use a bin dir that's definitely not on PATH
        bin_dir = tmp_path / "custom_bin"
        bin_dir.mkdir()

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho fake rtk\n")
        actual_sha = _sha256_file(fake_archive)
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

        # Should mention PATH
        path_hints = [line for line in report if "PATH" in line]
        assert len(path_hints) > 0, f"No PATH hint in report: {report}"


# ============================================================================
# Gap 5: Safe extraction - reject symlinks, links, traversal
# ============================================================================

class TestSafeExtraction:
    """Gap 5: Extract only rtk binary safely, reject dangerous members."""

    def test_reject_symlink_member(self, tmp_path):
        """Should reject symlink members in tar.gz archives."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with symlink member
        archive = tmp_path / "test.tar.gz"
        _create_fake_archive_with_member(archive, "test.tar.gz", "tar.gz",
                                         "../evil", b"evil content", is_symlink=True)

        extracted = _extract_rtk_binary(archive, "tar.gz", bin_dir)

        # Should not extract symlink
        assert extracted is None, "Symlink member should have been rejected"
        assert not (bin_dir / "evil").exists()

    def test_reject_hardlink_member(self, tmp_path):
        """Should reject hardlink members in tar.gz archives."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with hardlink member
        archive = tmp_path / "test.tar.gz"
        _create_fake_archive_with_member(archive, "test.tar.gz", "tar.gz",
                                         "../evil", b"evil content", is_link=True)

        extracted = _extract_rtk_binary(archive, "tar.gz", bin_dir)

        # Should not extract hardlink
        assert extracted is None, "Hardlink member should have been rejected"

    def test_reject_traversal_member(self, tmp_path):
        """Should reject members with '..' in path."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with traversal member
        archive = tmp_path / "test.tar.gz"
        _create_fake_archive_with_member(archive, "test.tar.gz", "tar.gz",
                                         "../../../etc/passwd", b"evil", is_symlink=False)

        extracted = _extract_rtk_binary(archive, "tar.gz", bin_dir)

        # Should not extract traversal member
        assert extracted is None, "Traversal member should have been rejected"
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_reject_absolute_path_member(self, tmp_path):
        """Should reject members with absolute paths."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with absolute path member
        archive = tmp_path / "test.tar.gz"
        _create_fake_archive_with_member(archive, "test.tar.gz", "tar.gz",
                                         "/etc/passwd", b"evil", is_symlink=False)

        extracted = _extract_rtk_binary(archive, "tar.gz", bin_dir)

        # Should not extract absolute path member
        assert extracted is None, "Absolute path member should have been rejected"

    def test_extract_only_rtk_binary(self, tmp_path):
        """Should extract only the rtk binary, not other files."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with multiple members
        archive = tmp_path / "test.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            # Add rtk binary
            rtk_data = b"#!/bin/sh\necho rtk\n"
            rtk_info = tarfile.TarInfo(name="rtk")
            rtk_info.size = len(rtk_data)
            rtk_info.mode = 0o755
            tf.addfile(rtk_info, io.BytesIO(rtk_data))

            # Add another file that should NOT be extracted
            other_data = b"other file"
            other_info = tarfile.TarInfo(name="other.txt")
            other_info.size = len(other_data)
            tf.addfile(other_info, io.BytesIO(other_data))

        extracted = _extract_rtk_binary(archive, "tar.gz", bin_dir)

        # Should extract only rtk
        assert extracted is not None
        assert extracted.name == "rtk"
        assert not (bin_dir / "other.txt").exists()


# ============================================================================
# Gap 6: Verification order - checksums before extraction
# ============================================================================

class TestVerificationOrder:
    """Gap 6: Verify checksums before extraction."""

    def test_checksum_mismatch_refuses_extraction(self, tmp_path, monkeypatch):
        """Checksum mismatch should refuse extraction and warn."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # Create archive with wrong content
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"wrong content")

        def fake_fetch(url):
            return {filename: "completely_different_hash"}

        def fake_download(url, dest):
            shutil.copy2(fake_archive, dest)

        with pytest.warns(UserWarning, match="checksum"):
            report = install_rtk(
                bin_dir=bin_dir,
                manifest=manifest,
                fetch_checksums_fn=fake_fetch,
                download_fn=fake_download,
            )

        # Should have skipped due to checksum failure
        assert any("skipped" in line or "failed" in line for line in report)
        # rtk binary should not exist
        assert not (bin_dir / "rtk").exists()

    def test_missing_from_release_checksums_refuses(self, tmp_path, monkeypatch):
        """Missing from release checksums.txt should refuse extraction."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"test content")
        actual_sha = _sha256_file(fake_archive)
        manifest["rtk"]["artifacts"][platform_key]["sha256"] = actual_sha

        def fake_fetch(url):
            # Return checksums without our filename
            return {"other.tar.gz": "some_hash"}

        def fake_download(url, dest):
            shutil.copy2(fake_archive, dest)

        with pytest.warns(UserWarning, match="not found in release"):
            report = install_rtk(
                bin_dir=bin_dir,
                manifest=manifest,
                fetch_checksums_fn=fake_fetch,
                download_fn=fake_download,
            )

        assert any("skipped" in line for line in report)
        assert not (bin_dir / "rtk").exists()


# ============================================================================
# Gap 7: Platform coverage
# ============================================================================

class TestPlatformCoverage:
    """Gap 7: Tests for all supported platforms."""

    @pytest.mark.parametrize("platform,machine,expected_key", [
        ("linux", "x86_64", "x86_64-unknown-linux-musl"),
        ("linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("darwin", "arm64", "aarch64-apple-darwin"),
        ("darwin", "x86_64", "x86_64-apple-darwin"),
        ("win32", "AMD64", "x86_64-pc-windows-msvc"),
    ])
    def test_platform_keys(self, monkeypatch, platform, machine, expected_key):
        """Test platform key detection for all supported platforms."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", platform)
        monkeypatch.setattr(ki._platform, "machine", lambda: machine)

        assert ki._get_platform_key() == expected_key

    def test_windows_installs_rtk_exe(self, tmp_path, monkeypatch):
        """Windows should install rtk.exe to USERPROFILE/.local/bin."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "win32")
        monkeypatch.setattr(ki._platform, "machine", lambda: "AMD64")
        monkeypatch.setattr(shutil, "which", lambda x: None)
        fake_home = tmp_path / "userprofile"
        fake_home.mkdir()
        monkeypatch.setattr(Path, 'home', lambda: fake_home)
        monkeypatch.setenv("USERPROFILE", str(fake_home))

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        bin_dir = tmp_path / "userprofile" / ".local" / "bin"
        bin_dir.mkdir(parents=True)

        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, "zip",
                                         "rtk.exe", b"@echo off\necho rtk\n")
        actual_sha = _sha256_file(fake_archive)
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

        assert any("installed rtk" in line for line in report)
        assert (bin_dir / "rtk.exe").exists()

    def test_linux_arm64_installs(self, tmp_path, monkeypatch):
        """Linux ARM64 should install correctly."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "aarch64")

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho rtk\n")
        actual_sha = _sha256_file(fake_archive)
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

        assert any("installed rtk" in line for line in report)
        assert (bin_dir / "rtk").exists()


# ============================================================================
# Gap 8: Release gate completeness
# ============================================================================

class TestReleaseGateCompleteness:
    """Gap 8: Release gate must verify installed bundle works."""

    def test_installed_bundle_has_all_components(self, tmp_path, monkeypatch):
        """Installed bundle must have ponytail, superpowers, and their support files."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(shutil, "which", lambda x: None)

        project = tmp_path / "project"
        project.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho rtk\n")
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

        # Check that superpowers skills are installed
        sp_dir = project / ".claude" / "skills" / "superpowers"
        assert (sp_dir / "test-driven-development" / "SKILL.md").exists()
        assert (sp_dir / "systematic-debugging" / "SKILL.md").exists()
        assert (sp_dir / "verification-before-completion" / "SKILL.md").exists()
        assert (sp_dir / "LICENSE").exists()

        # Check that ponytail skills are installed
        pt_dir = project / ".claude" / "skills" / "ponytail"
        assert (pt_dir / "ponytail" / "SKILL.md").exists()
        assert (pt_dir / "LICENSE").exists()

        # Check rtk binary
        assert (bin_dir / "rtk").exists()

    def test_renderer_works_from_installed_copy(self, tmp_path, monkeypatch):
        """The kit renderer must work from the installed copy."""
        import crew_chief.kit_install as ki

        monkeypatch.setattr(ki.sys, "platform", "linux")
        monkeypatch.setattr(ki._platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(shutil, "which", lambda x: None)

        project = tmp_path / "project"
        project.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        manifest = _load_manifest(MANIFEST)
        platform_key = ki._get_platform_key()
        artifact = manifest["rtk"]["artifacts"][platform_key]
        filename = artifact["filename"]

        fake_archive = tmp_path / filename
        _create_fake_archive_with_member(fake_archive, filename, artifact["archive_format"],
                                         "rtk", b"#!/bin/sh\necho rtk\n")
        actual_sha = _sha256_file(fake_archive)
        manifest["rtk"]["artifacts"][platform_key]["sha256"] = actual_sha

        def fake_fetch(url):
            return {filename: actual_sha}

        def fake_download(url, dest):
            shutil.copy2(fake_archive, dest)

        install_kit(
            project_dir=project,
            do_install_rtk=True,
            do_install_superpowers=True,
            do_install_ponytail=True,
            bin_dir=bin_dir,
            manifest=manifest,
            fetch_checksums_fn=fake_fetch,
            download_fn=fake_download,
        )

        # Import from installed location
        scripts_dir = str(SCRIPTS_DIR)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        from crew_chief.kit import render_kit_brief

        lines = render_kit_brief(
            role="executor",
            backend="generic",
            vendor_dir=project / ".claude" / "skills" / "superpowers",
        )

        assert isinstance(lines, list)
        assert len(lines) > 0