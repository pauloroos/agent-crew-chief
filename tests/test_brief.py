"""Tests for ``brief render`` (exec, review and converge briefs)."""

from __future__ import annotations

import json
from pathlib import Path

from crew_chief import records
from crew_chief.brief import render_converge_brief, render_exec_brief, render_review_brief
from crew_chief.cli import main
from crew_chief.sidecar import Sidecar, TaskMeta, save_sidecar
from crew_chief.speckit import SpecTask

TASK_T012 = SpecTask(
    id="T012", done=False, parallel=True, story="US1",
    text="Create User model in src/user.py", phase="Phase 2: Core",
    depends=(), line=7,
)
META = TaskMeta(
    type="code", risk="low", accept=["FR-001", "US1/AC9"],
    files=["src/user.py", "tests/test_user.py"],
    proof="python -m pytest tests/test_user.py -q", expect="1 passed",
    interfaces={"User": "dataclass User(id: int, name: str)"},
)

TASKS = (
    "## Phase 2: Core\n"
    "- [ ] T012 [P] [US1] Create User model in src/user.py\n"
    "- [ ] T013 Implement UserService\n"
)
SPEC = (
    "# Spec\n\n## Requirements\n\n"
    "- **FR-001**: System MUST export UTF-8 CSV\n"
)


def _heads(brief: str) -> list[str]:
    return [line for line in brief.splitlines() if line.startswith("## ")]


def test_exec_brief_has_sections_in_order_and_no_full_spec():
    b = render_exec_brief(
        TASK_T012, META, ["FR-001: System MUST export UTF-8 CSV", "UNRESOLVED: US1/AC9"],
        ["No new deps"], "003-csv-export",
    )
    assert _heads(b) == [
        "## Acceptance criteria", "## Global constraints", "## Interfaces",
        "## Files you may change", "## Proof", "## Report",
    ]
    lines = b.splitlines()
    assert lines[0] == "# T012 Create User model in src/user.py"
    assert lines[1] == "Feature: 003-csv-export · Phase: Phase 2: Core"
    assert "UNRESOLVED: US1/AC9" in b
    assert "FR-001: System MUST export UTF-8 CSV" in b
    assert "No new deps" in b
    assert "python -m pytest tests/test_user.py -q" in b and "1 passed" in b
    assert "End with one status line: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT" in b


def test_exec_brief_omits_interfaces_and_says_not_restricted_without_files():
    bare = TaskMeta(type="code", risk="low")
    b = render_exec_brief(TASK_T012, bare, [], [], "003-csv-export")
    assert "## Interfaces" not in _heads(b)
    assert "Not restricted — keep the change minimal" in b


def test_review_brief_asks_two_verdicts():
    b = render_review_brief(TASK_T012, META, ["FR-001: ..."])
    assert "## Spec verdict" in b and "## Quality verdict" in b
    assert "Do not trust the executor's report; read the diff (git show HEAD)." in b
    assert "done | missing | misunderstood" in b
    assert "extra" in b
    assert "Critical | Important | Minor" in b
    assert "FR-001: ..." in b


def test_converge_brief_names_the_spec_files_and_gap_format():
    b = render_converge_brief("specs/003-csv-export")
    assert "specs/003-csv-export/spec.md" in b and "specs/003-csv-export/tasks.md" in b
    assert "- [ ] T### <text> per <ref> (<gap-type>)" in b
    assert "Converged" in b


# -- CLI -------------------------------------------------------------------


def _setup(repo: Path) -> tuple[str, Path]:
    folder = repo / "specs" / "003-csv-export"
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text(TASKS, encoding="utf-8")
    (folder / "spec.md").write_text(SPEC, encoding="utf-8")
    save_sidecar(folder, Sidecar(
        spec_dir="specs/003-csv-export",
        global_constraints=["No new deps"],
        tasks={"T012": TaskMeta(
            "code", "low", accept=["FR-001", "FR-001", "US1/AC9"],
            files=["src/user.py"], proof="python check.py", expect="ok",
        )},
    ))
    job = records.new_job(repo, "Spec job", "request", True, "parts")["id"]
    assert main(["spec", "import", "--project", str(repo), "--job", job,
                 "--spec-dir", "specs/003-csv-export"]) == 0
    return job, folder


def _render(repo: Path, job: str, *extra: str) -> int:
    return main(["brief", "render", "--project", str(repo), "--job", job, *extra])


def _piece_n(repo: Path, job: str, ref: str) -> int:
    stored = records.load_job(repo, job)
    return records.piece_for_ref(stored, ref)["n"]


def test_brief_render_cli_prints_exec_brief(git_repo, capsys):
    job, _ = _setup(git_repo)
    n = _piece_n(git_repo, job, "T012")
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", str(n)) == 0
    out = capsys.readouterr().out
    assert out.startswith("# T012 Create User model in src/user.py")
    assert "Feature: 003-csv-export" in out
    assert "FR-001: System MUST export UTF-8 CSV" in out
    assert out.count("FR-001") == 1          # refs deduplicated
    assert "UNRESOLVED: US1/AC9" in out
    assert "No new deps" in out
    assert "# Spec" not in out               # never the whole spec


def test_brief_render_cli_review_and_converge(git_repo, capsys):
    job, _ = _setup(git_repo)
    n = _piece_n(git_repo, job, "T012")
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", str(n), "--review") == 0
    assert "## Spec verdict" in capsys.readouterr().out
    assert _render(git_repo, job, "--converge") == 0
    assert "specs/003-csv-export/tasks.md" in capsys.readouterr().out


def test_brief_render_cli_without_ref_exits_1(git_repo, capsys):
    job = records.new_job(git_repo, "Plain", "request", True, "parts")["id"]
    records.update_job(git_repo, job, lambda j: records.add_piece(j, "manual", "code", "low"))
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", "1") == 1
    assert "piece 1 has no spec task; write the brief yourself" in capsys.readouterr().err


def test_brief_render_cli_stale_piece_exits_1(git_repo, capsys):
    job, folder = _setup(git_repo)
    n = _piece_n(git_repo, job, "T012")
    # tasks.md regenerated: T012 now names a different task
    (folder / "tasks.md").write_text(
        TASKS.replace("Create User model in src/user.py", "Something else entirely"),
        encoding="utf-8",
    )
    assert main(["spec", "import", "--project", str(git_repo), "--job", job]) == 0
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", str(n)) == 1
    assert (
        f"piece {n} is stale (task T012 changed); re-import the spec"
        in capsys.readouterr().err
    )


def test_brief_render_cli_writes_out_file(git_repo, tmp_path, capsys):
    job, _ = _setup(git_repo)
    n = _piece_n(git_repo, job, "T012")
    target = tmp_path / "briefs" / "t012.md"
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", str(n), "--out", str(target)) == 0
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# T012 ") and "## Report" in text
    assert str(target) in capsys.readouterr().out


def test_brief_render_cli_json(git_repo, capsys):
    job, _ = _setup(git_repo)
    n = _piece_n(git_repo, job, "T012")
    capsys.readouterr()
    assert _render(git_repo, job, "--piece", str(n), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "exec" and payload["piece"] == n
    assert payload["brief"].startswith("# T012 ")


def test_brief_render_cli_requires_piece_unless_converge(git_repo, capsys):
    job, _ = _setup(git_repo)
    capsys.readouterr()
    assert _render(git_repo, job) == 2
    assert "--piece" in capsys.readouterr().err
