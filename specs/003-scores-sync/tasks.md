# Tasks: scores, engineers, sync

## Lane `core` (race engineer; jobs in order)

- [ ] T001 [US1, US2] kind code, risk normal: `score.render` collapses n 0
  rows (FR-001); `ACC models` "graded but not listed" line (FR-002); tests.
- [ ] T002 [US3, US4] kind code, risk **high** (it rewrites the user's
  ledger): `ledger.read()` dedupe (FR-003), `ACC merge` (FR-004), `ACC sync
  --init` / `ACC sync` (FR-005) in a new `sync.py`; tests from SC-001 written
  first. Depends on T001 (same files: cli.py).

## Lane `docs` (race engineer) [P]

- [ ] T003 kind docs, risk low: SKILL.md (engineers are your own subagents,
  record them as yourself; sync at start and end when set up; `ACC merge` /
  `ACC sync` in the command mentions), README + pt-BR section, the
  `ACC score` example if it shows untried rows, CHANGELOG [0.11.0], version
  0.11.0 everywhere, `tests/test_docs.py`.

## Crew chief

- [ ] T004 Merge lanes, full tests, push, PR, CI; after merge and release:
  create the private repo `pauloroos/crew-chief-ledger`, `ACC sync --init` on
  the PC and in the VPS container, check both `ACC score` match (SC-002).
