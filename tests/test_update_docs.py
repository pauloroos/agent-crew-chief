"""T009 — update check documentation assertions.

Verification boundary: docs assertions cover commands/placeholder/stop-on-failure
instruction, default, only external access, offline silence, no setup; preserve
existing doc and web security tests.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


def _read_readme_en() -> str:
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def _read_readme_pt() -> str:
    return (Path(__file__).resolve().parents[1] / "README.pt-BR.md").read_text(encoding="utf-8")


def _read_central_tutorial_en() -> str:
    path = Path(__file__).resolve().parents[1] / "docs" / "tutorials" / "04-central.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_central_tutorial_pt() -> str:
    path = Path(__file__).resolve().parents[1] / "docs" / "tutorials" / "04-central.pt-BR.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


DOCS = {
    "readme_en": _read_readme_en,
    "readme_pt": _read_readme_pt,
    "central_en": _read_central_tutorial_en,
    "central_pt": _read_central_tutorial_pt,
}

# Exact required strings that must appear in each document's update section
REQUIRED_EN = [
    # Preference and default
    "check_updates",
    "strict boolean",
    "default `true`",
    # Opt-out
    "check_updates = false",
    "opt out",
    "preferences are never rewritten",
    "project-over-global precedence",
    # Only external network access
    "only external network access",
    # No setup
    "No setup is required",
    # 24h throttle including failures
    "rolling 24 hours",
    "including failures",
    # Offline silence
    "silent when offline",
    "on timeout",
    "stale or invalid",
    # Exact FR-016 notice with v0.4.4
    "ACC v0.4.4 available. Update manually in your ACC source clone: git fetch --tags; git checkout v0.4.4; python -I install.py --command; python -I install.py <project>",
    # Four separate commands, check each, stop on failure
    "four separate manual commands",
    "Check each succeeds and STOP on failure before proceeding",
    # Project placeholder
    "<project> is an explicit user-supplied project directory path",
    # No auto-execute
    "never infers clone paths",
    "never auto-executes these commands",
    "never downloads, installs, or refreshes installed skills automatically",
]

REQUIRED_PT = [
    # Preference and default
    "check_updates",
    "booleano estrito",
    "padrão `true`",
    # Opt-out
    "check_updates = false",
    "desativar completamente",
    "preferências nunca são reescritas",
    "projeto-sobre-global",
    # Only external network access
    "única conexão de rede externa",
    # No setup
    "Nenhuma configuração é necessária",
    # 24h throttle including failures
    "24 horas rolantes",
    "incluindo falhas",
    # Offline silence
    "silenciosa quando offline",
    "em timeout",
    "desatualizado ou inválido",
    # Exact FR-016 notice with v0.4.4 (same in both languages)
    "ACC v0.4.4 available. Update manually in your ACC source clone: git fetch --tags; git checkout v0.4.4; python -I install.py --command; python -I install.py <project>",
    # Four separate commands, check each, stop on failure
    "quatro comandos manuais separados",
    "Confira se cada um funciona e PARE na falha antes de prosseguir",
    # Project placeholder
    "<project> é um caminho de diretório de projeto fornecido explicitamente pelo usuário",
    # No auto-execute
    "nunca infere caminhos do clone",
    "nunca executa esses comandos automaticamente",
    "nunca baixa, instala ou atualiza skills instaladas automaticamente",
]


def _normalize(text: str) -> str:
    """Collapse whitespace and drop Markdown inline-code backticks for matching."""
    return " ".join(text.replace("`", "").split())


def _find_update_section(text: str) -> str:
    """Extract the update check section from a document."""
    # Look for the update check section header and everything after it
    # until the next major section (## ) or end of file. A missing section
    # returns an empty string so callers fail instead of accepting the doc.
    lines = text.split("\n")
    in_section = False
    section_lines = []
    for line in lines:
        if "update check" in line.lower() or "checagem de atualização" in line.lower() or "atualização" in line.lower():
            if line.strip().startswith("##") or line.strip().startswith("###"):
                in_section = True
        if in_section:
            if line.strip().startswith("## ") and "update check" not in line.lower() and "checagem de atualização" not in line.lower() and "atualização" not in line.lower():
                break
            section_lines.append(line)
    return "\n".join(section_lines)


@pytest.mark.parametrize("doc_name,reader,required", [
    ("readme_en", _read_readme_en, REQUIRED_EN),
    ("readme_pt", _read_readme_pt, REQUIRED_PT),
    ("central_en", _read_central_tutorial_en, REQUIRED_EN),
    ("central_pt", _read_central_tutorial_pt, REQUIRED_PT),
])
def test_update_section_contains_all_required(doc_name: str, reader, required: list[str]):
    """Each document must contain every required string in its update section."""
    text = reader()
    section = _normalize(_find_update_section(text))
    for req in required:
        assert _normalize(req) in section, f"{doc_name}: missing required text: {req!r}"


@pytest.mark.parametrize("doc_name,reader", [
    ("readme_en", _read_readme_en),
    ("readme_pt", _read_readme_pt),
    ("central_en", _read_central_tutorial_en),
    ("central_pt", _read_central_tutorial_pt),
])
def test_update_section_exists(doc_name: str, reader):
    """Each document must have an update check section."""
    text = reader()
    section = _find_update_section(text)
    # The section must be non-trivial (more than just a header)
    assert len(section) > 200, f"{doc_name}: update section too short or missing"


def test_exact_fr016_notice_in_all_docs():
    """The exact FR-016 notice line with v0.4.4 must appear in all four docs."""
    notice = "ACC v0.4.4 available. Update manually in your ACC source clone: git fetch --tags; git checkout v0.4.4; python -I install.py --command; python -I install.py <project>"
    for name, reader in DOCS.items():
        text = reader()
        assert notice in text, f"{name}: exact FR-016 notice not found"


def test_four_separate_commands_documented():
    """All four manual commands must be explicitly called out."""
    for name, reader in DOCS.items():
        text = reader()
        # Each of the four commands must appear
        assert "git fetch --tags" in text, f"{name}: 'git fetch --tags' missing"
        assert "git checkout v0.4.4" in text, f"{name}: 'git checkout v0.4.4' missing"
        assert "python -I install.py --command" in text, f"{name}: 'python -I install.py --command' missing"
        assert "python -I install.py <project>" in text, f"{name}: 'python -I install.py <project>' missing"


def test_stop_on_failure_instruction():
    """The stop-on-failure instruction must be explicit."""
    for name, reader in DOCS.items():
        text = _normalize(reader())
        # Must have explicit stop-on-failure language
        lowered = text.lower()
        has_stop = (
            "stop on failure" in lowered
            or "pare na falha" in lowered
        )
        assert has_stop, f"{name}: stop-on-failure instruction missing"


def test_no_setup_required_for_updates():
    """Update check docs must not require any setup steps."""
    for name, reader in DOCS.items():
        text = reader()
        section = _normalize(_find_update_section(text)).lower()
        # Should not say things like "run setup first" in the update context
        assert "run setup" not in section, f"{name}: update section mentions 'run setup'"
        assert "configure before" not in section, f"{name}: update section mentions 'configure before'"


def test_offline_silence_documented():
    """Docs must acknowledge offline/timeouts are silent (no error shown)."""
    for name, reader in DOCS.items():
        text = reader()
        section = _normalize(_find_update_section(text)).lower()
        # Must mention silent/offline/timeout behavior
        assert "silent" in section or "silenciosa" in section, f"{name}: offline silence not documented"
        assert "offline" in section, f"{name}: offline not mentioned"
        assert "timeout" in section, f"{name}: timeout not mentioned"


def test_no_auto_install_or_refresh():
    """Docs must state ACC never auto-installs or refreshes skills."""
    for name, reader in DOCS.items():
        text = reader()
        section = _normalize(_find_update_section(text)).lower()
        assert "auto-executes" in section or "automaticamente" in section, f"{name}: auto-execute denial missing"
        assert "never downloads" in section or "nunca baixa" in section, f"{name}: no-auto-download missing"
