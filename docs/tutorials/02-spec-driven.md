# Spec-driven work

This tutorial builds a small feature from a spec-kit-style folder. You will add two pure text helpers and focused tests. It assumes you have read [Your first job in 10 minutes](01-first-job.md), so it links to that tutorial for copying the example, installing ACC, setting the isolated preferences file, and discovering executors.

All execution work in this tutorial uses free OpenCode models. Do not dispatch an included, cheap, or paid executor.

## 1. Prepare the lab

Follow steps 1–4 of [the first tutorial](01-first-job.md), but use a separate copy such as `~/projects/hello-crew-spec`. Before every ACC command, export the lab preferences path:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
```

The setup command produced this real tier summary in the lab:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

The exact counts depend on your machine. If there is no free OpenCode executor, stop and configure one. Do not use a paid fallback for this tutorial.

## 2. Create the spec folder

From the copied Git project, create the numbered spec folder:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec new --project . --title "Text tools"
```

You should see:

```text
specs/001-text-tools
```

If a different number appears, use the folder printed by your command. Replace the generated template contents with this minimal `spec.md`:

```markdown
# Text tools

**Created**: 2026-10-20
**Status**: Draft

## What the user wants

A small text-tools module should make common text cleanup predictable. It will provide a normalize helper and a word-count helper with focused tests.

## Stories

### User Story 1 - Clean and count text (Priority: P1)

As a Python caller, I want to normalize text and count its words so that small text-processing tasks use one consistent module.

**Acceptance Scenarios**:

1. **Given** text with extra whitespace, **When** the caller normalizes it, **Then** leading and trailing whitespace is removed and internal runs become one space.
2. **Given** normalized or ordinary text, **When** the caller counts words, **Then** the result is the number of non-empty whitespace-separated words.

### Open questions and edge cases

- Empty or whitespace-only text should normalize to an empty string and count as zero words.

## Requirements

- **FR-001**: Provide `normalize(text)` with the whitespace behavior in the acceptance scenarios.
- **FR-002**: Provide `word_count(text)` that returns the number of non-empty whitespace-separated words.

## Done when

- **SC-001**: Focused tests cover ordinary, repeated-whitespace, and empty input for both helpers.
```

Use this `plan.md`:

```markdown
# Implementation Plan: Text tools

**Spec**: spec.md

## Summary

Add a small stdlib-only module with two pure functions. Keep the public API simple and verify behavior with focused pytest tests.

## Technical Context

- **Language/Version**: Python 3.11+
- **Dependencies**: none at runtime
- **Storage**: n/a
- **Testing**: pytest

## Project Structure

```text
src/hello_crew/text_tools.py
 tests/test_text_tools.py
```

## Decisions

- Use `str.split()` for whitespace handling because it drops empty runs without extra parsing.
```

Use these task lines in `tasks.md`:

```markdown
# Tasks: Text tools

## Phase 1: Foundational

- [ ] T001 [P] [US1] Add `normalize(text)` in src/hello_crew/text_tools.py
- [ ] T002 [P] [US1] Add `word_count(text)` in src/hello_crew/text_tools.py

## Phase 2: Verification

- [ ] T003 [US1] Add focused tests in tests/test_text_tools.py; depends on T001, T002
```

`[P]` says a task may run in parallel. Here T001 and T002 both use the same file, so ACC warns about that conflict. Run them one at a time.

## 3. Add the crew.toml sidecar

The spec describes the feature, but the executor also needs allowed files, a proof command, and dependencies. Create `specs/001-text-tools/crew.toml`:

```toml
global_constraints = ["Python 3.11+, stdlib only at runtime", "Keep public helpers pure and small"]

[tasks.T001]
type = "code"
risk = "low"
accept = ["FR-001", "US1/AC1"]
files = ["src/hello_crew/text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = []
interfaces = { normalize = "def normalize(text) -> str" }

[tasks.T002]
type = "code"
risk = "low"
accept = ["FR-002", "US1/AC2"]
files = ["src/hello_crew/text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = []
interfaces = { word_count = "def word_count(text) -> int" }

[tasks.T003]
type = "tests"
risk = "low"
accept = ["SC-001"]
files = ["tests/test_text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = ["T001", "T002"]
interfaces = {}
```

Keep the task text that `spec import` writes into each sidecar entry when you use a newer or older ACC version. That text lets ACC detect stale pieces later.

## 4. Create and import the job

Write the request, then create the job. The first line is ordinary shell work; the second command is ACC:

```bash
printf '%s\n' 'Implement the text-tools spec in specs/001-text-tools. For T001, add only src/hello_crew/text_tools.py with normalize(text): use split-and-join whitespace normalization. Run the proof from the generated brief.' > .crew-chief-request.md

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC job new --project . --title "Text tools" --request-file .crew-chief-request.md
```

The lab printed this job id:

```text
20261020-1452-text-tools
```

Your id will differ. Use it below as `JOB`.

Import the spec:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec import --project . --job JOB --spec-dir specs/001-text-tools
```

The lab printed:

```text
piece 1: T001 (code, low)
piece 2: T002 (code, low)
piece 3: T003 (tests, low)
parallel conflict: T001/T002 share src/hello_crew/text_tools.py
```

The conflict is a warning, not a reason to stop. Do not dispatch those two pieces at the same time.

Check readiness:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec status --project . --job JOB
```

You should see:

```text
Phase 1: Foundational: 0/2 accepted
Phase 2: Verification: 0/1 accepted
ready: T001, T002
blocked:
  T003: depends on T001
```

## 5. Render a task brief and dispatch it

Render T001 instead of writing a large hand-made brief:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --out .crew-chief-brief.md
```

The lab printed `wrote .crew-chief-brief.md`. The brief contained the acceptance criteria, constraints, interface, allowed file, and this proof:

```text
## Proof
Command: `python -m pytest tests/test_text_tools.py -q`
Expect: passed
```

Pick a free OpenCode executor:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

The lab picked:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use the exact free OpenCode id printed by your run.

### A real proof failure

The first lab dispatch returned `proof_failed`. The executor changed a file outside T001's allowed list, so the generated tests were not where the proof expected them:

```text
status: proof_failed
branch: crew/20261020-1452-text-tools-1-1
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 27.5s
steps: 10
tokens: 233672 (in 230748 / out 2924)
proof: failed (exit 4)
scope: src/hello_crew/__init__.py
```

Read the recorded attempt before retrying. Do not accept `proof_failed`; it counts as a failed attempt.

Pick another free OpenCode model and dispatch again:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude opencode:opencode-go/beam-free

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

The successful lab attempt used `opencode:opencode-go/minimax-m3.1-flash-free` and printed:

```text
status: ok
branch: crew/20261020-1452-text-tools-1-2
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 129.0s
steps: 21
tokens: 942551 (in 932015 / out 10536)
proof: passed
scope: tests/test_text_tools.py
```

The `scope:` line matters. The proof passed, but the executor also added the test file. That is outside T001's `files` list, so the reviewer called it an extra and the grade was 90. Decide whether that extra belongs in a later task before accepting similar work.

## 6. Review with a rendered brief

Choose a reviewer from a different family, then render the review brief:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude EXECUTOR

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --review --out .crew-chief-review.md
```

The lab first picked an Antigravity Claude reviewer, but dispatch returned this honest quota result:

```text
status: quota
branch: -
worktree: -
base: -
destructive_suspect: -
outside_writes: -
duration: 9.5s
steps: 1
tokens: 0 (in 0 / out 0)
answer:
Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 140h1m53s.
```

Pick again, excluding that reviewer. The lab then used free `opencode:opencode/beam-free` and reviewed the successful worktree:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

The review returned `status: ok`, `HEAD confirmed`, and these verdicts:

```text
## Spec verdict
- FR-001: done
- US1/AC1: done

`extra`: Added `tests/test_text_tools.py` (not in the allowed-files list, though required by the proof command); added `__all__` and a detailed docstring beyond the requested helper.

## Quality verdict
No Critical or Important findings.
```

Use the `worktree` from the successful implementation dispatch, not a new path. If the reviewer reports the wrong `HEAD`, discard that review and dispatch it again against the correct worktree.

## 7. Accept tasks and run their proofs

Grade and accept T001 after reading the review:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 2 --missing 0 --extra 2

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

The lab printed:

```text
score: 90
adherence 2/2 → 100; 0 corrections; 2 extra −10; penalties none → 90
tasks.md has other changes; marked T001 but did not commit
accepted crew/20261020-1452-text-tools-1-2
```

The accept command ticks T001 from `[ ]` to `[X]`. Because the spec files had other changes, it did not make a separate task-marking commit.

Repeat the same render, pick, dispatch, grade, and accept cycle for T002. In the lab, the free OpenCode executor was `opencode:opencode/glm-5.3-flash-free`; its proof passed and its grade was 100. Then dispatch T003, which is now unblocked. The lab used `opencode:opencode/deepseek-v4.1-flash-free`; its proof passed and its grade was 100.

Check the completed spec:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec status --project . --job JOB
```

The lab printed:

```text
Phase 1: Foundational: 2/2 accepted
Phase 2: Verification: 1/1 accepted
ready: -
blocked: -
```

## 8. Record a decision and close

For a durable design choice, record a one-line ruling and promote it to an ADR:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC ruling add --project . --job JOB --text "Keep text helpers pure and stdlib-only — predictable whitespace behavior is enough for this small feature — adding validation now would expand the API."

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC decision add --project . --job JOB --from-ruling 1 --context "The feature needs two small helpers with no runtime dependency." --alternatives "Add a third-party text library; validate every input type." --consequences "Callers get a small predictable API; non-string validation remains outside this feature."
```

The lab printed `ruling 1 recorded` and created:

```text
docs/decisions/0001-keep-text-helpers-pure-and-stdlib-only.md
```

Close the feature:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC close --project . --job JOB --type feat
```

The lab printed `changelog: CHANGELOG.md (Added)`, a feature commit message, the task/executor scores, and this economy line:

```text
free work: 100% of pieces, 100% of tokens · coordinator: 0% · 1.7 attempts/piece · mean score 97
```

`close` adds the job to `CHANGELOG.md` and prints the commit message. Commit the accepted work, the ADR, and the changelog entry as your project workflow requires.

## If you edit tasks.md later

A piece remembers the task text it was imported from. If you regenerate or edit `tasks.md`, the piece becomes stale when that text changes. Run `spec import` again after fixing the task and sidecar. Until then, `brief render` and `dispatch` refuse the stale piece. If spec-kit renumbers an id, also check the sidecar's `task` text; ACC warns when an entry such as T012 was written for a different task. Do not bypass this with `--ignore-deps`: that option is for dependency checks, not stale task text.

If you add a new task, add its sidecar table and proof before dispatching. If you change allowed files, acceptance references, or the proof, render a fresh brief and read the new `scope:` line before accepting.

## What happened in this run

- Files written: `spec.md`, `plan.md`, `tasks.md`, `crew.toml`, the three task outputs, an ADR, and `CHANGELOG.md` in the lab.
- Commands run: `spec new`, `setup`, `discover`, `job new`, `spec import`, `spec status`, `brief render`, `pick`, `dispatch`, `grade`, `accept`, `ruling add`, `decision add`, and `close`.
- Product notes: a proof can pass while `scope:` reports an extra file; `accept` warns and does not make its own commit when `tasks.md` already has other changes; a quota response is recorded as a failed attempt and needs another free reviewer.
