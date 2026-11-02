# Tasks: The Pit Wall method

## Phase 1: CLI (lane `core`, kind code)

- [x] T001 `cli.py` / `score.py`: `ACC pick --exclude <agent:model>`
  (repeatable, matches `score.executor` with or without effort), with and
  without models.toml; tests in `tests/test_acc.py` / `tests/test_models.py`
  written first.

- [x] T007 New ledger kind `coordinate` in `ledger.KINDS` (record, score,
  pick, models.toml kinds accept it); tests. Depends on T001 (same lane).

## Phase 2: Method and docs (lane `docs`, kind docs) [P]

- [x] T002 [P] `skill/crew-chief/pit-wall.md`: the whole method (spec "The
  method" section), with the plain-mode and Orca commands for Fresh tyres,
  scrutineers and the pit stop.
- [x] T003 [P] `skill/crew-chief/templates/scrutineer.md` (FR-003).
- [x] T004 `skill/crew-chief/SKILL.md`: step 1 marks risk and reads the pace;
  step 2 uses `--exclude` and the pace's max cost; step 5 points to
  `pit-wall.md` for Relay, Fresh tyres and Scrutineering, replacing the
  current retry text; the approval stop and report show risk/pattern. At most
  180 lines. Depends on T002.
- [x] T005 `README.md` + `README.pt-BR.md` section "The Pit Wall method" /
  "O método Pit Wall"; `docs/tutorial*.md` mention of pace; `CHANGELOG.md`
  [0.10.0]; `__version__ = "0.10.0"`, version pins and `tests/test_docs.py`
  (version + new term checks, SC-001). Depends on T002.

- [x] T008 The team (spec "The team"): `templates/race-engineer.md` (the
  engineer brief: lane, jobs, pace, rules, report format); SKILL.md becomes
  the crew chief's guide (dispatch one race engineer per lane, check and
  grade it as `coordinate`); `pit-wall.md` is written for the race engineer;
  README/pt-BR name crew chief / race engineer (engenheiro de corrida) /
  driver (piloto) / scrutineer (fiscal); CHANGELOG. Depends on T002-T005.

## Phase 3: Validate and deliver (coordinator)

- [x] T006 Merge both lanes, full tests, then the SC-002 dry run (crew chief dispatching a race engineer) on a copy of
  `examples/hello-crew` (a `high` job, two scrutineers, a pit stop), record
  the grades, push, PR, CI.
