# Your models (models.toml)

**Created**: 2026-11-01
**Status**: Draft

## What the user wants

Since 0.7, `ACC pick` only knows the models in the ledger: a model with no
grade is never a candidate, and the coordinator cannot tell which models the
user has or wants in a project. Users declare their models in a
`models.toml`, machine-wide and per project; `pick` and `score` respect it.
ACC never discovers or guesses models.

## Stories

### User Story 1 - Declare models once (Priority: P1)

The user writes `~/.crew-chief/models.toml` with the models their CLIs reach,
and optionally `.crew-chief/models.toml` in a repository to narrow or change
that list.

**Acceptance Scenarios**:

1. **Given** no models file anywhere, **When** the coordinator runs
   `ACC pick --kind code`, **Then** the output is exactly the 0.8 output.
2. **Given** a global list, **When** it runs `ACC models`, **Then** it sees
   every model with its source (global/project), cost, kinds, note and its
   score for each kind, or "not tried yet".
3. **Given** a global list and a project file with `inherit = false`, **When**
   it runs `ACC pick`, **Then** only project models are candidates.
4. **Given** a lane worktree, **When** `ACC models` runs there, **Then** it
   reads the main repository's `.crew-chief/models.toml`.

### User Story 2 - Pick among my models (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a list with a proven model (score >= 70 over >= 2 jobs), **When**
   `ACC pick --kind code`, **Then** the cheapest proven model wins, as in 0.8.
2. **Given** a list with nothing proven for the kind, **When** `ACC pick`,
   **Then** it suggests an untried model of the cheapest tier ("not tried
   yet"), else the best "not proven yet" one.
3. **Given** ledger grades for a model not in the list, **When** `ACC pick`,
   **Then** that model is never suggested (it still shows in `ACC score`).

### Open questions and edge cases (decided)

- Matching a declared model to ledger grades: same `agent` and `model`; when
  the entry sets `effort`, only grades with that effort.
- Cost for tiers and `--max-cost` comes from the declared `cost`, not the
  ledger.
- A project entry with the same `agent`+`model` as a global one overrides
  only the fields it sets, so `enabled = false` alone switches a model off.
  `cost` is required on the merged entry, not on every override.
- `enabled = false` in either file drops the model from the effective list.
- `inherit` is valid only in the project file; in the global file it is an
  unknown key (error).
- `ACC score` with `--kind`: declared models that accept the kind and have no
  grade get a row with n 0 and score "-". Without `--kind`: one extra section
  "not tried yet" lists declared models with no grade at all.
- An invalid file stops `models`, `pick` and `score` with exit 2 and
  `<path>: <problem>`; `record` and `import` do not read the files.

## Requirements

- **FR-001**: Read `$CREW_CHIEF_HOME/models.toml` (default
  `~/.crew-chief/models.toml`) and `<main repo root>/.crew-chief/models.toml`,
  both optional, with `tomllib`; the repo root comes from
  `git rev-parse --git-common-dir`, as `_project_name()` does.
- **FR-002**: Schema: top level `inherit` (bool, project only) and `model`
  (array of tables). Per model: `agent` (str, required), `model` (str,
  required), `cost` (one of free/included/cheap/paid, required after merge),
  `effort` (str), `kinds` (list of ledger kinds; absent = all), `enabled`
  (bool, default true), `note` (str). Any other key, a wrong type, a bad cost
  or kind, or broken TOML is an error naming the file and the problem.
- **FR-003**: `ACC models [--json]` prints the effective list.
- **FR-004**: `ACC pick --kind X [--max-cost C] [--json]` follows the stories
  above when any list exists, and is unchanged when none does.
- **FR-005**: `ACC score` adds the unscored declared models as above.
- **FR-006**: No field is read as a secret; the docs say never to put keys or
  tokens in the file.
- **FR-007**: SKILL.md step 2 gains: "Run `ACC models` first. If it lists
  models, pick only among them (`ACC pick`); the notes say when to use each."
  SKILL.md stays at or under 180 lines.
- **FR-008**: README and README.pt-BR get "Your models" / "Seus modelos";
  `examples/models.toml` is commented with generic names; tutorial updated
  where it shows `pick`; CHANGELOG [0.9.0]; `__version__ = "0.9.0"`.

## Done when

- **SC-001**: The tests listed in tasks.md pass, along with the 40 tests from 0.8, on
  Windows locally and in CI (Windows/Linux/macOS, Python 3.11 and 3.13).
- **SC-002**: In `examples/hello-crew` with a sample global and project file,
  `ACC models` and `ACC pick --kind code` print the expected list and pick.
- **SC-003**: Nothing personal (hosts, IPs, account or private combo names,
  keys) in code, tests, examples or docs.
