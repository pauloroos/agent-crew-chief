# Tasks: Lanes visíveis no Orca numa rodada lançada

<!--
  One line per task: [checkbox] [ID] [P?] [Story?] Description with file path.
  Mark [P] when the task touches different files from its neighbours and has
  no dependency on unfinished tasks. Write "depends on T001, T002" in the
  text for a dependency on another task. Replace the headings' content below.
-->

## Phase 1: Setup

- [ ] T001 [P] branch de feature `acc/lanes-orca` a partir de `main`, com a
  pasta de spec `specs/004-lanes-orca` commitada antes das branches de lane.

## Phase 2: Foundational

- [ ] T002 lane `core` (kind code, risk normal): `ACC launch --engineer` em
  `skill/crew-chief/scripts/crew_chief/launch.py` e
  `skill/crew-chief/scripts/crew_chief/cli.py` (FR-001, FR-002) e o
  `CREW_PROMPT` novo (FR-003). Depende de T001.

## Phase 3: User Story 1 (P1)

- [ ] T003 lane `core` (kind tests, risk normal): teste novo da rodada
  lançada em `tests/test_launch.py` (SC-002) e ajuste do teste do prompt
  `crew` existente, que ainda pede "sem Orca". Depende de T002.

## Phase 4: Polish

- [ ] T004 [P] lane `docs` (kind docs, risk low): versão 0.17.0 em
  `skill/crew-chief/scripts/crew_chief/__init__.py` e em
  `tests/test_docs.py`, seção `## [0.17.0]` no `CHANGELOG.md`, `SKILL.md`
  (seção do launcher + seção 3, até 180 linhas), `pit-wall.md`, `README.md` e
  `README.pt-BR.md` (mesmo conteúdo, com a linha do `--engineer` no bloco do
  launcher) e `docs/tutorial.md` + `docs/tutorial.pt-BR.md` (FR-004, FR-005,
  FR-006). Depende de T001; a forma do comando está fixada em `spec.md`
  (FR-001), então a lane corre em paralelo com a `core`.

- [x] T006 (crew chief, kind code, risk normal, feito): o script da rodada
  acha `cat`, `tee` e `timeout` no terminal do Orca (FR-007) e uma worktree
  ligada não vira projeto no Orca (FR-008), com testes em
  `tests/test_launch.py`. Achado rodando a primeira rodada: a rodada morria
  antes do agente começar. Commitado na branch de feature, antes das lanes.
