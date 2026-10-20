"""Tests for ``spec new``, ``spec import`` and ``spec status``."""

from __future__ import annotations

import json
import shutil
import subprocess
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
    assert "stale pieces: 2 (T012)" in out
    assert "orphan pieces: T013" in out
    pieces = _stored_job(git_repo, job)["pieces"]
    by_ref = {p["ref"]: p for p in pieces}
    assert pieces[1]["title"] == "T012 Create User model in src/a.py"   # untouched
    assert pieces[1]["stale"] is True and "stale" not in by_ref["T020"]
    assert by_ref["T012"]["title"] == "T012 Totally different work"     # the new piece
    assert "T020" in by_ref and "T021" in by_ref
    assert len(pieces) == 6
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


RENUMBERED = (
    "## Phase 1: Setup\n- [ ] T001 Create project structure\n"
    "## Phase 2: Core\n"
    "- [ ] T012 Totally different work\n"
    "- [ ] T020 [P] Create User model in src/a.py\n"
)


def test_reimport_after_renumber_marks_old_piece_stale_and_creates_new(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    first = {p["ref"]: p for p in _stored_job(git_repo, job)["pieces"]}
    assert first["T012"]["task_text"] == "Create User model in src/a.py"
    _write_spec(git_repo, "specs/003-x", RENUMBERED)
    capsys.readouterr()
    assert _import(git_repo, job) == 0
    out = capsys.readouterr().out
    assert "stale pieces: 2 (T012)" in out
    pieces = _stored_job(git_repo, job)["pieces"]
    old = next(p for p in pieces if p["n"] == 2)
    assert old["stale"] is True and old["ref"] == "T012"
    assert old["task_text"] == "Create User model in src/a.py"       # kept for history
    new = [p for p in pieces if p["ref"] == "T012" and not p.get("stale")]
    assert len(new) == 1 and new[0]["n"] > 3
    assert new[0]["title"] == "T012 Totally different work"
    assert new[0]["task_text"] == "Totally different work"
    assert records.piece_for_ref({"pieces": pieces}, "T012")["n"] == new[0]["n"]
    # a third import changes nothing
    assert _import(git_repo, job) == 0
    assert len(_stored_job(git_repo, job)["pieces"]) == len(pieces)
    assert "stale pieces" not in capsys.readouterr().out


def test_reimport_detects_staleness_of_pieces_without_task_text(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0

    def drop_task_text(stored):
        for piece in stored["pieces"]:
            piece.pop("task_text", None)

    records.update_job(git_repo, job, drop_task_text)
    _write_spec(git_repo, "specs/003-x", RENUMBERED)
    capsys.readouterr()
    assert _import(git_repo, job) == 0
    assert "stale pieces: 2 (T012)" in capsys.readouterr().out


def test_reimport_with_unchanged_text_marks_nothing_stale(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert _import(git_repo, job) == 0
    assert not any(p.get("stale") for p in _stored_job(git_repo, job)["pieces"])


def test_spec_status_ignores_stale_pieces_and_lists_them(git_repo, capsys):
    _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    records.update_job(git_repo, job, lambda j: records.mark_accepted(j, 2, {"n": 1, "branch": "b"}))
    _write_spec(git_repo, "specs/003-x", RENUMBERED)
    assert _import(git_repo, job) == 0
    capsys.readouterr()
    assert main(["spec", "status", "--project", str(git_repo), "--job", job, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    # the accepted piece belonged to the OLD T012, so the new T012 is not accepted
    assert payload["phases"][1] == {"phase": "Phase 2: Core", "accepted": 0, "total": 2}
    assert payload["stale"] == [{"piece": 2, "ref": "T012"}]
    assert "T012" in payload["ready"] or "T012" in payload["blocked"]
    assert main(["spec", "status", "--project", str(git_repo), "--job", job]) == 0
    assert "stale: 2 (T012)" in capsys.readouterr().out


# --- accept marks [X]; dispatch waits for dependencies and phases ----------

_BOM = "\ufeff"
_STUB_PREFS = """
[[tools]]
name = "stub"
kind = "generic"
bin = "stub"
models = ["m"]
run_cmd = ["stub", "{prompt}"]

[[models]]
match = "stub:*"
cost = "free"
"""
_STUB = (
    "from pathlib import Path\n"
    "Path('out.py').write_text('x = 1\\n', encoding='utf-8')\n"
    "print('done')\n"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return result.stdout


def _imported(repo: Path, tasks_bytes: bytes) -> tuple[str, Path]:
    """Spec folder holding ``tasks_bytes`` (committed), imported into a job."""
    folder = repo / "specs" / "003-x"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text("# Spec\n", encoding="utf-8")
    (folder / "tasks.md").write_bytes(tasks_bytes)
    _git(repo, "add", "specs/003-x/tasks.md")
    _git(repo, "commit", "-m", "add tasks")
    job = _new_job(repo)
    assert _import(repo, job, "--spec-dir", "specs/003-x") == 0
    return job, folder


def _with_ok_attempt(repo: Path, job: str, piece: int) -> None:
    """Give ``piece`` an ok exec attempt whose branch adds ``out.py``."""
    from crew_chief.worktree import commit_worktree, create_worktree

    worktree, branch = create_worktree(repo, job, piece, 1)
    (worktree / "out.py").write_text("x = 1\n", encoding="utf-8")
    commit_worktree(worktree, "crew change")
    attempt = {
        "n": 1, "executor": "stub:m", "role": "exec", "status": "ok",
        "branch": branch, "worktree": str(worktree),
    }
    records.update_job(repo, job, lambda j: records.add_attempt(j, piece, attempt))


def test_accept_marks_task_done_and_commits(git_repo, capsys):
    text = SAMPLE.replace("T012 [P] Create User model", "T012 [P] Criar modelo Usuário")
    crlf = (_BOM + text).replace("\n", "\r\n").encode("utf-8")
    job, folder = _imported(git_repo, crlf)
    _with_ok_attempt(git_repo, job, 2)
    capsys.readouterr()

    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0

    expected = crlf.replace(b"- [ ] T012", b"- [X] T012")
    assert (folder / "tasks.md").read_bytes() == expected
    assert _git(git_repo, "log", "-1", "--format=%s").strip() == "docs(tasks): mark T012 done"
    assert _git(git_repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "specs/003-x/tasks.md"
    ]
    assert "accepted" in capsys.readouterr().out
    assert _stored_job(git_repo, job)["pieces"][1]["accepted"]


def test_accept_warns_when_task_vanished(git_repo, capsys):
    job, folder = _imported(git_repo, SAMPLE.encode("utf-8"))
    _with_ok_attempt(git_repo, job, 2)
    (folder / "tasks.md").write_text(SAMPLE.replace("T012", "T099"), encoding="utf-8")
    _git(git_repo, "add", "specs/003-x/tasks.md")
    _git(git_repo, "commit", "-m", "renumber")
    capsys.readouterr()

    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0

    assert "task T012 not found in tasks.md; not marked" in capsys.readouterr().err
    assert "docs(tasks)" not in _git(git_repo, "log", "--format=%s")
    assert (folder / "tasks.md").read_text(encoding="utf-8") == SAMPLE.replace("T012", "T099")


def test_accept_does_not_mark_stale_piece(git_repo, capsys):
    job, folder = _imported(git_repo, SAMPLE.encode("utf-8"))
    _with_ok_attempt(git_repo, job, 2)
    records.update_job(git_repo, job, lambda j: j["pieces"][1].update(stale=True))
    capsys.readouterr()

    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0

    assert "stale" in capsys.readouterr().err
    assert (folder / "tasks.md").read_text(encoding="utf-8") == SAMPLE
    assert "docs(tasks)" not in _git(git_repo, "log", "--format=%s")


def test_accept_without_spec_makes_no_tasks_commit(git_repo, capsys):
    job = records.new_job(git_repo, "Plain", "do it", True, "one part")
    records.add_piece(job, "One", "code", "low")
    records.save_job(git_repo, job)
    _with_ok_attempt(git_repo, job["id"], 1)

    assert main(["accept", "--project", str(git_repo), "--job", job["id"], "--piece", "1"]) == 0
    assert "docs(tasks)" not in _git(git_repo, "log", "--format=%s")


def _dispatch_repo(repo: Path, fake_bin, monkeypatch) -> Path:
    path = repo / "docs" / "crew-chief" / "preferences.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB_PREFS, encoding="utf-8")
    fake_bin("stub", _STUB)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda cmd, *a, **k: real_which(cmd, *a, **k) if cmd == "stub" else None,
    )
    brief = repo.parent / "brief.md"
    brief.write_text("Create out.py.\n", encoding="utf-8")
    return brief


def _dispatch(repo: Path, job: str, piece: int, brief: Path, *extra: str) -> int:
    return main(["dispatch", "--project", str(repo), "--job", job, "--piece", str(piece),
                 "--executor", "stub:m", "--brief-file", str(brief), *extra])


def test_dispatch_waits_for_dependencies(git_repo, fake_bin, monkeypatch, capsys):
    brief = _dispatch_repo(git_repo, fake_bin, monkeypatch)
    job, _ = _imported(git_repo, SAMPLE.encode("utf-8"))
    capsys.readouterr()

    assert _dispatch(git_repo, job, 3, brief, "--no-proof") == 2

    err = capsys.readouterr().err.strip()
    assert err.splitlines() == ["piece 3 (T013) waits for T012"]
    assert _stored_job(git_repo, job)["pieces"][2]["attempts"] == []


def test_dispatch_waits_for_phase(git_repo, fake_bin, monkeypatch, capsys):
    brief = _dispatch_repo(git_repo, fake_bin, monkeypatch)
    job, _ = _imported(git_repo, SAMPLE.encode("utf-8"))
    capsys.readouterr()

    assert _dispatch(git_repo, job, 2, brief, "--no-proof") == 2

    assert "piece 2 (T012) waits for phase 'Phase 1: Setup'" in capsys.readouterr().err


def test_dispatch_ignore_deps_records_them(git_repo, fake_bin, monkeypatch, capsys):
    brief = _dispatch_repo(git_repo, fake_bin, monkeypatch)
    job, _ = _imported(git_repo, SAMPLE.encode("utf-8"))

    assert _dispatch(git_repo, job, 3, brief, "--no-proof", "--ignore-deps") == 0

    attempt = _stored_job(git_repo, job)["pieces"][2]["attempts"][0]
    assert attempt["ignored_deps"] == ["T012"]


def test_dispatch_proceeds_once_dependencies_are_accepted(
    git_repo, fake_bin, monkeypatch, capsys
):
    brief = _dispatch_repo(git_repo, fake_bin, monkeypatch)
    job, _ = _imported(git_repo, SAMPLE.encode("utf-8"))
    for piece in (1, 2):
        records.update_job(
            git_repo, job,
            lambda j, p=piece: records.mark_accepted(j, p, {"n": 1, "branch": "b"}),
        )

    assert _dispatch(git_repo, job, 3, brief, "--no-proof") == 0

    assert "ignored_deps" not in _stored_job(git_repo, job)["pieces"][2]["attempts"][0]


def test_dispatch_review_is_not_blocked_by_dependencies(
    git_repo, fake_bin, monkeypatch, capsys
):
    brief = _dispatch_repo(git_repo, fake_bin, monkeypatch)
    job, _ = _imported(git_repo, SAMPLE.encode("utf-8"))
    assert _dispatch(git_repo, job, 1, brief, "--no-proof") == 0     # T001 is ready
    worktree = _stored_job(git_repo, job)["pieces"][0]["attempts"][0]["worktree"]
    capsys.readouterr()

    code = _dispatch(git_repo, job, 3, brief, "--review", "--target", worktree)

    assert code == 0
    assert "waits for" not in capsys.readouterr().err


def test_accept_writes_tasks_md_atomically(git_repo, capsys, monkeypatch):
    crlf = (_BOM + SAMPLE).replace("\n", "\r\n").encode("utf-8")
    job, folder = _imported(git_repo, crlf)
    _with_ok_attempt(git_repo, job, 2)
    calls = []
    real = records.write_atomic
    monkeypatch.setattr(
        records, "write_atomic", lambda path, text: (calls.append(path), real(path, text))
    )
    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0
    assert (folder / "tasks.md").resolve() in [Path(p).resolve() for p in calls]
    assert (folder / "tasks.md").read_bytes() == crlf.replace(b"- [ ] T012", b"- [X] T012")


def test_accept_does_not_commit_tasks_md_with_other_changes(git_repo, capsys):
    job, folder = _imported(git_repo, SAMPLE.encode("utf-8"))
    _with_ok_attempt(git_repo, job, 2)
    edited = SAMPLE + "- [ ] T014 Added by hand, not committed\n"
    (folder / "tasks.md").write_text(edited, encoding="utf-8")
    capsys.readouterr()

    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0

    err = capsys.readouterr().err
    assert "tasks.md has other changes; marked T012 but did not commit" in err
    assert "docs(tasks)" not in _git(git_repo, "log", "--format=%s")
    assert (folder / "tasks.md").read_text(encoding="utf-8") == edited.replace(
        "- [ ] T012", "- [X] T012"
    )


def test_accept_does_not_commit_an_untracked_tasks_md(git_repo, capsys):
    folder = _write_spec(git_repo, "specs/003-x")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    _with_ok_attempt(git_repo, job, 2)
    capsys.readouterr()

    assert main(["accept", "--project", str(git_repo), "--job", job, "--piece", "2"]) == 0

    assert "tasks.md has other changes; marked T012 but did not commit" in capsys.readouterr().err
    assert "docs(tasks)" not in _git(git_repo, "log", "--format=%s")
    assert "- [X] T012" in (folder / "tasks.md").read_text(encoding="utf-8")
    assert "specs/003-x/tasks.md" not in _git(git_repo, "ls-files")


def test_spec_import_refuses_to_rebind_a_job_to_another_folder(git_repo, capsys):
    _write_spec(git_repo, "specs/001-a")
    _write_spec(git_repo, "specs/002-b")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/001-a") == 0
    before = (records.jobs_dir(git_repo) / f"{job}.json").read_text(encoding="utf-8")
    capsys.readouterr()

    assert _import(git_repo, job, "--spec-dir", "specs/002-b") == 2

    err = capsys.readouterr().err.strip()
    assert err.splitlines() == ["job is bound to specs/001-a; use a new job"]
    assert (records.jobs_dir(git_repo) / f"{job}.json").read_text(encoding="utf-8") == before
    assert not (git_repo / "specs" / "002-b" / SIDECAR_NAME).exists()
    # the same folder again is fine
    assert _import(git_repo, job, "--spec-dir", "specs/001-a") == 0


def test_spec_import_keeps_crew_toml_comments(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    original = (
        "# my notes about proofs\n"
        '[tasks.T012]\ntype = "data"\nrisk = "high"\nproof = "python check.py"\n'
        "# keep this\n"
    )
    (spec / SIDECAR_NAME).write_text(original, encoding="utf-8")
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    text = (spec / SIDECAR_NAME).read_text(encoding="utf-8")
    assert text.startswith("# my notes about proofs\n")
    assert "# keep this\n" in text
    assert "crew.toml rewritten" not in capsys.readouterr().err
    sidecar = load_sidecar(spec)
    assert sidecar.job == job and sidecar.spec_dir == "specs/003-x"
    assert set(sidecar.tasks) == {"T001", "T012", "T013"}
    assert sidecar.tasks["T012"].proof == "python check.py"


def test_spec_import_warns_when_crew_toml_must_be_rewritten(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    (spec / SIDECAR_NAME).write_text(
        '# dotted\n[tasks]\nT013.type = "code"\nT013.risk = "low"\n', encoding="utf-8"
    )
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 0
    assert "crew.toml rewritten; comments are not kept" in capsys.readouterr().err


def test_spec_import_rejects_unknown_crew_toml_key(git_repo, capsys):
    spec = _write_spec(git_repo, "specs/003-x")
    (spec / SIDECAR_NAME).write_text(
        '[tasks.T012]\ntype = "code"\nrisk = "low"\nexpectt = "passed"\n', encoding="utf-8"
    )
    job = _new_job(git_repo)
    assert _import(git_repo, job, "--spec-dir", "specs/003-x") == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "T012" in err[0] and "unknown key 'expectt'" in err[0]
    assert _stored_job(git_repo, job)["pieces"] == []
