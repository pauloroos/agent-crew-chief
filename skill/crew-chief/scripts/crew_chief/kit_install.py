"""Base kit installer: ponytail and superpowers.

Copies the vendored skills into a project (or a home directory) so every
brief can tell the agents to use them; ``--no-ponytail`` and
``--no-superpowers`` skip each one. Nothing is downloaded.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

# --- constants ---------------------------------------------------------------

MANIFEST_PATH = Path(__file__).resolve().parent / "kit_manifest.json"


def _load_manifest(path: Path | None = None) -> dict[str, Any]:
    """Load the kit manifest JSON."""
    manifest_path = path or MANIFEST_PATH
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Kit manifest not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))



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
                do_install_superpowers: bool = True,
                do_install_ponytail: bool = True,
                dry_run: bool = False,
                home: Path | None = None,
                manifest: dict | None = None) -> list[str]:
    """Install ponytail and superpowers; return the one-line kit summary.

    ``project_dir=None`` installs under ``home`` (default: the user's home).
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

    return [f"kit: ponytail {pt_status}, superpowers {sp_status}"]
