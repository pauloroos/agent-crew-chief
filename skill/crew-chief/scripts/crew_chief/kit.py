"""Base kit renderer and resolver (ponytail, rtk, superpowers).

Resolves user/project preferences and runtime flags, injects prompt/brief
additions directly at launch boundaries, and records resolved skills in attempts.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path
from typing import Any

from .brief import PONYTAIL_REVIEW_LINE, _style_section

RTK_VALUES = ("auto", "on", "off")

ALLOWED_SUPERPOWERS = (
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
)

SKILL_SUPPORT_FILES: dict[str, list[str]] = {
    "test-driven-development": ["writing-good-tests.md"],
    "systematic-debugging": [
        "root-cause-tracing.md",
        "defense-in-depth.md",
        "condition-based-waiting.md",
        "find-polluter.sh",
        "condition-based-waiting-example.ts",
    ],
    "verification-before-completion": [],
}

SKILL_DEPENDENCIES: dict[str, list[str]] = {
    "test-driven-development": [],
    "systematic-debugging": [
        "test-driven-development",
        "verification-before-completion",
    ],
    "verification-before-completion": [],
}

_WARNED_MESSAGES: set[str] = set()


def reset_kit_warnings() -> None:
    """Reset the process-level warning deduplication state (useful for test isolation)."""
    _WARNED_MESSAGES.clear()



def detect_rtk(pref: str, path_lookup: Any = None) -> tuple[bool, str | None]:
    """Check rtk preference and binary presence on PATH.

    Returns ``(active, warning)``.
    - ``auto`` with a binary: active, no warning.
    - ``auto`` without a binary: inactive, no warning.
    - ``off``: inactive, no warning.
    - ``on`` without a binary: inactive, one warning.
    - ``on`` with a binary: active, no warning.
    """
    if path_lookup is None:
        path_lookup = shutil.which
    if pref not in RTK_VALUES:
        raise ValueError(
            f"Invalid rtk preference: {pref!r} (expected one of {', '.join(RTK_VALUES)})"
        )
    if pref == "off":
        return False, None
    found = path_lookup("rtk") is not None
    if pref == "auto":
        return (True, None) if found else (False, None)
    # pref == "on"
    if found:
        return True, None
    return False, "rtk is configured 'on' but was not found on PATH"


def resolve_superpowers(
    pref: str = "off",
    job_flag: str | None = None,
    piece_flag: str | None = None,
    task_type: str | None = None,
    task_text: str | None = None,
    *,
    dispatch_flag: str | None = None,
    user_pref: str | None = None,
    project_pref: str | None = None,
) -> tuple[str, str | None]:
    """Resolve superpowers mode and active skill.

    Returns elements in order: ``(mode, skill)``. ``skill`` is None when
    mode is off. Precedence: dispatch > piece > job > project > user > default off.

    When mode is ``auto``:
    - ``code`` / ``tests`` -> ``test-driven-development``
    - ``diagnosis`` / ``bug`` -> ``systematic-debugging``
    - ``docs`` / ``rename`` -> ``off``
    Unknown skill names raise ValueError.
    """
    if dispatch_flag is not None:
        raw = dispatch_flag
    elif piece_flag is not None:
        raw = piece_flag
    elif job_flag is not None:
        raw = job_flag
    elif project_pref is not None:
        raw = project_pref
    elif pref != "off":
        raw = pref
    elif user_pref is not None:
        raw = user_pref
    else:
        raw = pref or "off"

    if raw == "off":
        return "off", None

    if raw in ALLOWED_SUPERPOWERS:
        return raw, raw

    if raw != "auto":
        raise ValueError(
            f"Unknown superpowers skill: {raw!r} (expected one of 'off', 'auto', {', '.join(ALLOWED_SUPERPOWERS)})"
        )

    norm_type = (task_type or "").lower().strip()
    norm_text = (task_text or "").lower()

    if norm_type in ("code", "tests", "test"):
        return "auto", "test-driven-development"
    if norm_type in ("diagnosis", "bug", "debug"):
        return "auto", "systematic-debugging"
    if norm_type in ("docs", "rename"):
        return "off", None

    if "bug" in norm_text or "diagnos" in norm_text:
        return "auto", "systematic-debugging"
    if "doc" in norm_text or "rename" in norm_text:
        return "off", None
    if "test" in norm_text or "code" in norm_text:
        return "auto", "test-driven-development"

    return "off", None


def _superpowers_dir(vendor_dir: Path | None = None) -> Path:
    if vendor_dir is not None:
        return Path(vendor_dir)
    script_path = Path(__file__).resolve()
    # Layouts:
    # 1. Repo layout: .../skill/crew-chief/scripts/crew_chief/kit.py -> repo/vendor/superpowers
    # 2. Installed project: .../.claude/skills/crew-chief/scripts/crew_chief/kit.py
    #    -> .claude/skills/superpowers (sibling of crew-chief)
    # 3. Machine-wide: ~/.claude/skills/crew-chief/scripts/crew_chief/kit.py
    #    -> ~/.claude/skills/superpowers
    # 4. Fallback: crew-chief/vendor/superpowers
    candidates = [
        # Repo layout (vendor/superpowers at repo root)
        script_path.parents[4] / "vendor" / "superpowers",
        script_path.parents[3] / "vendor" / "superpowers",
        script_path.parents[2] / "vendor" / "superpowers",
        script_path.parents[1] / "vendor" / "superpowers",
        # Installed layout: superpowers sibling to crew-chief skill dir
        script_path.parents[3] / "superpowers",
        script_path.parents[2] / "superpowers",
        script_path.parents[1] / "superpowers",
        script_path.parent / "superpowers",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return candidates[0]


def _skill_dir(skill: str, sp_dir: Path) -> Path:
    if (sp_dir / "skills" / skill).is_dir():
        return sp_dir / "skills" / skill
    if (sp_dir / skill).is_dir():
        return sp_dir / skill
    return sp_dir / "skills" / skill


def load_superpowers_skill(skill: str, vendor_dir: Path | None = None) -> str | None:
    """Read the vendored SKILL.md for ``skill``, or return None if missing."""
    sp_dir = _superpowers_dir(vendor_dir)
    s_dir = _skill_dir(skill, sp_dir)
    path = s_dir / "SKILL.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    msg = f"Vendored superpowers skill {skill!r} not found at {path}"
    if msg not in _WARNED_MESSAGES:
        _WARNED_MESSAGES.add(msg)
        warnings.warn(msg, UserWarning, stacklevel=2)
    return None


def resolve_skill_references(
    skill: str, vendor_dir: Path | None = None
) -> dict[str, Path]:
    """Resolve support files and dependency skills to actual bundled paths."""
    sp_dir = _superpowers_dir(vendor_dir)
    s_dir = _skill_dir(skill, sp_dir)
    refs: dict[str, Path] = {}

    for filename in SKILL_SUPPORT_FILES.get(skill, []):
        p = s_dir / filename
        if p.is_file():
            refs[filename] = p

    for dep in SKILL_DEPENDENCIES.get(skill, []):
        dep_dir = _skill_dir(dep, sp_dir)
        p = dep_dir / "SKILL.md"
        if p.is_file():
            refs[f"superpowers:{dep}"] = p

    return refs


def render_kit_brief(
    role: str = "executor",
    backend: str = "generic",
    config: Any = None,
    piece: dict | None = None,
    job: dict | None = None,
    is_reseller: bool = False,
    *,
    dispatch_flag: str | None = None,
    brief: str | None = None,
    path_lookup: Any = None,
    vendor_dir: Path | None = None,
) -> list[str]:
    """Render the base kit prompt/brief additions for injection.

    Returns lines including:
    - Ponytail style rules according to configured ``ponytail_level``
    - rtk prefix instructions if rtk is active
    - Superpowers skill prompt block if resolved skill is active
    - Reseller requirements (concise rationale + validation evidence, no private
      chain-of-thought, and final status line) if backend is claude-ghost or marked reseller.
    """
    if path_lookup is None:
        path_lookup = shutil.which
    lines: list[str] = []

    # 1. Ponytail
    ponytail_level = "full"
    if config is not None:
        if hasattr(config, "ponytail_level"):
            ponytail_level = config.ponytail_level
        elif isinstance(config, dict) and "ponytail_level" in config:
            ponytail_level = config["ponytail_level"]

    has_ponytail = bool(brief and "## Style (ponytail" in brief)
    has_review_line = bool(brief and PONYTAIL_REVIEW_LINE in brief)

    if ponytail_level != "off" and not has_ponytail:
        lines.extend(_style_section(ponytail_level))
    if role in ("review", "reviewer") and ponytail_level != "off" and not has_review_line:
        lines.append(PONYTAIL_REVIEW_LINE)
        lines.append("")

    # 2. RTK
    rtk_pref = "auto"
    if config is not None:
        if hasattr(config, "rtk"):
            rtk_pref = config.rtk
        elif isinstance(config, dict) and "rtk" in config:
            rtk_pref = config["rtk"]

    active_rtk, warning = detect_rtk(rtk_pref, path_lookup=path_lookup)
    if warning and warning not in _WARNED_MESSAGES:
        _WARNED_MESSAGES.add(warning)
        warnings.warn(warning, UserWarning, stacklevel=2)
    if active_rtk:
        lines.extend([
            "## RTK",
            "Prefix supported commands with `rtk` (e.g. `rtk pytest -q`, `rtk git status`).",
            "Use `rtk proxy <cmd>` for raw output.",
            "",
        ])

    # 3. Superpowers
    superpowers_pref = "off"
    if config is not None:
        if hasattr(config, "superpowers"):
            superpowers_pref = config.superpowers
        elif isinstance(config, dict) and "superpowers" in config:
            superpowers_pref = config["superpowers"]

    piece_flag = piece.get("superpowers") if isinstance(piece, dict) else None
    job_flag = job.get("superpowers") if isinstance(job, dict) else None
    task_type = None
    task_text = None
    if isinstance(piece, dict):
        task_type = piece.get("work_type") or piece.get("type")
        task_text = piece.get("text") or piece.get("title") or piece.get("task_text")

    mode, skill = resolve_superpowers(
        pref=superpowers_pref,
        job_flag=job_flag,
        piece_flag=piece_flag,
        dispatch_flag=dispatch_flag,
        task_type=task_type,
        task_text=task_text,
    )

    if skill is not None:
        skill_text = load_superpowers_skill(skill, vendor_dir=vendor_dir)
        if skill_text is not None:
            lines.extend([
                f"## Superpowers ({skill})",
                f"use the skill superpowers:{skill}",
                "",
                skill_text.strip(),
                "",
            ])
            refs = resolve_skill_references(skill, vendor_dir=vendor_dir)
            if refs:
                lines.extend([
                    "### Bundled support files and dependencies",
                    "These materials are available locally from the bundled kit (no plugin installation required):",
                ])
                for ref_name, ref_path in refs.items():
                    lines.append(f"- `{ref_name}`: {ref_path}")
                lines.append("")

    # 4. Reseller requirements
    if backend == "claude-ghost" or is_reseller:
        lines.extend([
            "## Reseller instructions",
            "Give a concise written rationale and validation evidence (never private chain-of-thought).",
            "End with exactly one line:",
            "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT",
            "",
        ])

    return lines
