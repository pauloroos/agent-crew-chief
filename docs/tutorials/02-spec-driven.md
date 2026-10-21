# Spec-driven work

This tutorial builds a small feature from a spec-kit-style folder. You will add two pure text helpers and focused tests. It assumes you have read [Your first job in 10 minutes](01-first-job.md), so it links to that tutorial for copying the example, installing ACC, setting the isolated preferences file, and discovering executors.

All execution work in this tutorial uses free OpenCode models. Do not dispatch an included, cheap, or paid executor.

> **How to read the outputs in this tutorial.** The command outputs quoted here come from an earlier lab run of this tutorial, made with the old proof of step 3. The updated steps — the per-task proofs, the first dispatch, the review, the grade and the economy line — are shown as illustrative expected output: no run of the updated tutorial has been recorded yet, and every job id, branch, SHA, duration, token count and score changes from machine to machine and from run to run. Blocks labelled *historical* are the authentic output of that earlier run; blocks labelled *illustrative* are what the updated steps are expected to print.

## 1. Prepare the lab

Follow steps 1–4 of [the first tutorial](01-first-job.md), but use a separate copy such as `~/projects/hello-crew-spec`. Before every ACC command, export the lab preferences path:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
```

That path points at the preferences file created inside the copied project, so this tutorial never touches your normal ACC settings.

On Windows PowerShell, assign the same path before every ACC call. PowerShell has no `$CC` string to expand, so call the script directly:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew-spec/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
```

The same mapping applies before every ACC call of this tutorial; a different path is ignored.

The setup command prints a tier summary like this one from the lab:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

Treat those counts as illustrative: they change over time and from machine to machine, with the CLIs you have installed. What matters is that at least one free OpenCode executor shows up. If there is no free OpenCode executor, stop and configure one. Do not use a paid fallback for this tutorial.

## 2. Create the spec folder

From the copied Git project, create the numbered spec folder:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
proof = '''python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import normalize; assert normalize('hello crew') == 'hello crew'; assert normalize('  hello   crew  ') == 'hello crew'; assert normalize('') == ''; assert normalize('   ') == ''; print('passed')"'''
expect = "passed"
depends = []
interfaces = { normalize = "def normalize(text) -> str" }

[tasks.T002]
type = "code"
risk = "low"
accept = ["FR-002", "US1/AC2"]
files = ["src/hello_crew/text_tools.py"]
proof = '''python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import word_count; assert word_count('hello crew') == 2; assert word_count('  hello   crew  ') == 2; assert word_count('') == 0; assert word_count('   ') == 0; print('passed')"'''
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

Each proof checks its own task. T001 and T002 read the module in `src` and assert the behavior of the helper they own, so neither task needs a file that belongs to another task. T003 is the verification task: it runs pytest over the finished module, which is legitimate because its dependencies are accepted by then. Keep `expect = "passed"` in all three entries.

A task that proves itself must not need a file outside its own `files` list. If a proof can only pass after another task has written a file, that proof belongs to the wrong task: fix the proof, not the scope.

Keep the task text that `spec import` writes into each sidecar entry when you use a newer or older ACC version. That text lets ACC detect stale pieces later.

## 4. Create and import the job

Write the request, then create the job. The first line is ordinary shell work; the second command is ACC:

```bash
printf '%s\n' 'Implement the text-tools spec in specs/001-text-tools. For T001, add only src/hello_crew/text_tools.py with normalize(text): use split-and-join whitespace normalization. Run the proof from the generated brief.' > .crew-chief-request.md

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC job new --project . --title "Text tools" --request-file .crew-chief-request.md
```

The lab printed this job id:

```text
20261020-1452-text-tools
```

Your id will differ. Use it below as `JOB`.

Import the spec:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --out .crew-chief-brief.md
```

You should see `wrote .crew-chief-brief.md`. With the sidecar of step 3, the brief carries the acceptance criteria, constraints, interface, allowed file, and this proof:

```text
## Proof
Command: `python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import normalize; assert normalize('hello crew') == 'hello crew'; assert normalize('  hello   crew  ') == 'hello crew'; assert normalize('') == ''; assert normalize('   ') == ''; print('passed')"`
Expect: passed
```

The proof reads the module in `src` and checks the helper in place. T001 never needs the test file that T003 writes, so the task stays inside its own allowed files.

Pick a free OpenCode executor:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

The earlier lab run picked:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use the exact free OpenCode id printed by your run. Dispatch it with the rendered brief:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

A successful dispatch prints this shape (illustrative — your job id, branch, duration and token counts differ):

```text
status: ok
branch: crew/<job-id>-1-1
worktree: ../hello-crew-spec.crew/<job-id>-1-1
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 129.0s
steps: 21
tokens: 942551 (in 932015 / out 10536)
proof: passed
```

Expect no `scope:` line: the executor changed only `src/hello_crew/text_tools.py`, the file T001 allows. Nothing had to be added to make the proof pass, so the piece has no extra to count.

Read the recorded attempt before you accept it. Do not accept `proof_failed`; it counts as a failed attempt. Read it, fix the cause, pick another free OpenCode model, and dispatch again. Replace `opencode:opencode-go/beam-free` in the next command with the id that just failed:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude opencode:opencode-go/beam-free

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Never add a file outside the task's `files` list just to make a proof pass. A proof that needs a file from another task is a broken proof: fix the proof, not the scope.

### Historical: the earlier run under the old proof

An earlier run of this tutorial gave T001 and T002 a pytest proof that needed the test file T003 writes. In a new project that proof could not pass without touching a file outside the task, so the first attempt failed:

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

That run picked another free OpenCode model and dispatched again; the retry used `opencode:opencode-go/minimax-m3.1-flash-free` and printed:

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

There the `scope:` line matters: the proof passed, but the executor had also added the test file, which is outside T001's `files` list, so the reviewer called it an extra and that run graded T001 with 90. Keep the lesson, not the numbers: read the recorded attempt before retrying, and never add an out-of-scope file just to satisfy a proof. The per-task proofs of step 3 remove that trap — T001 proves `normalize` and T002 proves `word_count` from `src`, and only T003, whose dependencies are accepted by then, runs pytest.

## 6. Review with a rendered brief

Choose a free OpenCode reviewer from a different underlying model family, then render the review brief. If `pick` proposes any other tool, or any included, cheap, or paid model, exclude its exact id with `--exclude` and pick again. Apply this rule to reviews as well as to implementation: never dispatch Antigravity, Codex, Claude, an included, a cheap, or a paid executor in this tutorial.

Exclude the model that wrote the code, in both routes. Treat `opencode:opencode/MODEL` and `opencode:opencode-go/MODEL` as the same underlying model for an independent review: when the task was implemented by one of them, exclude both. ACC may treat the two ids as different quota families, so the exclusion has to name both, even though the model behind them is the same one. Replace `MODEL` in the command below with the model your implementation attempt actually used — the id your `pick` printed — and do it for every task you review. If more than one executor produced a successful attempt on that piece (a retry after a failure, for example), exclude the aliases of every one of them.

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude opencode:opencode/MODEL opencode:opencode-go/MODEL

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --review --out .crew-chief-review.md
```

`--exclude` takes a list of ids, so add every id you want out to the same flag and run `pick` again. A reviewer proposed by another tool is excluded without dispatch; nothing of it runs.

The rendered review brief carries the criteria, the allowed files, and the two verdicts. Append the rules the product cannot infer, so the reviewer states the HEAD it checked and does not hide a file it did not need:

```bash
cat >> .crew-chief-review.md <<'EOF'

Start your final answer with `HEAD confirmed: <actual short SHA>`. Report every changed file outside the allowed-files list as extra, even if that file is needed by the proof command. Do not infer that a passing proof permits additional files.

Global edge case from the spec: Empty or whitespace-only text should normalize to an empty string and count as zero words.
EOF
```

In PowerShell, the same append:

```powershell
@'

Start your final answer with `HEAD confirmed: <actual short SHA>`. Report every changed file outside the allowed-files list as extra, even if that file is needed by the proof command. Do not infer that a passing proof permits additional files.

Global edge case from the spec: Empty or whitespace-only text should normalize to an empty string and count as zero words.
'@ | Add-Content .crew-chief-review.md
```

Run the append after every `brief render`: rendering the brief again overwrites the file.

Dispatch the review against the worktree of the successful implementation attempt:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

In the earlier lab run the review came from the free `opencode:opencode/beam-free`, a different model family from the executor that wrote the code in that run. Use the exact free OpenCode id your own `pick` prints. If a free OpenCode reviewer returns `status: quota`, read the recorded attempt, exclude that id, and pick another free OpenCode reviewer; never fall back to another tool.

A successful review answers `status: ok` and these verdicts (illustrative — the SHA and the findings depend on the worktree):

```text
HEAD confirmed: <short SHA of the reviewed worktree>

## Spec verdict
- FR-001: done
- US1/AC1: done

`extra`: none

## Quality verdict
No Critical or Important findings.
```

Use the `worktree` from the successful implementation dispatch, not a new path. If the reviewer reports the wrong `HEAD` or omits the required HEAD confirmation, discard that review and dispatch it again against the correct worktree.

## 7. Accept tasks and run their proofs

Grade and accept T001 after reading the review. When the review reports no file outside the allowed list, there is no extra to count:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 2 --missing 0 --extra 0

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

Expect this output (illustrative — the score follows your review; with no extras it is 100):

```text
score: 100
adherence 2/2 → 100; 0 corrections; 0 extra; penalties none → 100
tasks.md has other changes; marked T001 but did not commit
accepted crew/<job-id>-1-1
```

Count an extra only when the review really reports one. Each extra costs 5 points, and a file outside the task's `files` list is an extra even when the proof needed it. The earlier lab run, whose executor added the test file, graded T001 with 90.

The accept command ticks T001 from `[ ]` to `[X]`. Because the spec files had other changes, it did not make a separate task-marking commit.

Repeat the same render, pick, dispatch, grade, and accept cycle for T002. In the earlier lab run, the free OpenCode executor was `opencode:opencode/glm-5.3-flash-free`; its proof passed and its grade was 100. Then dispatch T003, which is now unblocked. That run used `opencode:opencode/deepseek-v4.1-flash-free`; its proof passed and its grade was 100.

Check the completed spec:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC ruling add --project . --job JOB --text "Keep text helpers pure and stdlib-only — predictable whitespace behavior is enough for this small feature — adding validation now would expand the API."

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC decision add --project . --job JOB --from-ruling 1 --context "The feature needs two small helpers with no runtime dependency." --alternatives "Add a third-party text library; validate every input type." --consequences "Callers get a small predictable API; non-string validation remains outside this feature."
```

The lab printed `ruling 1 recorded` and created:

```text
docs/decisions/0001-keep-text-helpers-pure-and-stdlib-only.md
```

Close the feature:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC close --project . --job JOB --type feat
```

The command prints `changelog: CHANGELOG.md (Added)`, a feature commit message, the task/executor scores, and this economy line (illustrative — your numbers depend on your run):

```text
free work: 100% of pieces, 100% of recorded tokens · coordinator: not recorded · 1.0 attempts/piece · mean score 100
```

Read the line before you celebrate it:

- `attempts/piece` counts execution attempts only. Attempts that did no work — `quota` and `brief_refused` — are left out of the count, and review dispatches are not counted either. When each of the three pieces needed one execution attempt, the number is 1.0.
- Report the total dispatches separately when you describe retries. For the three pieces above that would be six dispatches: three executions and three reviews.
- coordinator: not recorded means no coordinator usage was recorded. The free percentage covers recorded tokens only; the actual coordination cost is unknown until you record it with usage add. An explicit zero-token entry differs from no entry: it shows coordinator: 0% when there are execution tokens.
- `mean score 100` is the mean of the three grades of step 7 when all three are 100. It says nothing about what the run cost.

`close` adds the job to `CHANGELOG.md` and prints the commit message. Commit the accepted work, the ADR, and the changelog entry as your project workflow requires.

## If you edit tasks.md later

A piece remembers the task text it was imported from. If you regenerate or edit `tasks.md`, the piece becomes stale when that text changes. Run `spec import` again after fixing the task and sidecar. Until then, `brief render` and `dispatch` refuse the stale piece. If spec-kit renumbers an id, also check the sidecar's `task` text; ACC warns when an entry such as T012 was written for a different task. Do not bypass this with `--ignore-deps`: that option is for dependency checks, not stale task text.

If you add a new task, add its sidecar table and proof before dispatching. If you change allowed files, acceptance references, or the proof, render a fresh brief and read the new `scope:` line before accepting.

## What happened in the earlier lab run

- Files written: `spec.md`, `plan.md`, `tasks.md`, `crew.toml`, the three task outputs, an ADR, and `CHANGELOG.md` in the lab.
- Commands run: `spec new`, `setup`, `discover`, `job new`, `spec import`, `spec status`, `brief render`, `pick`, `dispatch`, `grade`, `accept`, `ruling add`, `decision add`, and `close`.
- Product notes: a per-task proof checks the task's own file, so no out-of-scope file is needed to make it pass; `scope:` lists the files that are outside the task's allowed list; `accept` warns and does not make its own commit when `tasks.md` already has other changes; a quota response is recorded as a failed attempt and needs another free reviewer.
