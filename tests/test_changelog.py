"""Tests for the project CHANGELOG entry that ``close`` writes."""

from __future__ import annotations

import json
from pathlib import Path

from crew_chief import records
from crew_chief.changelog import CATEGORY_BY_COMMIT, entry_line, insert_entry
from crew_chief.cli import main


def test_insert_creates_file_with_unreleased_and_category():
    out = insert_entry(None, "Added", "- CSV export. <!-- crew:J1 -->")
    assert out.startswith("# Changelog\n")
    assert "keepachangelog.com" in out
    assert "## [Unreleased]" in out
    assert "### Added\n- CSV export. <!-- crew:J1 -->" in out


def test_insert_is_idempotent():
    once = insert_entry(None, "Added", "- X. <!-- crew:J1 -->")
    assert insert_entry(once, "Added", "- X. <!-- crew:J1 -->") == once
    # the marker anywhere in the file counts, even in a released section
    text = "# C\n\n## [0.1.0] - 2026-01-01\n### Added\n- X. <!-- crew:J1 -->\n"
    assert insert_entry(text, "Added", "- X. <!-- crew:J1 -->") == text


def test_insert_never_touches_released_sections():
    text = "# Changelog\n\n## [0.1.0] - 2026-01-01\n### Added\n- Old.\n"
    out = insert_entry(text, "Fixed", "- Bug. <!-- crew:J2 -->")
    assert out.index("## [Unreleased]") < out.index("## [0.1.0]")
    assert out.endswith("## [0.1.0] - 2026-01-01\n### Added\n- Old.\n")
    assert "### Fixed\n- Bug. <!-- crew:J2 -->" in out


def test_insert_into_existing_unreleased_category_appends_at_the_end():
    text = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- One.\n- Two.\n\n"
        "### Fixed\n- Bug.\n\n## [0.1.0] - 2026-01-01\n### Added\n- Old.\n"
    )
    out = insert_entry(text, "Added", "- Three. <!-- crew:J3 -->")
    assert "### Added\n- One.\n- Two.\n- Three. <!-- crew:J3 -->\n\n### Fixed" in out
    assert out.endswith("## [0.1.0] - 2026-01-01\n### Added\n- Old.\n")


def test_insert_new_category_inside_existing_unreleased_in_canonical_order():
    text = (
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Bug.\n\n"
        "## [0.1.0] - 2026-01-01\n### Added\n- Old.\n"
    )
    out = insert_entry(text, "Added", "- New. <!-- crew:J4 -->")
    unreleased = out[out.index("## [Unreleased]"):out.index("## [0.1.0]")]
    assert unreleased.index("### Added") < unreleased.index("### Fixed")
    assert out.count("### Added") == 2  # one in Unreleased, the released one


def test_insert_into_empty_unreleased_directly_above_a_release():
    text = "# C\n\n## [Unreleased]\n## [0.1.0] - 2026-01-01\n- Old.\n"
    out = insert_entry(text, "Changed", "- Tweak. <!-- crew:J5 -->")
    assert out == (
        "# C\n\n## [Unreleased]\n\n### Changed\n- Tweak. <!-- crew:J5 -->\n\n"
        "## [0.1.0] - 2026-01-01\n- Old.\n"
    )


def test_insert_keeps_link_references_at_the_end():
    text = (
        "# C\n\n## [Unreleased]\n\n### Added\n- One.\n\n"
        "[Unreleased]: https://example.com/compare/v0.1.0...HEAD\n"
    )
    out = insert_entry(text, "Added", "- Two. <!-- crew:J6 -->")
    assert out.endswith(
        "- One.\n- Two. <!-- crew:J6 -->\n\n"
        "[Unreleased]: https://example.com/compare/v0.1.0...HEAD\n"
    )


def test_insert_without_any_release_heading_appends_unreleased():
    out = insert_entry("# Changelog\n\nSome notes.\n", "Added", "- N. <!-- crew:J7 -->")
    assert out.startswith("# Changelog\n\nSome notes.\n")
    assert out.endswith("## [Unreleased]\n\n### Added\n- N. <!-- crew:J7 -->\n")


def test_insert_keeps_crlf_and_bom():
    text = "\ufeff# Changelog\r\n\r\n## [0.1.0] - 2026-01-01\r\n### Added\r\n- Old.\r\n"
    out = insert_entry(text, "Added", "- Novo é bom. <!-- crew:J8 -->")
    assert out.startswith("\ufeff# Changelog\r\n")
    assert out.endswith("## [0.1.0] - 2026-01-01\r\n### Added\r\n- Old.\r\n")
    assert "\n" not in out.replace("\r\n", "")  # no bare LF anywhere
    assert "- Novo é bom. <!-- crew:J8 -->\r\n" in out
    again = insert_entry(out, "Added", "- Outro. <!-- crew:J9 -->")
    assert "\n" not in again.replace("\r\n", "")
    assert again.index("J8") < again.index("J9") < again.index("[0.1.0]")


def test_insert_keeps_lf_when_the_file_is_lf():
    out = insert_entry("# C\n\n## [0.1.0]\n", "Added", "- A. <!-- crew:J1 -->")
    assert "\r" not in out


def test_category_by_commit_type():
    assert CATEGORY_BY_COMMIT == {
        "feat": "Added", "fix": "Fixed", "refactor": "Changed", "perf": "Changed",
    }


def test_entry_line_mentions_spec_and_adrs():
    job = {"id": "J1", "title": "Export CSV", "spec_dir": "specs/001-csv",
           "decisions": ["0007", "0008"]}
    assert entry_line(job) == (
        "- Export CSV (specs/001-csv, ADR 0007, ADR 0008). <!-- crew:J1 -->"
    )
    assert entry_line({"id": "J2", "title": "Fix it"}) == "- Fix it. <!-- crew:J2 -->"
    assert entry_line({"id": "J3", "title": "Do.", "decisions": ["0001"]}) == (
        "- Do (ADR 0001). <!-- crew:J3 -->"
    )


# --- close ------------------------------------------------------------------


def _accepted_job(repo, title="Export CSV", piece_type="code", accept=True):
    job = records.new_job(repo, title, "do it", True, "one part")
    records.add_piece(job, "One", piece_type, "low")
    if accept:
        records.mark_accepted(job, 1, {"n": 1, "branch": "crew/x"})
    job["spec_dir"] = "specs/001-csv"
    job["decisions"] = ["0007"]
    records.save_job(repo, job)
    return job["id"]


def _close(repo, job_id, *extra):
    return main(["close", "--project", str(repo), "--job", job_id, *extra])


def _prefs(repo, text):
    path = repo / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_close_writes_changelog_once_and_respects_none_and_flag(git_repo, capsys):
    repo = git_repo
    job_id = _accepted_job(repo)
    path = repo / "CHANGELOG.md"

    assert _close(repo, job_id) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "changelog: CHANGELOG.md (Added)"
    commit = out.split("---")[0]
    assert "CHANGELOG.md" in commit.split("\n", 1)[1]  # listed in the message
    text = path.read_text(encoding="utf-8")
    assert text.count(f"<!-- crew:{job_id} -->") == 1
    assert "(specs/001-csv, ADR 0007)" in text

    assert _close(repo, job_id) == 0  # twice: unchanged, no second entry
    again = capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == text
    assert not again.startswith("changelog:")

    other = _accepted_job(repo, title="Other")
    assert _close(repo, other, "--no-changelog") == 0
    capsys.readouterr()
    assert path.read_text(encoding="utf-8") == text

    _prefs(repo, 'changelog = "none"\n')
    assert _close(repo, other) == 0
    assert "changelog:" not in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == text


def test_close_skips_changelog_without_accepted_piece_or_for_docs(git_repo, capsys):
    repo = git_repo
    pending = _accepted_job(repo, title="Pending", accept=False)
    docs = _accepted_job(repo, title="Docs", piece_type="docs")
    for job_id in (pending, docs):
        assert _close(repo, job_id) == 0
    capsys.readouterr()
    assert not (repo / "CHANGELOG.md").exists()


def test_close_uses_changelog_path_and_keeps_crlf_and_bom(git_repo, capsys):
    repo = git_repo
    _prefs(repo, 'changelog_path = "docs/CHANGES.md"\n')
    target = repo / "docs" / "CHANGES.md"
    target.write_bytes(
        b"\xef\xbb\xbf# Changelog\r\n\r\n## [1.0.0] - 2026-01-01\r\n- Old.\r\n")
    job_id = _accepted_job(repo, title="Exportação")

    assert _close(repo, job_id, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changelog"] == {"path": "docs/CHANGES.md", "category": "Added"}
    assert "docs/CHANGES.md" in payload["commit"]
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf# Changelog\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert "Exportação".encode() in raw
    assert raw.endswith(b"## [1.0.0] - 2026-01-01\r\n- Old.\r\n")


def test_close_rejects_changelog_path_outside_the_project(git_repo, capsys):
    repo = git_repo
    _prefs(repo, 'changelog_path = "../CHANGELOG.md"\n')
    job_id = _accepted_job(repo)
    assert _close(repo, job_id) != 0
    assert not (repo.parent / "CHANGELOG.md").exists()


def test_commit_message_is_unchanged_without_a_changelog_entry():
    job = {"id": "J", "title": "T", "pieces": [{"type": "code", "title": "P"}]}
    message, _ = records.summaries(job)
    assert "CHANGELOG" not in message
    assert records.commit_type(job) == "feat"


def test_insert_before_changesets_style_release_headings():
    text = "# my-pkg\n\n## 1.2.0\n\n### Minor Changes\n\n- abc: thing\n\n## v1.1.0\n\n- old\n"
    out = insert_entry(text, "Added", "- New. <!-- crew:J1 -->")
    assert out.index("## [Unreleased]") < out.index("## 1.2.0")
    assert out.endswith("## 1.2.0\n\n### Minor Changes\n\n- abc: thing\n\n## v1.1.0\n\n- old\n")
    assert "### Added\n- New. <!-- crew:J1 -->" in out


def test_close_writes_the_changelog_atomically(git_repo, capsys, monkeypatch):
    calls = []
    real = records.write_atomic
    monkeypatch.setattr(
        records, "write_atomic", lambda path, text: (calls.append(path), real(path, text))
    )
    target = git_repo / "CHANGELOG.md"
    target.write_bytes(b"\xef\xbb\xbf# Changelog\r\n\r\n## [1.0.0]\r\n- Old.\r\n")
    job_id = _accepted_job(git_repo)
    assert _close(git_repo, job_id) == 0
    assert target.resolve() in [Path(p).resolve() for p in calls]
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf# Changelog\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")
