# Tasks: Your models (models.toml)

## Phase 1: Core (lane `core`, kind code)

- [ ] T001 [US1] `skill/crew-chief/scripts/crew_chief/models.py`: find the two
  files (repo root via git-common-dir, shared with `cli._project_name`), parse
  with `tomllib`, validate (FR-002), merge global + project (override by
  agent+model, `enabled`, `inherit`). Raise one error type with
  `<path>: <problem>`.
- [ ] T002 [US1] `cli.py`: `ACC models [--json]`; exit 2 with the error line
  on an invalid file. Depends on T001.
- [ ] T003 [US2] `score.py` + `cli.py`: `pick` restricted to the effective
  list (proven cheapest -> untried cheapest tier -> best not proven; declared
  cost for tiers and `--max-cost`); `score` adds n 0 rows / "not tried yet".
  No list = 0.8 behavior. Depends on T001.
- [ ] T004 [US1, US2] `tests/test_models.py`: no file (same as 0.8); only
  global; only project; global + project override; `enabled = false`;
  `inherit = false`; kinds; `--max-cost`; invalid files (broken TOML, bad
  cost, missing field, unknown key, `inherit` in global); a lane worktree
  reads the main repo's file. Written first (TDD) alongside T001-T003.

## Phase 2: Docs (lane `docs`, kind docs) [P]

- [ ] T005 [P] `skill/crew-chief/SKILL.md` step 2 (FR-007), still <= 180 lines.
- [ ] T006 [P] `README.md` + `README.pt-BR.md`: "Your models" / "Seus
  modelos" (two places, merge, example, never keys or tokens); `ACC models`
  in the command list.
- [ ] T007 [P] `examples/models.toml`, commented, generic names.
- [ ] T008 [P] `docs/tutorial.md` + `.pt-BR.md` where they show `pick`;
  `CHANGELOG.md` [0.9.0]; `__version__ = "0.9.0"` and `tests/test_docs.py`
  version pins (v0.9.0 in READMEs and tutorials).

## Phase 3: Deliver (coordinator)

- [ ] T009 Merge both lanes into `acc/models-toml`, full test suite, run
  `ACC models` and `ACC pick --kind code` in `examples/hello-crew` with a
  sample global + project file, push, open the PR, check CI.
