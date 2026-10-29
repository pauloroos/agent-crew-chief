"""Tests for kit resolver and renderer (crew_chief/kit.py)."""

from __future__ import annotations

import pytest
from pathlib import Path

from crew_chief.config import Prefs, DEFAULT_TOOLS
from crew_chief.kit import (
    detect_rtk,
    resolve_superpowers,
    render_kit_brief,
    reset_kit_warnings,
    resolve_skill_references,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_prefs(
    ponytail_level: str = "full",
    rtk: str = "auto",
    superpowers: str = "off",
) -> Prefs:
    return Prefs(
        tools=list(DEFAULT_TOOLS),
        models=[],
        strong_order=["codex", "claude"],
        explore_rate=0.1,
        forbid_training=False,
        sensitive_paths=[],
        issue_tracker="none",
        dispatch_timeout_s=900,
        review_default="",
        ponytail_level=ponytail_level,
        rtk=rtk,
        superpowers=superpowers,
    )


# --- detect_rtk tests ---


def test_detect_rtk_auto_with_binary():
    """auto with a binary is active and no warning."""
    active, warning = detect_rtk("auto", path_lookup=lambda name: "/usr/local/bin/rtk" if name == "rtk" else None)
    assert active is True
    assert warning is None


def test_detect_rtk_auto_without_binary():
    """auto without a binary is inactive and produces no warning."""
    active, warning = detect_rtk("auto", path_lookup=lambda name: None)
    assert active is False
    assert warning is None


def test_detect_rtk_off():
    """off is inactive and produces no warning regardless of PATH."""
    active, warning = detect_rtk("off", path_lookup=lambda name: "/usr/local/bin/rtk")
    assert active is False
    assert warning is None

    active, warning = detect_rtk("off", path_lookup=lambda name: None)
    assert active is False
    assert warning is None


def test_detect_rtk_on_without_binary():
    """on without a binary returns inactive and one warning."""
    active, warning = detect_rtk("on", path_lookup=lambda name: None)
    assert active is False
    assert warning is not None
    assert "rtk" in warning.lower()
    assert "path" in warning.lower()


def test_detect_rtk_on_with_binary():
    """on with a binary is active and produces no warning."""
    active, warning = detect_rtk("on", path_lookup=lambda name: "/bin/rtk")
    assert active is True
    assert warning is None


def test_detect_rtk_invalid_value():
    """Invalid rtk preference raises ValueError."""
    with pytest.raises(ValueError, match="Invalid rtk"):
        detect_rtk("unknown", path_lookup=lambda name: None)


# --- resolve_superpowers tests ---


def test_resolve_superpowers_precedence():
    """Precedence: piece > job > project > user > default off."""
    # Default is off
    mode, skill = resolve_superpowers()
    assert mode == "off"
    assert skill is None

    # User pref overrides default
    mode, skill = resolve_superpowers(user_pref="test-driven-development")
    assert mode == "test-driven-development"
    assert skill == "test-driven-development"

    # Project pref overrides user pref
    mode, skill = resolve_superpowers(
        user_pref="test-driven-development",
        project_pref="systematic-debugging",
    )
    assert mode == "systematic-debugging"
    assert skill == "systematic-debugging"

    # Job flag overrides project and user pref
    mode, skill = resolve_superpowers(
        user_pref="test-driven-development",
        project_pref="systematic-debugging",
        job_flag="verification-before-completion",
    )
    assert mode == "verification-before-completion"
    assert skill == "verification-before-completion"

    # Piece flag overrides job flag
    mode, skill = resolve_superpowers(
        user_pref="test-driven-development",
        project_pref="systematic-debugging",
        job_flag="verification-before-completion",
        piece_flag="off",
    )
    assert mode == "off"
    assert skill is None


def test_resolve_superpowers_auto_mapping():
    """auto mapping: code/tests -> test-driven-development; diagnosis/bug -> systematic-debugging; docs/rename -> off."""
    # code / tests
    for t_type in ("code", "tests"):
        mode, skill = resolve_superpowers("auto", task_type=t_type)
        assert mode == "auto"
        assert skill == "test-driven-development"

    # diagnosis / bug
    for t_type in ("diagnosis", "bug"):
        mode, skill = resolve_superpowers("auto", task_type=t_type)
        assert mode == "auto"
        assert skill == "systematic-debugging"

    # docs / rename
    for t_type in ("docs", "rename"):
        mode, skill = resolve_superpowers("auto", task_type=t_type)
        assert mode == "off"
        assert skill is None


def test_resolve_superpowers_auto_mapping_via_task_text():
    """auto mapping detects task_text hints when task_type is unstated or generic."""
    mode, skill = resolve_superpowers("auto", task_text="Fix bug in parser regex")
    assert mode == "auto"
    assert skill == "systematic-debugging"

    mode, skill = resolve_superpowers("auto", task_text="Update README docs")
    assert mode == "off"
    assert skill is None

    mode, skill = resolve_superpowers("auto", task_text="Implement unit tests for login")
    assert mode == "auto"
    assert skill == "test-driven-development"


def test_resolve_superpowers_unknown_skill_raises():
    """Unknown skill name raises ValueError."""
    with pytest.raises(ValueError, match="Unknown superpowers"):
        resolve_superpowers("nonexistent-skill")

    with pytest.raises(ValueError, match="Unknown superpowers"):
        resolve_superpowers(piece_flag="custom-bad-skill")


# --- render_kit_brief tests ---


def test_render_kit_brief_ponytail_levels():
    """render_kit_brief includes ponytail text for levels, and omits when off."""
    # Full level
    prefs_full = _make_prefs(ponytail_level="full", rtk="off", superpowers="off")
    lines = render_kit_brief("executor", "codex", prefs_full)
    text = "\n".join(lines)
    assert "## Style (ponytail full)" in text
    assert "smallest change" in text

    # Lite level
    prefs_lite = _make_prefs(ponytail_level="lite", rtk="off", superpowers="off")
    lines = render_kit_brief("executor", "codex", prefs_lite)
    text = "\n".join(lines)
    assert "## Style (ponytail lite)" in text

    # Ultra level
    prefs_ultra = _make_prefs(ponytail_level="ultra", rtk="off", superpowers="off")
    lines = render_kit_brief("executor", "codex", prefs_ultra)
    text = "\n".join(lines)
    assert "## Style (ponytail ultra)" in text

    # Off level
    prefs_off = _make_prefs(ponytail_level="off", rtk="off", superpowers="off")
    lines = render_kit_brief("executor", "codex", prefs_off)
    text = "\n".join(lines)
    assert "## Style" not in text
    assert "ponytail" not in text.lower()


def test_render_kit_brief_reviewer_includes_bloat_finding():
    """Reviewer role with ponytail active includes PONYTAIL_REVIEW_LINE."""
    prefs = _make_prefs(ponytail_level="full", rtk="off", superpowers="off")
    lines = render_kit_brief("reviewer", "codex", prefs)
    text = "\n".join(lines)
    assert "Bloat is a finding" in text


def test_render_kit_brief_rtk_active_and_inactive():
    """rtk prefix lines only rendered when rtk is active."""
    # Active rtk
    prefs_rtk_on = _make_prefs(ponytail_level="off", rtk="on", superpowers="off")
    lines = render_kit_brief(
        "executor",
        "codex",
        prefs_rtk_on,
        path_lookup=lambda name: "/bin/rtk",
    )
    text = "\n".join(lines)
    assert "rtk pytest -q" in text
    assert "rtk git status" in text
    assert "rtk proxy <cmd>" in text

    # Inactive rtk (off)
    prefs_rtk_off = _make_prefs(ponytail_level="off", rtk="off", superpowers="off")
    lines = render_kit_brief(
        "executor",
        "codex",
        prefs_rtk_off,
        path_lookup=lambda name: "/bin/rtk",
    )
    text = "\n".join(lines)
    assert "rtk" not in text.lower()

    # Inactive rtk (auto, binary missing)
    prefs_rtk_auto = _make_prefs(ponytail_level="off", rtk="auto", superpowers="off")
    lines = render_kit_brief(
        "executor",
        "codex",
        prefs_rtk_auto,
        path_lookup=lambda name: None,
    )
    text = "\n".join(lines)
    assert "rtk" not in text.lower()


def test_render_kit_brief_superpowers_injection():
    """Superpowers skill text is injected when skill is active."""
    prefs = _make_prefs(
        ponytail_level="off",
        rtk="off",
        superpowers="test-driven-development",
    )
    lines = render_kit_brief("executor", "codex", prefs)
    text = "\n".join(lines)
    assert "test-driven-development" in text
    assert "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST" in text


def test_render_kit_brief_reseller_requirements():
    """Reseller format for claude-ghost or is_reseller=True."""
    prefs = _make_prefs(ponytail_level="off", rtk="off", superpowers="off")

    # claude-ghost backend
    lines_ghost = render_kit_brief("executor", "claude-ghost", prefs)
    text_ghost = "\n".join(lines_ghost)
    assert "concise written rationale" in text_ghost
    assert "validation evidence" in text_ghost
    assert "chain-of-thought" in text_ghost
    assert "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in text_ghost

    # is_reseller flag with generic backend
    lines_reseller = render_kit_brief("executor", "codex", prefs, is_reseller=True)
    text_reseller = "\n".join(lines_reseller)
    assert "ACC_RESULT: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in text_reseller

    # Standard backend with is_reseller=False
    lines_normal = render_kit_brief("executor", "codex", prefs, is_reseller=False)
    text_normal = "\n".join(lines_normal)
    assert "ACC_RESULT" not in text_normal
    assert "chain-of-thought" not in text_normal


# --- T002 Correction regressions ---


def test_render_kit_brief_rtk_warning_process_dedup():
    """Missing rtk with pref='on' emits warning only once across different call sites even with simplefilter('always')."""
    import warnings

    prefs_on = _make_prefs(ponytail_level="off", rtk="on", superpowers="off")
    reset_kit_warnings()

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")

        def call_site_one():
            return render_kit_brief("executor", "codex", prefs_on, path_lookup=lambda _: None)

        def call_site_two():
            return render_kit_brief("reviewer", "claude-ghost", prefs_on, path_lookup=lambda _: None)

        res1 = call_site_one()
        res2 = call_site_two()

        # Exactly one process-level warning
        assert len(recorded) == 1
        assert "rtk is configured 'on' but was not found on PATH" in str(recorded[0].message)

        # Neither call renders any rtk instruction when binary is missing
        text1 = "\n".join(res1)
        text2 = "\n".join(res2)
        assert "rtk" not in text1.lower()
        assert "rtk" not in text2.lower()

    # Isolation check: reset allows warning to be triggered again
    reset_kit_warnings()
    with warnings.catch_warnings(record=True) as recorded2:
        warnings.simplefilter("always")
        call_site_one()
        assert len(recorded2) == 1


def test_superpowers_references_resolve_all_three_skills():
    """Dependencies and relative support references resolve to actual bundled content/paths for all 3 skills."""
    skills = [
        "test-driven-development",
        "systematic-debugging",
        "verification-before-completion",
    ]
    for skill in skills:
        refs = resolve_skill_references(skill)
        assert isinstance(refs, dict)
        # Verify that all referenced paths exist and are non-empty files
        for name, path in refs.items():
            assert path.is_file(), f"Referenced asset {name} -> {path} does not exist"
            assert path.stat().st_size > 0, f"Referenced asset {name} -> {path} is empty"

    # Specific check for test-driven-development: writing-good-tests.md must resolve
    tdd_refs = resolve_skill_references("test-driven-development")
    assert "writing-good-tests.md" in tdd_refs
    assert tdd_refs["writing-good-tests.md"].name == "writing-good-tests.md"

    # Specific check for systematic-debugging: relative support files and dependency skills
    debug_refs = resolve_skill_references("systematic-debugging")
    for expected in [
        "root-cause-tracing.md",
        "defense-in-depth.md",
        "condition-based-waiting.md",
        "find-polluter.sh",
        "condition-based-waiting-example.ts",
        "superpowers:test-driven-development",
        "superpowers:verification-before-completion",
    ]:
        assert expected in debug_refs, f"Expected reference {expected} missing from systematic-debugging refs"
        assert debug_refs[expected].is_file()


def test_render_kit_brief_injects_resolvable_bundled_locations():
    """Rendered brief for skills includes explicit usable locations for support files and dependencies."""
    # systematic-debugging
    prefs_debug = _make_prefs(ponytail_level="off", rtk="off", superpowers="systematic-debugging")
    lines = render_kit_brief("executor", "codex", prefs_debug)
    text = "\n".join(lines)
    assert "## Superpowers (systematic-debugging)" in text
    assert "### Bundled support files and dependencies" in text
    assert "root-cause-tracing.md" in text
    assert "superpowers:test-driven-development" in text
    assert "superpowers:verification-before-completion" in text
    assert "using-superpowers" not in text

    # test-driven-development
    prefs_tdd = _make_prefs(ponytail_level="off", rtk="off", superpowers="test-driven-development")
    lines_tdd = render_kit_brief("executor", "codex", prefs_tdd)
    text_tdd = "\n".join(lines_tdd)
    assert "## Superpowers (test-driven-development)" in text_tdd
    assert "### Bundled support files and dependencies" in text_tdd
    assert "writing-good-tests.md" in text_tdd


def test_superpowers_relocatable_bundle_layout(tmp_path):
    """Relocatable installed-bundle layout with custom vendor_dir resolves references correctly."""
    custom_vendor = tmp_path / "custom_vendor" / "superpowers"
    skills_dir = custom_vendor / "skills"
    # Copy only minimal runtime assets to custom location
    tdd_dir = skills_dir / "test-driven-development"
    tdd_dir.mkdir(parents=True)
    (tdd_dir / "SKILL.md").write_text("# TDD\n[writing-good-tests.md](writing-good-tests.md)", encoding="utf-8")
    (tdd_dir / "writing-good-tests.md").write_text("# Good Tests", encoding="utf-8")

    debug_dir = skills_dir / "systematic-debugging"
    debug_dir.mkdir(parents=True)
    (debug_dir / "SKILL.md").write_text("# Debugging\nsuperpowers:test-driven-development", encoding="utf-8")
    (debug_dir / "root-cause-tracing.md").write_text("# Root cause", encoding="utf-8")
    (debug_dir / "defense-in-depth.md").write_text("# Defense", encoding="utf-8")
    (debug_dir / "condition-based-waiting.md").write_text("# Waiting", encoding="utf-8")
    (debug_dir / "find-polluter.sh").write_text("#!/bin/sh", encoding="utf-8")
    (debug_dir / "condition-based-waiting-example.ts").write_text("// ts", encoding="utf-8")

    vbc_dir = skills_dir / "verification-before-completion"
    vbc_dir.mkdir(parents=True)
    (vbc_dir / "SKILL.md").write_text("# VBC", encoding="utf-8")

    refs = resolve_skill_references("systematic-debugging", vendor_dir=custom_vendor)
    for name, path in refs.items():
        assert path.is_file()
        assert str(custom_vendor) in str(path)

    # Render brief with custom vendor_dir
    prefs = _make_prefs(ponytail_level="off", rtk="off", superpowers="systematic-debugging")
    lines = render_kit_brief("executor", "codex", prefs, vendor_dir=custom_vendor)
    text = "\n".join(lines)
    assert str(custom_vendor) in text

