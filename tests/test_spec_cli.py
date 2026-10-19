"""Tests for ``spec new``, ``spec import`` and ``spec status``."""

from __future__ import annotations

import json
from pathlib import Path

from crew_chief import records
from crew_chief.cli import main
from crew_chief.sidecar import SIDECAR_NAME, Sidecar, TaskMeta, load_sidecar, save_sidecar
from crew_chief.speckit import parse_spec_refs, parse_tasks

SAMPLE = (
    "# Tasks\n\n## Phase 1: Setup\n"
    "- [ ] T001 Create project structure\n"
    "- [X] T002 Configure linting\n\n"
    "## Phase 2: Core\n"
    "- [ ] T012 [P] Create User model in src/a.py\n"
    "- [ ] T013 Implement UserService in src/svc.py (depends on T012)\n"
)

PARALLEL = (
    "## Phase 2: Core\n"
    "- [ ] T012 [P] Create User model\n"
    "- [ ] T014 [P] Create Order model\n"
)


def _write_spec(repo: Path, rel: str, tasks: str = SAMPLE) -> Path:
    folder = repo / rel
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tasks.md").write_text(tasks, encoding="utf-8")
    (folder / "spec.md").write_text("# Spec\n", encoding="utf-8")
    return folder


def _new_job(repo: Path) -> str:
    return records.new_job(repo, "Spec job", "request", True, "parts")["id"]


def _stored_job(repo: Path, job_id: str) -> dict:
    path = records.jobs_dir(repo) / f"{job_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _import(repo: Path, job: str, *extra: str) -> int:
    return main(["spec", "import", "--project", str(repo), "--job", job, *extra])


def test_spec_new_creates_numbered_folder_without_specify(git_repo, capsys):
    (git_repo / "specs" / "001-old").mkdir(parents=True)
    assert main(["spec", "new", "--project", str(git_repo), "--title", "CSV export"]) == 0
    folder = git_repo / "specs" / "002-csv-export"
    assert {p.name for p in folder.iterdir()} == {"spec.md", "plan.md", "tasks.md"}
    assert not (git_repo / ".specify").exists()
    assert capsys.readouterr().out.strip() == "specs/002-csv-export"
    assert "CSV export" in (folder / "spec.md").read_text(encoding="utf-8")


def test_spec_new_starts_at_001_and_ignores_other_folders(git_repo, capsys):
    (git_repo / "specs" / "notes").mkdir(parents=True)
    assert main(["spec", "new", "--project", str(git_repo), "--title", "Árvore  de Dados!"]) == 0
    assert capsys.readouterr().out.strip() == "specs/001-arvore-de-dados"


def test_spec_templates_use_the_spec_kit_grammar(git_repo):
    assert main(["spec", "new", "--project", str(git_repo), "--title", "X"]) == 0
    folder = git_repo / "specs" / "001-x"
    refs = parse_spec_refs((folder / "spec.md").read_text(encoding="utf-8"))
    assert {"US1", "US1/AC1", "FR-001", "SC-001"} <= set(refs)
    assert parse_tasks((folder / "tasks.md").read_text(encoding="utf-8")) == []
    assert (folder / "plan.md").read_text(encoding="utf-8").strip()


def test_spec_import_creates_pieces_and_sidecar(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    stored = _stored_job(git_repo, job)
    assert [p["ref"] for p in stored["pieces"]] == ["T001", "T012", "T013"]
    assert stored["spec_dir"] == "specs/003-x"
    assert stored["pieces"][2]["type"] == "code"
    sidecar = load_sidecar(spec)
    assert sidecar.tasks["T013"].depends == ["T012"]
    assert sidecar.job == job and sidecar.spec_dir == "specs/003-x"
    assert "T002" not in sidecar.tasks
    out = capsys.readouterr().out
    assert "T012" in out and "missing proof: T001, T012, T013" in out
    assert (spec / "tasks.md").read_text(encoding="utf-8") == SAMPLE


def test_spec_import_uses_sidecar_type_and_risk(git_repo):
    spec = _write_spec(git_repo, "specs/003-x")
    save_sidecar(spec, Sidecar(spec_dir="x", tasks={
        "T012": TaskMeta("data", "high", proof="python check.py", depends=["T001"]),
        "T013": TaskMeta("code", "low", depends=[]),
    }))
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    pieces = _stored_job(git_repo, job)["pieces"]
    assert (pieces[1]["type"], pieces[1]["risk"]) == ("data", "high")
    assert (pieces[2]["type"], pieces[2]["risk"]) == ("code", "low")
    sidecar = load_sidecar(spec)
    assert sidecar.tasks["T012"].depends == ["T001"]      # sidecar wins
    assert sidecar.tasks["T013"].depends == ["T012"]      # prose fills the gap
    assert sidecar.tasks["T012"].proof == "python check.py"


def test_spec_import_twice_does_not_duplicate(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert _import(git_repo, job) == 0                    # spec_dir remembered
    stored = _stored_job(git_repo, job)
    assert [p["ref"] for p in stored["pieces"]] == ["T001", "T012", "T013"]
    assert [p["n"] for p in stored["pieces"]] == [1, 2, 3]


def test_spec_import_adds_only_new_tasks(git_repo):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    _write_spec(git_repo, "specs/003-x", SAMPLE + "- [ ] T014 Another thing\n")
    assert _import(git_repo, job) == 0
    refs = [p["ref"] for p in _stored_job(git_repo, job)["pieces"]]
    assert refs == ["T001", "T012", "T013", "T014"]


def test_spec_import_keeps_pieces_without_ref(git_repo):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    records.update_job(git_repo, job, lambda j: records.add_piece(j, "manual", "code", "low"))
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    pieces = _stored_job(git_repo, job)["pieces"]
    assert pieces[0]["title"] == "manual" and "ref" not in pieces[0]
    assert [p.get("ref") for p in pieces] == [None, "T001", "T012", "T013"]


def test_spec_import_warns_orphan_sidecar_entries(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    save_sidecar(spec, Sidecar(spec_dir="x", tasks={"T099": TaskMeta("code", "low")}))
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert "orphan sidecar entries: T099" in capsys.readouterr().out


def test_spec_import_after_renumbering_warns_and_never_repoints(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    capsys.readouterr()
    # spec-kit regenerates tasks.md: T012 became T020, T013 became T021, and
    # T012 now names a different task.
    renumbered = (
        "## Phase 1: Setup\n- [ ] T001 Create project structure\n"
        "## Phase 2: Core\n"
        "- [ ] T012 Totally different work\n"
        "- [ ] T020 [P] Create User model in src/a.py\n"
        "- [ ] T021 Implement UserService in src/svc.py (depends on T020)\n"
    )
    _write_spec(git_repo, "specs/003-x", renumbered)
    assert _import(git_repo, job) == 0
    out = capsys.readouterr().out
    assert "changed task text: T012" in out
    assert "orphan pieces: T013" in out
    pieces = _stored_job(git_repo, job)["pieces"]
    by_ref = {p["ref"]: p for p in pieces}
    assert by_ref["T012"]["title"] == "T012 Create User model in src/a.py"   # untouched
    assert "T020" in by_ref and "T021" in by_ref
    assert len(pieces) == 5
    assert "orphan sidecar entries: T013" in out


def test_spec_import_warns_parallel_conflict(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x", PARALLEL)
    save_sidecar(spec, Sidecar(spec_dir="x", tasks={
        "T012": TaskMeta("code", "low", files=["src/a.py", "src/b.py"]),
        "T014": TaskMeta("code", "low", files=["src\\a.py"]),
    }))
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert "parallel conflict: T012/T014 share src/a.py" in capsys.readouterr().out


def test_spec_import_no_conflict_for_non_parallel_or_other_phase(git_repo, capsys):
    tasks = (
        "## Phase 1: A\n- [ ] T001 [P] one\n- [ ] T002 two\n"
        "## Phase 2: B\n- [ ] T003 [P] three\n"
    )
    spec = _write_spec(git_repo, "specs/003-x", tasks)
    shared = ["src/a.py"]
    save_sidecar(spec, Sidecar(spec_dir="x", tasks={
        t: TaskMeta("code", "low", files=shared) for t in ("T001", "T002", "T003")
    }))
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert "parallel conflict" not in capsys.readouterr().out


def test_spec_import_bad_spec_dir_exits_2_and_writes_nothing(git_repo, capsys):
    job = _new_job(git_repo)
    before = (records.jobs_dir(git_repo) / f"{job}.json").read_text(encoding="utf-8")
    assert _import(git_repo, job, "--spec-dir", "specs/nope") == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert (records.jobs_dir(git_repo) / f"{job}.json").read_text(encoding="utf-8") == before
    assert _stored_job(git_repo, job)["pieces"] == []


def test_spec_import_missing_tasks_md_exits_2_and_writes_nothing(git_repo, capsys):
    (git_repo / "specs" / "003-x").mkdir(parents=True)
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    assert "spec_dir" not in _stored_job(git_repo, job)
    assert not (git_repo / "specs" / "003-x" / SIDECAR_NAME).exists()


def test_spec_import_broken_sidecar_exits_2_and_writes_nothing(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    (spec / SIDECAR_NAME).write_text("[tasks.T001\n", encoding="utf-8")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 2
    assert _stored_job(git_repo, job)["pieces"] == []


def test_spec_import_without_any_spec_dir_exits_2(git_repo, capsys):
    job = _new_job(git_repo)
    assert _import(git_repo, job) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_spec_import_reads_specify_feature_json(git_repo):
    _write_spec(git_repo, "specs/003-x")
    (git_repo / ".specify").mkdir()
    (git_repo / ".specify" / "feature.json").write_text(
        json.dumps({"feature_directory": "specs/003-x"}), encoding="utf-8"
    )
    job = _new_job(git_repo)
    assert _import(git_repo, job) == 0
    assert _stored_job(git_repo, job)["spec_dir"] == "specs/003-x"


def test_spec_import_feature_json_pointing_nowhere_exits_2(git_repo, capsys):
    (git_repo / ".specify").mkdir()
    (git_repo / ".specify" / "feature.json").write_text(
        json.dumps({"feature_directory": "specs/ghost"}), encoding="utf-8"
    )
    job = _new_job(git_repo)
    assert _import(git_repo, job) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
    assert _stored_job(git_repo, job)["pieces"] == []


def test_spec_import_json_output(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [c["ref"] for c in payload["created"]] == ["T001", "T012", "T013"]
    assert payload["missing_proof"] == ["T001", "T012", "T013"]
    assert payload["orphan_sidecar"] == []


def test_spec_status_lists_ready_and_blocked(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    capsys.readouterr()
    assert main(["spec", "status", "--project", str(git_repo), "--job", job]) == 0
    out = capsys.readouterr().out
    assert "Phase 1: Setup: 1/2 accepted" in out      # T002 was done already
    assert "Phase 2: Core: 0/2 accepted" in out
    assert "ready: T001" in out
    assert "T012: phase 'Phase 1: Setup' incomplete" in out
    assert "T013: depends on T012" in out


def test_spec_status_after_accepting_the_first_piece(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    records.update_job(git_repo, job, lambda j: records.mark_accepted(j, 1, {"n": 1, "branch": "b"}))
    capsys.readouterr()
    assert main(["spec", "status", "--project", str(git_repo), "--job", job, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] == ["T012"]
    assert payload["blocked"] == {"T013": "depends on T012"}
    assert payload["phases"][0] == {"phase": "Phase 1: Setup", "accepted": 2, "total": 2}


def test_spec_status_without_spec_exits_2(git_repo, capsys):
    job = _new_job(git_repo)
    assert main(["spec", "status", "--project", str(git_repo), "--job", job]) == 2
