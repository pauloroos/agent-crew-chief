# Cleaner scores, engineers on the board, one scoreboard across machines

**Created**: 2026-11-01
**Status**: Draft

## What the user wants

Three changes for ACC 0.11.0:

1. **Shorter `ACC score`.** With a `models.toml`, `ACC score` prints one table
   row for every listed model with no grade, which is 20 noise rows per kind.
2. **Engineers on the board.** Race engineers are the crew chief's own
   subagents, which are usually not in `models.toml`. Their `coordinate`
   grades are recorded, but nothing points the user to them.
3. **One scoreboard across machines.** Each machine keeps its own
   `~/.crew-chief/ledger.jsonl`, so grades from a PC and a server never meet.
   Users should be able to merge a ledger file by hand, or keep machines
   synced through a private git repository of their own. Both are optional:
   without setup, nothing changes.

## Stories

### User Story 1 - Untried models in one line (P1)

1. **Given** a models.toml with 15 models and 3 graded for `code`, **When**
   `ACC score --kind code`, **Then** the table shows the 3 graded rows and one
   line `not tried yet (12): <executor>, <executor>, ...`.
2. `ACC score --json` and `ACC pick` output are unchanged.

### User Story 2 - Graded but not listed (P2)

1. **Given** `coordinate` grades for `claude:model-x`, which is not in
   models.toml, **When** `ACC models`, **Then** after the table it prints
   `graded but not listed: claude:model-x (coordinate 69.0, n 1)` so the user
   can add it.
2. SKILL.md says that race engineers are by default the crew chief's own
   subagents, and that their grades are recorded under the crew chief's own
   `agent:model`. `ACC pick --kind coordinate` is used only to choose an
   engineer that is not the crew chief itself.

### User Story 3 - Merge a ledger file (P1)

1. **Given** a ledger file copied from another machine, **When** `ACC merge
   <file>`, **Then** grades not yet in the local ledger are appended and
   duplicates are skipped. It prints `merged N new grades from <file> (M
   already here)`, and running it again adds 0.
2. A file that is missing or not a ledger fails with exit 2 and
   `<path>: <problem>`. Bad lines are skipped, as `read()` already does.

### User Story 4 - Sync through a private git repo (P1)

1. **Given** an empty private repo URL, **When** `ACC sync --init <url>` runs
   on machine A, **Then** `$CREW_CHIEF_HOME` becomes a git repository, and
   `ledger.jsonl`, `.gitattributes` and `.gitignore` are committed and pushed.
2. **Given** machine B with its own ledger, **When** `ACC sync --init <url>`
   runs there, **Then** B's grades and A's grades are combined with no
   duplicates, and the result is pushed.
3. **Given** both machines recorded new grades, **When** each runs `ACC sync`,
   **Then** both end with every grade once. A concurrent append never causes
   a merge conflict.
4. **Given** sync was never set up, **When** `ACC sync`, **Then** it prints
   `sync is not set up: ACC sync --init <url of a private repo>` and exits 0.
5. **Given** a network or authentication error, **When** `ACC sync`, **Then**
   it prints git's error and exits 1, and the local ledger is unchanged apart
   from any local commit.

### Decided

- **Identity:** an entry's key is its canonical JSON (`json.dumps(entry,
  sort_keys=True, ensure_ascii=False)`). `ledger.read()` returns each key
  once, which makes duplicates harmless everywhere. No ids are added, and
  existing ledgers work unchanged.
- **Repository contents:**
  - `.gitattributes` holds `ledger.jsonl merge=union text eol=lf`, git's
    built-in union merge for append-only files.
  - `.gitignore` ignores everything except `ledger.jsonl`, `.gitattributes`
    and `.gitignore`. `models.toml` stays per machine, because machines reach
    different models.
- **`ACC sync` steps:**
  1. commit local changes;
  2. `git pull --no-rebase`, with `--allow-unrelated-histories` on the first
     sync of a second machine;
  3. drop exact duplicate lines, keeping their first position, and commit if
     anything changed;
  4. push.
- **Git identity:** commits use `-c user.name=crew-chief -c
  user.email=crew-chief@localhost`, so sync works on machines without a git
  identity and never writes personal data.
- **Credentials:** ACC uses the git access the machine already has. It never
  asks for, stores or creates a credential.
- **Skill:** SKILL.md says that when `ACC sync` is set up, the crew chief runs
  it at the start of a request and after the last grade.

## Requirements

- **FR-001**: `score.render` collapses rows with n 0 into one
  `not tried yet (N): ...` line per kind. JSON is unchanged.
- **FR-002**: `ACC models` prints the `graded but not listed` line for
  executors that have grades but are not in the effective list. With no list,
  nothing changes.
- **FR-003**: `ledger.read()` deduplicates by canonical JSON, keeping the
  first position.
- **FR-004**: `ACC merge <file>` (US3).
- **FR-005**: `ACC sync --init <url>` and `ACC sync` (US4). Standard library
  plus the `git` CLI only.
- **FR-006**: README and pt-BR get a section "Scores on several machines" /
  "Placar em várias máquinas" covering both ways, that they are optional,
  that the repository must be private, and that no tokens are involved.
  SKILL.md covers US2 and the sync rule and stays at most 180 lines.
  CHANGELOG gets [0.11.0], and `__version__ = "0.11.0"`.

## Done when

- **SC-001**:
  - Tests cover the collapsed line, the not-listed hint, dedupe in `read()`,
    `merge` (new, duplicate, rerun, bad file) and `sync`. The `sync` tests use
    two homes and a local bare repository as the remote: init on A, init on
    B with its own grades, concurrent appends on both, rerun idempotent, the
    not-set-up message, and a broken remote that exits 1.
  - CI is green on all 6 jobs.
- **SC-002**: The owner's PC and the VPS container are synced through the
  private repo `pauloroos/crew-chief-ledger`, and both show the same `ACC
  score`.
