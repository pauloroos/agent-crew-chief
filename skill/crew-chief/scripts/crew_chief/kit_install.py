"""Base kit installer: ponytail, rtk-oss, and superpowers.

Installs the complete base kit into a project or machine-wide location,
supporting opt-in rtk (--with-rtk), opt-outs (--no-superpowers, --no-ponytail) and
dry-run mode. RTK installation verifies SHA-256 checksums against both
the pinned manifest and release checksums.txt before extraction.

Network or verification failures produce non-fatal warnings without
failing ACC installation. Tests use fake release server fixtures, never
real network.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import shutil
import sys
import tarfile
import tempfile
import warnings
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request

# --- constants ---------------------------------------------------------------

MANIFEST_PATH = Path(__file__).resolve().parent / "kit_manifest.json"

RTK_LICENSE = "Apache-2.0"
SUPERPOWERS_LICENSE = "MIT"


def _user_bin_dir(home: Path | None = None) -> Path:
    """Return the user bin directory for the current platform, resolved at call time."""
    if home is not None:
        return Path(home).expanduser() / ".local" / "bin"
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE")
        base = Path(user_profile) if user_profile else Path.home()
        return base / ".local" / "bin"
    return Path.home() / ".local" / "bin"


def __getattr__(name: str) -> Any:
    if name in ("LINUX_BIN_DIR", "MACOS_BIN_DIR", "WINDOWS_BIN_DIR"):
        return _user_bin_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load the kit manifest JSON."""
    manifest_path = path or MANIFEST_PATH
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Kit manifest not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, timeout: int = 30) -> None:
    """Download a file from URL to dest with timeout."""
    req = Request(url, headers={"User-Agent": "agent-crew-chief/kit-installer"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _fetch_checksums(checksums_url: str, timeout: int = 30) -> dict[str, str]:
    """Fetch and parse release checksums.txt into {filename: sha256}."""
    req = Request(checksums_url, headers={"User-Agent": "agent-crew-chief/kit-installer"})
    with urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            checksums[parts[1]] = parts[0]
    return checksums


def _get_platform_key() -> str:
    """Return the platform key matching kit_manifest.json artifact keys."""
    system = sys.platform
    arch = _platform.machine().lower()

    if system == "win32":
        if arch in ("amd64", "x86_64"):
            return "x86_64-pc-windows-msvc"
        raise ValueError(f"Unsupported Windows architecture: {arch}")

    if system == "darwin":
        if arch in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if arch in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
        raise ValueError(f"Unsupported macOS architecture: {arch}")

    # Linux
    if arch in ("arm64", "aarch64"):
        return "aarch64-unknown-linux-gnu"
    if arch in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-musl"
    raise ValueError(f"Unsupported Linux architecture: {arch}")


def _verify_archive(archive_path: Path, expected_sha256: str, filename: str,
                    checksums: dict[str, str]) -> bool:
    """Verify archive SHA-256 against manifest pin AND release checksums."""
    actual = _sha256_file(archive_path)

    # Check against manifest pin
    if actual != expected_sha256:
        warnings.warn(
            f"Archive checksum mismatch: expected {expected_sha256}, "
            f"got {actual} (manifest pin)",
            UserWarning,
        )
        return False

    # Check against release checksums.txt
    if filename not in checksums:
        warnings.warn(
            f"Filename {filename!r} not found in release checksums.txt",
            UserWarning,
        )
        return False

    if checksums[filename] != expected_sha256:
        warnings.warn(
            f"Release checksums.txt mismatch for {filename}: "
            f"manifest says {expected_sha256}, release says {checksums[filename]}",
            UserWarning,
        )
        return False

    return True


def _is_rtk_on_path() -> bool:
    """Return True if an rtk binary is already on PATH."""
    return shutil.which("rtk") is not None


def _extract_rtk_binary(archive_path: Path, archive_format: str,
                        dest_dir: Path) -> Path | None:
    """Extract the rtk executable from the archive into dest_dir safely.

    Reads member bytes directly without using tf.extract / zf.extract.
    Rejects archives containing symlinks, links, or path traversal members.
    Returns path to extracted binary, or None on failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        if archive_format == "tar.gz":
            with tarfile.open(archive_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if member.issym() or member.islnk():
                        return None
                    name_parts = member.name.replace("\\", "/").split("/")
                    if ".." in name_parts or member.name.startswith("/") or os.path.isabs(member.name):
                        return None

                target_member = None
                for member in tf.getmembers():
                    basename = Path(member.name).name
                    if basename in ("rtk", "rtk.exe") and member.isfile():
                        target_member = member
                        break

                if target_member is None:
                    return None

                f = tf.extractfile(target_member)
                if f is None:
                    return None
                data = f.read()
                dest_file = dest_dir / ("rtk.exe" if target_member.name.endswith(".exe") else "rtk")
                dest_file.write_bytes(data)
                dest_file.chmod(0o755)
                return dest_file

        elif archive_format == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    name_parts = info.filename.replace("\\", "/").split("/")
                    if ".." in name_parts or info.filename.startswith("/") or os.path.isabs(info.filename):
                        return None
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        return None

                target_info = None
                for info in zf.infolist():
                    basename = Path(info.filename).name
                    if basename in ("rtk.exe", "rtk") and not info.is_dir():
                        target_info = info
                        break

                if target_info is None:
                    return None

                data = zf.read(target_info)
                dest_file = dest_dir / ("rtk.exe" if target_info.filename.endswith(".exe") else "rtk")
                dest_file.write_bytes(data)
                if sys.platform != "win32":
                    dest_file.chmod(0o755)
                return dest_file

    except Exception as exc:
        warnings.warn(f"Failed to extract rtk binary: {exc}", UserWarning)
        return None

    return None


def install_rtk(bin_dir: Path | None = None, dry_run: bool = False,
                manifest: dict | None = None,
                fetch_checksums_fn=None, download_fn=None,
                home: Path | None = None) -> list[str]:
    """Install pinned rtk-oss v1.1.0 Apache-2.0 edition.

    If rtk is already on PATH via shutil.which, leave it untouched.
    Otherwise download ONLY the pinned release archive for the current
    platform, verify SHA-256 against both manifest pin and release
    checksums.txt, then extract the executable to bin_dir.

    Network or verification failures produce warnings but do not fail
    installation.

    Args:
        bin_dir: Target directory for the binary (default: user bin dir).
        dry_run: If True, report what would be done without downloading.
        manifest: Pre-loaded manifest (for testing).
        fetch_checksums_fn: Override for fetching checksums (testing).
        download_fn: Override for downloading archives (testing).
        home: Custom home directory (for testing / machine-wide).

    Returns:
        List of report lines.
    """
    report: list[str] = []

    # Check if rtk already exists on PATH
    if _is_rtk_on_path():
        report.append("rtk: already on PATH, left untouched")
        return report

    # Load manifest
    if manifest is None:
        try:
            manifest = _load_manifest()
        except FileNotFoundError as exc:
            warnings.warn(f"Cannot load kit manifest: {exc}", UserWarning)
            report.append("rtk: skipped (manifest not found)")
            return report

    rtk_config = manifest.get("rtk", {})
    if not rtk_config:
        warnings.warn("No rtk configuration in manifest", UserWarning)
        report.append("rtk: skipped (no rtk config in manifest)")
        return report

    version = rtk_config.get("version", "1.1.0")
    base_url = rtk_config.get("base_url", "")
    checksums_url = rtk_config.get("checksums_url", "")
    artifacts = rtk_config.get("artifacts", {})

    # Get platform-specific artifact
    try:
        platform_key = _get_platform_key()
    except ValueError as exc:
        warnings.warn(str(exc), UserWarning)
        report.append(f"rtk: skipped ({exc})")
        return report

    artifact = artifacts.get(platform_key)
    if not artifact:
        report.append(f"rtk: skipped (no artifact for platform {platform_key})")
        return report

    filename = artifact["filename"]
    archive_format = artifact["archive_format"]
    expected_sha256 = artifact["sha256"]
    download_url = f"{base_url}/{filename}" if base_url else filename

    target_dir = bin_dir or _user_bin_dir(home)

    if dry_run:
        report.append(f"would download rtk-oss {version} for {platform_key}")
        report.append(f"would install to {target_dir}")
        return report

    # Fetch release checksums
    if fetch_checksums_fn is not None:
        try:
            release_checksums = fetch_checksums_fn(checksums_url)
        except Exception as exc:
            warnings.warn(f"Failed to fetch release checksums: {exc}", UserWarning)
            report.append("rtk: skipped (cannot fetch release checksums)")
            return report
    else:
        try:
            release_checksums = _fetch_checksums(checksums_url)
        except Exception as exc:
            warnings.warn(f"Failed to fetch release checksums: {exc}", UserWarning)
            report.append("rtk: skipped (cannot fetch release checksums)")
            return report

    # Download archive
    archive_tmp = Path(tempfile.gettempdir()) / f"rtk-{filename}"
    if download_fn is not None:
        try:
            download_fn(download_url, archive_tmp)
        except Exception as exc:
            warnings.warn(f"Failed to download rtk archive: {exc}", UserWarning)
            report.append("rtk: skipped (download failed)")
            return report
    else:
        try:
            _download(download_url, archive_tmp)
        except Exception as exc:
            warnings.warn(f"Failed to download rtk archive: {exc}", UserWarning)
            report.append("rtk: skipped (download failed)")
            return report

    # Verify checksums
    if not _verify_archive(archive_tmp, expected_sha256, filename, release_checksums):
        archive_tmp.unlink(missing_ok=True)
        report.append("rtk: skipped (checksum verification failed)")
        return report

    # Extract binary
    extracted = _extract_rtk_binary(archive_tmp, archive_format, target_dir)
    archive_tmp.unlink(missing_ok=True)

    if extracted is None:
        report.append("rtk: skipped (extraction failed)")
        return report

    report.append(f"installed rtk-oss {version} to {extracted}")

    # PATH advice
    if str(target_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        report.append(
            f"add {target_dir} to your PATH to use `rtk` "
            "(nothing was changed for you)"
        )

    return report


def install_superpowers(dest_dir: Path, dry_run: bool = False,
                        manifest: dict | None = None,
                        source_dir: Path | None = None) -> list[str]:
    """Install vendored superpowers skills to dest_dir.

    Copies the three selected skills (test-driven-development,
    systematic-debugging, verification-before-completion) along with
    LICENSE and VERSION files.

    Args:
        dest_dir: Target directory for superpowers skills.
        dry_run: If True, report what would be done without copying.
        manifest: Pre-loaded manifest (for testing).
        source_dir: Source directory override (for testing).

    Returns:
        List of report lines.
    """
    report: list[str] = []

    if manifest is None:
        try:
            manifest = _load_manifest()
        except FileNotFoundError:
            report.append("superpowers: skipped (manifest not found)")
            return report

    sp_config = manifest.get("superpowers", {})
    skills = sp_config.get("skills", [])

    # Determine source directory
    if source_dir is not None:
        src = source_dir
    else:
        # Default: vendor/superpowers relative to repo root
        repo_root = Path(__file__).resolve().parents[4]
        src = repo_root / "vendor" / "superpowers"

    if not src.is_dir():
        report.append("superpowers: skipped (source directory not found)")
        return report

    dest = Path(dest_dir)

    if dry_run:
        report.append(f"would install superpowers skills to {dest}")
        for skill in skills:
            report.append(f"would copy skill: {skill}")
        return report

    # Create destination
    dest.mkdir(parents=True, exist_ok=True)

    # Copy LICENSE
    license_src = src / "LICENSE"
    if license_src.is_file():
        shutil.copy2(license_src, dest / "LICENSE")
        report.append(f"copied superpowers LICENSE to {dest / 'LICENSE'}")

    # Copy VERSION
    version_src = src / "VERSION"
    if version_src.is_file():
        shutil.copy2(version_src, dest / "VERSION")
        report.append(f"copied superpowers VERSION to {dest / 'VERSION'}")

    # Copy skills
    skills_src = src / "skills"
    if not skills_src.is_dir():
        report.append("superpowers: skipped (skills directory not found)")
        return report

    for skill_name in skills:
        skill_src = skills_src / skill_name
        skill_dest = dest / skill_name
        if skill_src.is_dir():
            if skill_dest.exists():
                shutil.rmtree(skill_dest)
            shutil.copytree(skill_src, skill_dest)
            report.append(f"installed superpowers skill: {skill_name}")
        else:
            report.append(f"superpowers: skill {skill_name} not found in source")

    return report


def install_ponytail(dest_dir: Path, dry_run: bool = False,
                     source_dir: Path | None = None) -> list[str]:
    """Install vendored ponytail skills to dest_dir.

    Args:
        dest_dir: Target directory for ponytail skills.
        dry_run: If True, report what would be done without copying.
        source_dir: Source directory override (for testing).

    Returns:
        List of report lines.
    """
    report: list[str] = []

    # Determine source directory
    if source_dir is not None:
        src = source_dir
    else:
        repo_root = Path(__file__).resolve().parents[4]
        src = repo_root / "vendor" / "ponytail"

    if not src.is_dir():
        report.append("ponytail: skipped (source directory not found)")
        return report

    dest = Path(dest_dir)

    if dry_run:
        report.append(f"would install ponytail to {dest}")
        return report

    # Create destination
    dest.mkdir(parents=True, exist_ok=True)

    # Copy ponytail skills
    skills_src = src / "skills"
    if skills_src.is_dir():
        for item in skills_src.iterdir():
            if item.is_dir():
                skill_dest = dest / item.name
                if skill_dest.exists():
                    shutil.rmtree(skill_dest)
                shutil.copytree(item, skill_dest)
                report.append(f"installed ponytail skill: {item.name}")

    # Copy LICENSE
    license_src = src / "LICENSE"
    if license_src.is_file():
        shutil.copy2(license_src, dest / "LICENSE")
        report.append(f"copied ponytail LICENSE to {dest / 'LICENSE'}")

    # Copy VERSION
    version_src = src / "VERSION"
    if version_src.is_file():
        shutil.copy2(version_src, dest / "VERSION")
        report.append(f"copied ponytail VERSION to {dest / 'VERSION'}")

    return report


def install_kit(project_dir: Path | None = None,
                do_install_rtk: bool = True,
                do_install_superpowers: bool = True,
                do_install_ponytail: bool = True,
                dry_run: bool = False,
                bin_dir: Path | None = None,
                home: Path | None = None,
                manifest: dict | None = None,
                fetch_checksums_fn=None,
                download_fn=None) -> list[str]:
    """Install the complete base kit and return summary report lines.

    Emits ONE line summarising the kit status, plus a PATH hint line
    only when the bin dir is not on PATH.

    Args:
        project_dir: Project directory for skill installation (optional).
        do_install_rtk: Whether to install rtk.
        do_install_superpowers: Whether to install superpowers.
        do_install_ponytail: Whether to install ponytail.
        dry_run: If True, report what would be done without installing.
        bin_dir: Custom bin directory for rtk (default: user bin dir).
        home: Custom home directory (default: current user's home).
        manifest: Pre-loaded manifest (for testing).
        fetch_checksums_fn: Override for fetching checksums (testing).
        download_fn: Override for downloading archives (testing).

    Returns:
        List containing the summary line, and optionally a PATH hint line.
    """
    home_dir = Path(home).expanduser() if home else Path.home()
    skills_base = project_dir if project_dir is not None else home_dir

    # 1. Ponytail
    if not do_install_ponytail:
        pt_status = "skipped (--no-ponytail)"
    else:
        pt_dest = skills_base / ".claude" / "skills" / "ponytail"
        pt_report = install_ponytail(pt_dest, dry_run=dry_run)
        if dry_run:
            pt_status = "would install"
        elif any("installed ponytail skill" in line or "copied ponytail" in line for line in pt_report):
            pt_status = "installed"
        elif any("skipped" in line for line in pt_report):
            pt_status = "warning (source not found)"
        else:
            pt_status = "installed"

    # 2. Superpowers
    if not do_install_superpowers:
        sp_status = "skipped (--no-superpowers)"
    else:
        sp_dest = skills_base / ".claude" / "skills" / "superpowers"
        sp_report = install_superpowers(sp_dest, dry_run=dry_run, manifest=manifest)
        if dry_run:
            sp_status = "would install"
        elif any("installed superpowers skill" in line or "copied superpowers" in line for line in sp_report):
            sp_status = "installed"
        elif any("skipped" in line for line in sp_report):
            sp_status = "warning (source not found)"
        else:
            sp_status = "installed"

    # 3. RTK
    target_bin = bin_dir or _user_bin_dir(home)
    path_hint_needed = False
    if not do_install_rtk:
        rtk_status = "skipped (pass --with-rtk to install)"
    else:
        rtk_report = install_rtk(
            bin_dir=bin_dir,
            dry_run=dry_run,
            manifest=manifest,
            fetch_checksums_fn=fetch_checksums_fn,
            download_fn=download_fn,
            home=home,
        )
        if any("already on PATH" in line for line in rtk_report):
            rtk_status = "kept (already on PATH)"
        elif dry_run:
            rtk_status = "would install"
            if str(target_bin) not in os.environ.get("PATH", "").split(os.pathsep):
                path_hint_needed = True
        elif any("installed rtk" in line for line in rtk_report):
            rtk_status = "installed"
            if str(target_bin) not in os.environ.get("PATH", "").split(os.pathsep):
                path_hint_needed = True
        else:
            reasons = [line for line in rtk_report if "skipped" in line]
            reason_msg = "download/verification failed"
            if reasons:
                r = reasons[0]
                if "(" in r and ")" in r:
                    reason_msg = r[r.find("(") + 1 : r.rfind(")")]
            rtk_status = f"warning ({reason_msg})"

    summary = f"kit: ponytail {pt_status}, superpowers {sp_status}, rtk {rtk_status}"
    report = [summary]
    if path_hint_needed:
        report.append(
            f"add {target_bin} to your PATH to use `rtk` "
            "(nothing was changed for you)"
        )

    return report