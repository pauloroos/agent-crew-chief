---
name: crew-chief
description: Coordinate several executor CLIs on any non-trivial task in a project where this skill is installed - planning, coding, refactoring, tests, docs, research, diagnosis, data work and code review. Use it whenever the request is more than a one-line answer: split the work into pieces, dispatch each piece to a free executor first, have the result reviewed by a different model family, grade it and keep the scoreboard. Not for trivial edits or plain conversation.
---

# crew-chief

You (the CLI the user opened) are the strong coordinator: you think, split the
work, write the briefs and judge. The hands-on work starts with **free**
executors and reaches the `paid` tier only with `--strong`; review and
findings climb the same ladder. Other CLI agents ("executors")
do the typing; you dispatch, judge and report. Dispatch without asking
permission first: split the request, run the cycle, then show the result at
the end.

Work types come in two groups:

- **Execution** - `code`, `tests`, `refactor`, `docs`, `data`: free executors
  first. `pick` tries the tools in `exec_first` (default `["opencode"]`) first,
  then any other free executor; with no free one left it can still take an
  `included` executor or a budget-allowed `cheap` one, and reaches the `paid`
  tier only with `--strong`.
- **Thinking** - `review`, `design`, `diagnosis`, `research`: any free
  executor, ranked by score; when no free one is available, or after 2 free
  failures, `pick` climbs to `included`, then to `cheap` while the budget
  allows, and last to `paid`.

## The CLI

`CC` below stands for the installed CLI, using whichever path exists:

```bash
python -I .agents/skills/crew-chief/scripts/crew-chief.py
python -I .claude/skills/crew-chief/scripts/crew-chief.py
```

Every subcommand takes `--project <path>` (default: the current directory) and
`--json`. Prefer `--json` when you need to parse the answer. Exit codes: `0`
success, `1` nothing to do (no executor, an attempt that is not `ok`, nothing to
accept), `2` a configuration error (one line on stderr).

### Interview

`CC interview --request "..."` creates an interview-mode job and lists the
pending questions (`project`, `request`, `coordinator`, `executors`,
`end_mode`); `CC interview --job ID --answer KEY=VALUE` answers one, and
`CC interview --job ID` shows the state. Once every question is answered the
job's coordinator is launched like `CC launch` (see the maestro below).

Start with `CC discover --project .`: it lists the executors available on this
machine as `<tool>:<model>  cost=<free|paid>  trains=<yes|no>`, plus a line for
every tool it skipped and why. It ends with one `work types: <tool>: ...` line
per tool: the work types `pick` may give it (`all types` unless the tool sets
`work_types`), and `first on execution work` for the `exec_first` tools.

After the list, `discover` prints what changed in the models since the last
check (`docs/crew-chief/models-seen.json`, committed with the jobs):
`new free: ...`, `new paid: ...`, `gone: ...`, `now paid: ...`/`now free: ...`,
or `models: no changes since <when>`; the first run only prints
`models: baseline: N executors recorded`. `CC job new` runs the same check on
stderr every time, and `pick`/`dispatch` run it on stderr when the last check
is older than `model_check_hours` (24 by default). Whenever one of these lines
shows up, **tell the user** which free models are new and which ones are gone
(paid changes matter less; mention them briefly). A new model enters with no
score: do not hand-pick or avoid it, `pick` gives it its chance on low-risk
work on its own. A tool that was skipped never makes its models "gone", and a
`models: check skipped` line never blocks the job.

## The job cycle

Seven steps, in order. For a project-size request that has a spec, follow
**Spec-driven work** below, which uses the same steps.

1. **Create the job.** Write the user's request verbatim to a UTF-8 file, then
   register it:

   ```bash
   CC job new --project . --title "Add CSV export" --request-file .crew-chief-request.md
   ```

   It prints the job id (use it everywhere below) on stdout and the models
   check on stderr: relay any new or gone free models to the user (see
   **The CLI**). If the work stays with you
   because it must not be distributed, add `--not-distributed --reason "<why>"`.
   Add `--issue <ref>` when the request came from an issue tracker.

2. **Split it into pieces.** One piece per unit you can dispatch, review and
   grade on its own:

   ```bash
   CC piece add --project . --job <id> --title "Write the exporter" --type code --risk low
   ```

   `--type` is one of `code, tests, refactor, design, docs, research, diagnosis,
   data, review`; `--risk` is `low, medium, high`. Prefer more, smaller pieces
   over one large one: a smaller brief is a cheaper failure.

3. **Pick an executor.**

   ```bash
   CC pick --project . --job <id> --piece 1
   ```

   It prints `<tool>:<model>` and the one-line reason. On execution work
   (`code`, `tests`, `refactor`, `docs`, `data`) the free executors are the
   candidates first, the `exec_first` tools (OpenCode by default) ahead of the
   other free ones; when none is left, `included` and budget-allowed `cheap`
   executors come next and the `paid` tier needs `--strong`. On thinking work
   (`review`, `design`, `diagnosis`, `research`) every free executor competes
   on score and the ladder climbs to `included`, `cheap` and then `paid`.
   Within that pool executors are ranked by their decayed score on this work
   type. A tool whose `work_types` leaves the piece's type out is never
   picked; the reason then ends with `not for <type>: <tools>`. `pick` already
   excludes executors that failed this piece; add `--exclude <id> ...` to leave
   more out. When the reason mentions a "second chance", a low-risk piece went
   to an unrated or below-median model on purpose - that is the point.
   Executors tied on score are spread over the job (the least used one first,
   then a different tool), so pick every piece with `pick` rather than reusing
   one id. An executor whose attempt ended in `quota` in the last
   `quota_cooldown_min` minutes (default 60), in any job, sits out together
   with its whole quota family; the reason then ends with
   `skipped for quota: <ids>`. The family drops a trailing effort suffix
   (`-minimal`, `-low`, `-medium`, `-high`, `-xhigh`, `-max`) from the model;
   on Antigravity, whose quota is shared by vendor, it is
   `antigravity:<model up to the first '-'>` (so `claude-opus-5-5-high` in
   quota also benches `claude-opus-5-5-medium` and `claude-sonnet-5`, but not
   `gemini-*`); on any other tool it is `<tool>:<model>`.

4. **Write the brief and dispatch.**

   ```bash
   CC dispatch --project . --job <id> --piece 1 --executor <tool>:<model> --brief-file .crew-chief-brief.md
   ```

   The brief is the whole contract: what to change, where, what "done" means,
   which files may be touched and how to test it. The executor runs in a
   throwaway git worktree and never touches your branch. `dispatch` prints
   `status`, `branch`, `worktree`, `base` (where the worktree started: `HEAD`
   or an earlier attempt, see step 6), `destructive_suspect`, `outside_writes`,
   `duration`, `steps` (agent turns, `-` when the tool does not say) and
   `tokens` (`~` and "estimated" when the tool reported none and the count is
   characters / 4), and exits `0` only on `status: ok`. Any other status
   (`no_change`, `failed`, `timeout`, `quota`, `outside_write`,
   `brief_refused`, and for spec pieces `proof_failed`) is recorded in the job
   with the executor's output as a note - read it before you retry. `no_change`
   means the executor exited cleanly but left the worktree exactly as it found
   it (the brief and output files do not count); it is a failure like the others. A review never
   changes files, so it stays `ok`. Give long work a
   smaller `--timeout <seconds>` only when you know the piece is short; the
   default comes from `dispatch_timeout_s` (900 s). Pieces that do not depend
   on each other can be dispatched in parallel (separate processes): every
   command updates the job record under a lock, and parallel attempts get
   their own worktrees.

5. **Review with a different model family.** Choose the reviewer first
   (unless `review_default` names one), then dispatch the review:

   ```bash
   CC pick --project . --job <id> --piece 1 --review
   CC dispatch --project . --job <id> --piece 1 --executor <other-tool>:<model> --brief-file .crew-chief-review.md --review --target <worktree>
   ```

   `pick --review` treats the choice as thinking work (type `review`) and
   leaves out the quota family of every executor with an `ok` attempt on the
   piece; the reason ends with `reviewer outside the family of: <ids>`.

   `--target` is the worktree printed by step 4. The reviewer runs read-only
   there, commits nothing and leaves no brief file behind; `dispatch` prints its
   answer after an `answer:` line. Ask the reviewer to
   check the request item by item, not to rewrite the code. See **Review**.

6. **Grade.** Record every correction, then score the piece:

   ```bash
   CC correction --project . --job <id> --piece 1 --reason "missed the empty case" --executor <tool>:<model>
   CC grade --project . --job <id> --piece 1 --executor <tool>:<model> --corrections 1 --done 4 --missing 0 --extra 0
   ```

   `--penalty` is repeatable and takes `destructive`, `outside_write`,
   `false_done` or `model_mismatch`. `grade` recomputes
   `docs/crew-chief/scoreboard.md`. See **Grading** for the inputs.

   **A correction builds on the earlier work.** When the review or your own
   reading asks for an adjustment, record the `correction` against the
   executor that did the work, pick again and dispatch with a brief that
   describes **only the adjustment** ("add `justify-center` to the card"), not
   the whole piece again. Once a correction is recorded after the piece's last
   `ok` exec attempt, the next `dispatch` starts its worktree from that
   attempt's branch instead of `HEAD`, so the new executor finds the earlier
   work in place; `dispatch` prints `base: crew/<job>-<piece>-<n> (attempt
   <n>)` and the attempt records it. Its diffstat, `no_change` and
   `destructive_suspect` are measured against that earlier work, so a one-line
   fix shows as one line. Pass `--fresh` to start from `HEAD` anyway (the
   earlier work is wrong in substance and must be redone), or
   `--base <attempt-number|branch>` to start from a specific attempt of the
   piece.

   **Grade every executor that worked on the piece.** Each `grade --executor
   <id>` keeps its own grade on the piece: grade the first executor with the
   correction it drew and the one that finished with its own result. Grading
   the same executor again replaces only that executor's grade. The scoreboard
   counts every one of them.

7. **Land it.**

   ```bash
   CC accept --project . --job <id> --piece 1
   CC discard --project . --job <id> --piece 1
   CC close --project . --job <id>
   ```

   `accept` merges the piece's last `ok` attempt into the current branch (after
   a correction it already carries the earlier work); `discard` drops it. On a
   spec piece `accept` also ticks the task `[X]` in `tasks.md` (see
   **Spec-driven work**).
   `close` prints the commit message for the delivery and the issue comment;
   both credit the executor of the last `ok` attempt first and list every
   other graded executor with its score. When a piece was accepted and the work is
   a feat, fix, refactor or perf, `close` also adds the job to the project's
   `CHANGELOG.md` under `## [Unreleased]` (once per job; `--no-changelog` or
   `changelog = "none"` skips it) and prints `changelog: <path> (<category>)`
   first, so stage that file with the commit. The type comes from `close --type
   feat|fix|refactor|perf|docs|chore`, else from a conventional prefix of the
   job title (`fix: ...`), else from the first piece's work type: pass `--type
   fix` when the job fixed a bug. Commit with the printed message and show the user the
   result plus the `close` output.

`CC scoreboard --project .` regenerates and prints the scoreboard when the user
asks how the executors are doing.

## Maestro: one coordinator per front

When the user hands you a goal too big for one job, or asks for the maestro,
you do not coordinate pieces yourself: you run coordinators. Each coordinator
works in its own worktree and branch, runs the job cycle above with its own
executors and reviewers, grades every piece and ends in one PR. You plan the
fronts, launch them, keep them unblocked and report. You never edit the
project's code yourself.

1. **Split into fronts.** A front is one independent outcome that fits one PR
   and touches files no other front touches. Work that depends on another
   front waits for that front's PR to merge; never run two fronts on the same
   files.
2. **One approval for the plan.** Show the user the fronts (goal, files or
   areas, coordinators) and ask once. Write each front's request to its own
   file; its name becomes the front's name.
3. **Launch one coordinator per front** from the project root:

   ```bash
   CC launch --request-file fronts/01-auth.md
   CC launch --request-file fronts/02-export.md --spec-dir specs/002-export
   CC launch --request-file fronts/03-docs.md --coordinator opencode:omni/chefe --coordinator claude:opus
   ```

   The coordinators come from `[team].coordinators` in the preferences, in
   order, as `tool:model` entries of any configured tool (claude, codex,
   opencode, antigravity, a `generic` tool...); repeated `--coordinator`
   flags replace the list for one front. When a coordinator hits a quota or
   fails, the next one of the list takes over the same worktree and job on
   its own. Keep every printed coordinator ID next to its front.
4. **Watch with `CC status`** (`--json` to parse), every few minutes, never in
   a tight loop. Each coordinator shows its state, current `tool:model`,
   rounds and switches, elapsed time, its own tokens and its job's economy
   (executor tokens by tier, attempts per piece, mean score). Act on `state`:
   - `question`: the coordinator wrote a question; answer it with
     `CC resume ID --answer "..."` when the goal already decides it,
     otherwise ask the user and relay the answer;
   - `quota`: every coordinator of the list hit a limit. `CC resume ID` once
     a limit has reset (it starts again from the top of the list);
   - `failed`: read `last_error` and the round logs in
     `<project>.crew/coordinators/<ID>/`; `CC resume ID` only after the cause
     is fixed, never as a blind retry;
   - `failed` after reviews: the review still fails after three tries; read
     the last `review-N.md` and decide with the user;
   - `completed`: the review passed; note the PR and its review score.
   Once a front's job is closed, the same `tool:model` reviews it in a clean
   session and a checkout of its own: a checklist of each requested item
   (delivered, partial or missing), the test results and the problems, ending
   in a verdict and a score. The review goes to `review-N.md` in the
   coordinator's folder and as a comment on the PR. A failed review sends the
   coordinator back to fix it on the same PR; a passed one completes the front
   and removes its worktrees and attempt branches (its folder, numbers and PR
   branch stay). `CC stop ID` kills a coordinator; its worktree and branch
   (`maestro/<front>-<id>`) stay. `CC clean` removes what completed fronts
   left behind; `CC clean ID` removes a stopped or failed front's worktrees
   once the user gives it up.
5. **Report** every front with its PR (or branch), its rounds, time, tokens,
   accepted pieces, the review's verdict and score, and what is left. The owner merges; you never
   merge, push to the base branch or tag. When the owner lets you merge, merge
   a front's PR only once `status` shows it `completed`: the runner pushes the
   coordinator's tokens to the branch right before, and an earlier merge
   leaves them out of the job record.

### When you are the launched coordinator

You run headless: nobody can answer a prompt. Follow the job cycle in the
worktree you were started in, record the job with `job new`, close it (which
pushes the branch and opens the PR) and stop. `close` writes the CHANGELOG
entry and the scoreboard: commit those files on the job branch and push it, so
the PR carries them. Put the output of the checks you ran (build, lint, tests)
in the PR description: a reviewer checks the delivery against the request
afterwards and fails it when they are missing. Reviewers on claude run without Bash and cannot read the
diff themselves: put the diff in the review brief (or paste it there). If you
need the owner's decision, write the question to the `question.md` path your
prompt names and stop; you are resumed with the answer. Every round you run is
timed and its tokens are added to the job's `usage` for you, so do not
`usage add` them.

## Spec-driven work

Size the process to the request before you create anything:

- **trivial** (a one-line answer, one small edit): no job, no spec. Do it.
- **small** (a few pieces, and the request fits in a paragraph): only a job,
  the seven steps above. Pieces without a spec task need no proof.
- **project** (a feature with several requirements the user will want to trace
  back to the code): a spec folder and the full cycle below.

A project job keeps its spec in a spec-kit style folder (`specs/NNN-slug/` with
`spec.md`, `plan.md`, `tasks.md`). crew-chief reads that format; it never
creates `.specify/` and never edits `spec.md` or `plan.md`. Extra facts the
executor needs live in a sidecar, `crew.toml`, next to `tasks.md`. If the
superpowers skills are installed, use them to discuss and design the spec with
the user; crew-chief starts when the plan exists.

The cycle:

1. **Spec folder.** Use the one the user already has (a spec-kit folder, or the
   one `.specify/feature.json` points to), or create one:

   ```bash
   CC spec new --project . --title "CSV export"
   ```

   It prints the new folder (`specs/001-csv-export`), with short templates to
   fill in. Write the requirements as `FR-001`, the user stories as `User Story
   1` with `Given/When/Then` scenarios, and the tasks as one line each in
   `tasks.md`: `- [ ] T001 [P] [US1] Create the exporter in src/exporter.py`.
   `[P]` marks a task that can run in parallel; write `depends on T001` in the
   text for a dependency.

2. **Job and import.**

   ```bash
   CC job new --project . --title "CSV export" --request-file .crew-chief-request.md
   CC spec import --project . --job <id> --spec-dir specs/001-csv-export
   ```

   Every open task becomes a piece (`ref` = the task id) with a type and a risk
   from `crew.toml`, or guessed from the task text. `--spec-dir` is only needed
   the first time. Read what `import` prints and fix it before you dispatch:
   `missing proof` (hands-on tasks without a proof), `orphan sidecar entries` and
   `orphan pieces` (a task id that is gone from `tasks.md`), `parallel conflict`
   (two `[P]` tasks of one phase that share `files`) and `stale pieces` (the text
   of a task changed since it was imported, for example after `tasks.md` was
   regenerated; a stale piece is refused by `dispatch` and `brief render`, so
   fix the spec and run `import` again) and `crew.toml entry T012 was written
   for another task; review it` (spec-kit renumbered the tasks and that id now
   names another task: the entry is not reused, and `dispatch` and `brief
   render` refuse the task until you fix the entry's fields and set its `task` to
   the current text). A job stays bound to its first spec folder; another
   `--spec-dir` is an error, so use a new job. Then fill in `crew.toml`, one
   table per task (`import` adds the tables and keeps your comments; an unknown
   key is an error):

   ```toml
   global_constraints = ["Python 3.11, stdlib only"]

   [tasks.T001]
   task = "Create the exporter in src/exporter.py"   # written by import; keep it
   type = "code"
   risk = "low"
   accept = ["FR-001", "US1/AC1"]        # references into spec.md
   files = ["src/exporter.py", "tests/test_exporter.py"]
   proof = "python -m pytest tests/test_exporter.py -q"
   expect = "passed"                      # optional: text the output must contain
   depends = []
   interfaces = { export_csv = "def export_csv(rows, path) -> int" }
   ```

   `CC spec status --project . --job <id>` shows the progress per phase, which
   tasks are ready and which are blocked, and by what.

3. **Brief and dispatch.** Render the brief from the task instead of writing it:

   ```bash
   CC brief render --project . --job <id> --piece 3 --out .crew-chief-brief.md
   CC pick --project . --job <id> --piece 3
   CC dispatch --project . --job <id> --piece 3 --executor <tool>:<model> --brief-file .crew-chief-brief.md
   ```

   The brief carries only the task, the acceptance criteria it points at (with
   their text), the global constraints, the interfaces, the files it may change
   and the proof. When the executor finishes `ok`, `dispatch` runs the proof in
   its worktree; a failing proof, or one that times out, makes the attempt
   `proof_failed` with the output in the note, and it counts as a failure. A
   hands-on spec piece (`code`, `tests`, `refactor`, `docs`, `data`) with no
   proof is refused unless you pass `--no-proof` (recorded as skipped). A piece
   whose dependency is not accepted yet, or whose earlier phase is not finished,
   is refused unless you pass `--ignore-deps` (recorded). A `scope:` line in the
   output lists files the attempt touched outside the task's `files`: read it
   before you accept. Pieces without a `ref` follow the plain job cycle and need
   no proof.

4. **Review.** Same as step 5 of the job cycle, with the brief rendered for you:

   ```bash
   CC pick --project . --job <id> --piece 3 --review
   CC brief render --project . --job <id> --piece 3 --review --out .crew-chief-review.md
   CC dispatch --project . --job <id> --piece 3 --executor <other-tool>:<model> --brief-file .crew-chief-review.md --review --target <worktree>
   ```

   The review brief is pinned to the piece's last `ok` exec attempt (or
   `--attempt N`): it names the worktree, the start and end commits, asks the
   reviewer to confirm `git rev-parse --short HEAD` and to review `git diff
   <start>..HEAD`, and repeats the global constraints. Pass that same worktree
   as `--target`. **Discard a review whose HEAD does not match** (or that
   answers `WRONG HEAD`) and dispatch it again. The reviewer gives two separate
   verdicts: **spec** (each acceptance criterion `done`, `missing` or
   `misunderstood`, plus anything `extra`) and **quality** (findings as
   `Critical`, `Important` or `Minor`). `dispatch --review` prints the answer
   after an `answer:` line (`answer` in `--json`). Read the diff yourself.

5. **Correct and grade.** `CC correction` and `CC grade` as in step 6; a
   correction brief describes only the adjustment.

6. **Accept.** `CC accept` merges the piece and marks its task `[X]` in
   `tasks.md`; only that one line of the file changes, and its line endings
   stay. When `tasks.md` is tracked and has no other changes the tick is its own
   commit (`docs(tasks): mark T003 done`); otherwise it is left uncommitted with
   a warning, so commit it with your other spec edits. A stale piece is merged
   but its task is not ticked.

7. **Converge.** When the tasks are done, one last review compares the code
   with the whole spec. Add a `review` piece, render the converge brief and run
   it against the project folder:

   ```bash
   CC piece add --project . --job <id> --title "Converge spec" --type review --risk low
   CC brief render --project . --job <id> --converge --out .crew-chief-converge.md
   CC dispatch --project . --job <id> --piece <n> --executor <other-tool>:<model> --brief-file .crew-chief-converge.md --review --target .
   ```

   The reviewer lists the gaps that are not tasks yet (`- [ ] T### <text> per
   <ref> (<gap-type>)`) or answers `Converged`. Add the gaps to `tasks.md`, run
   `spec import` again and repeat from step 3.

8. **Rulings and decisions.** Whenever you settle a question that an executor,
   a reviewer or the user could later ask about, record it in one line, then
   promote the ones worth keeping to an ADR:

   ```bash
   CC ruling add --project . --job <id> --text "<decision> — <why> — <cost if wrong>"
   CC decision add --project . --job <id> --from-ruling 1 --context "..." --alternatives "..." --consequences "..."
   CC decision add --project . --job <id> --title "Use TOML for the sidecar"
   ```

   `decision add` writes `docs/decisions/NNNN-slug.md`; commit it with the work.

9. **Close.** `CC close` as in step 7 of the job cycle: it adds the job to the
   project's CHANGELOG, prints the economy block (below) and the commit message.

### Your own spend and the economy alerts

The point of delegating is to spend fewer paid tokens, so the job keeps score of
yours too. **When your CLI reports your own token usage, record it with `usage
add`** (`--role coordinator`; add `--estimated` when the counts are guesses).
A coordinator started by `CC launch` skips this: its rounds are booked for it.

```bash
CC usage add --project . --job <id> --role coordinator --tokens-in 52000 --tokens-out 9000 --note "planning and review"
CC economy --project . --job <id>
```

`economy` (and `close`, before the commit message) prints one summary line:
the share of pieces and tokens that went to free executors, the coordinator's
share of all tokens, attempts per piece and the mean score, plus one
`alert: ...` line for each limit crossed: `alert_coordinator_share` (default
0.5), `alert_attempts_per_piece` (2.5) and `alert_job_minutes` (180); a `0`
turns an alert off. **Read the economy alerts.** If the coordinator is spending
more than it delegates, split less and do the work directly; if there are too
many attempts per piece, the briefs are too big or the pieces too vague: split
the piece or write the proof first. Tell the user when an alert fired.

### Cost tiers and `CC setup`

Every executor sits on a four-step ladder, cheapest first, with `forbidden`
outside it:

| Tier | What it means | How `pick` uses it |
| --- | --- | --- |
| `free` | No money, no subscription (free previews, contributor models, Antigravity quota) | The first choice for every piece |
| `included` | Covered by a subscription you already pay for (a Codex or Claude plan) | Next, once no free executor is left for the piece |
| `cheap` | Pay per use (OpenRouter, OpenCode's paid models) | Only while the day's spend is under `cheap_budget_usd_day` |
| `paid` | Pay per use, the strong fallback | Thinking work only, or any work with `--strong` |

`pick` climbs one step only when a tier runs out (nobody free is left for the
piece), never because a free executor merely failed. On execution work it never
reaches `paid` without `--strong`; with `--strong` it still tries `included` and
`cheap` before `paid`. Executors start at the **lowest effort** that suits the
piece (a `-low` variant before `-medium`), and a `quota` or `brief_refused`
attempt does not raise that ceiling. `--coordinator <tool>` (default
`$CREW_CHIEF_COORDINATOR`) names the CLI you are running in, so `pick` uses its
own CLI — or its subscription family — **last** among the `included` executors;
`--allow-own-cli` drops that rule.

When `discover` prints `run setup to configure cost tiers`, the global
preferences file has no `acc setup` block yet. **For beginners: run
`CC setup --yes`.** Otherwise run it once with the user present, or accept the
defaults:

```bash
CC setup --project .          # asks one question at a time
CC setup --yes --project .    # asks nothing: subscriptions no, budget 0, no training
```

It detects the CLIs on PATH, their models and the kind of each login (account
or API key — never the key itself), asks only what it cannot deduce (whether
the Claude subscription is paid for, the daily budget for cheap models, and
whether free models that train on what the user sends may be used), prints the
count per tier and writes one managed block between `# >>> acc setup >>>` and
`# <<< acc setup <<<` in `~/.config/agent-crew-chief/preferences.toml`,
leaving everything outside it alone. It never stores a key value — only the
kind of login and the names of environment variables. It needs a terminal:
without one and without `--yes` it exits `2` and writes nothing. `--lang pt|en`
picks the language of the questions (default: the system language).

A session looks like this:

```text
Do you pay for a Claude subscription (Pro/Max)? [y/N]
Are you willing to spend money on cheap pay-per-use models when the free ones run out? How much per day, in dollars? [0] 2
May we use free models that train on what you send? [y/N]
your tiers: free 6 · included 1 · cheap 3 (budget US$ 2) · paid 2
```

**The spend log.** Every `cheap` or `paid` attempt writes one JSON line to
`spend.jsonl`, next to the global preferences file, whichever project it ran
in; `pick` and `dispatch` read it to check the budget. `cheap_budget_usd_day`
is the global daily ceiling (`0` keeps the cheap tier closed; raise it in
`setup` or in the preferences file), `cheap_budget_usd_job` caps one job, and a
`[[models]]` rule may carry `cap_usd_day` to cap one executor. Two honest
limits: **one attempt can go over the daily budget by at most its own cost**
(the check happens before the run, and the cost is only known afterwards), and
several attempts running at once can each pass the check and together exceed
the budget. There is no per-attempt token limit because no supported CLI
accepts one. A cheap model whose cost is unknown and which has no `cap_usd_day`
gets **one probe attempt per executor per day** (only while the budget is above
`0` and has room); after that it stays closed until a cap is set.

**Sensitive paths.** The project's `sensitive_paths` name files and folders an
executor must never see. Each entry is a path prefix or an fnmatch wildcard
(`.env`, `secrets/*`): a prefix like `.env` also hides `.env.example`. `pick`
and `dispatch` refuse a brief that names one, and `dispatch` hides the tracked
matches from the exec worktree and checks the worktree again after the run — a
path found afterwards is noted on the attempt.

## What stays with the coordinator

- The plan: the split into pieces, their order and their risk.
- The briefs: the only spec an executor ever sees.
- The review: which model reviews, and the final judgment.
- `accept` / `discard`: nothing lands in the project's branch without you.
- The answer: the user talks to you, never to an executor.

Never hand a whole request to one executor when it has parts you can grade
separately. Never let an executor's own report of success decide the outcome -
read the diff.

## Escalation

Break the task down before you escalate; splitting is almost always cheaper than
a stronger model.

**Thinking work** (`review`, `design`, `diagnosis`, `research`) escalates on its
own. After **2 free failures** on the same piece - a non-`ok` attempt by a free
executor, or a correction against one - or when no free executor is left,
`pick` climbs the ladder: `included`, then `cheap` while the budget allows, and
last the `paid` tier in `strong_order`. The escalation is recorded in the job,
so do not force it by hand.

**Execution work** (`code`, `tests`, `refactor`, `docs`, `data`) never reaches
`paid` on its own. After free failures `pick` keeps offering the free executors
not yet tried on the piece; when none is left it climbs to `included` and then
to `cheap` while the budget allows, and prints
`no executor available: no free executor left for <type>; split the piece ...`
and exits `1` when every allowed tier is unavailable (`paid` may exist but is
gated behind `--strong`). Split the piece into smaller ones first; if it truly
cannot be split, do it yourself.

`--strong` is the rare exception: complex work that cannot be split. It leaves
every free executor out and is the only way execution work reaches the `paid`
tier. Use it deliberately and say so in the report.

If `pick` prints `no executor available` and exits `1`, no executor satisfied
the rules. Follow the reason (split, do it yourself) or report it to the user
instead of retrying blindly.

## Review

Every piece that changes files gets a review by a **different model family**
than the executor - the same family tends to bless its own habits. Use the
`review_default` preference as the executor id when it is set; otherwise run
`CC pick --job <id> --piece <n> --review`, which leaves out the executor's
quota family. Review is thinking work: any free model may do it; when no free
one is available `pick` climbs to `included`, then to `cheap` while the budget
allows, and the `paid` tier comes last - or you pass `--strong`. The reviewer
must not share a quota family with the executor that wrote the code.

The reviewer sees only the review brief and the target worktree. The coordinator
always judges: a review is input, not a verdict. Read the diff, the attempt note
and the reviewer's answer, then grade.

## Grading

The script computes the score from inputs you supply after reading the diff:

- **corrections** - how many times the work needed correcting (by you, the user
  or the review). Count every fix that was needed, not only the expensive ones.
- **done / missing / extra** - walk the request item by item and count each item
  as done, missing, or extra (delivered but not asked for).
- **penalties** - `destructive` (40) when the diff deletes or rewrites more than
  the piece asked for, `outside_write` (60) when the attempt wrote outside its
  worktree, `false_done` (40) when it claims part of the request is done and it
  is not, `model_mismatch` (30) when the tool answered with a different model
  than the one dispatched.

The score is `100 * done/(done+missing) - 15*corrections - 5*extra - the
penalties`, and the result is clamped to the 0-100 range. A low score is
information, not a death sentence: `pick` weighs it down, the scoreboard keeps
it, and an unrated or below-median executor still gets a second chance on
low-risk work (`explore_rate`, default `0.1`). No model is ever dropped for
good.

The scoreboard also shows, per executor and work type, how much work a pass
costs: `attempts/pass` (exec attempts per piece it got `ok` on), mean `steps`,
mean `tokens` (`~` when any count was estimated) and mean duration. They do not
change the score; use them to explain a choice and when you report back.

## Executor choice and subagents workflow

When an interactive coordinator starts a new job without `--executors` and
`executors_default` resolves to `"ask"`, it asks the owner exactly one question
before any dispatch: "Who should do the work: my own CLI (uses your subscription), my subagents in this session (uses your subscription), or free models?".
The answer is recorded via `job set --executors <own|subagents|free_first>`
and never asked again for that job. In non-interactive runs the question is
suppressed and the coordinator falls back to `free_first`, recording that it did.

In `subagents` mode, every piece done by a subagent runs in its own isolated
worktree created by ACC:

1. Call `piece start --native --job <id> --piece <n>` before launching a
   subagent. This creates the worktree and branch, records a running attempt
   with executor `native:<cli>/<model>`, and prints the worktree path.
2. Give the subagent **only** that worktree path. Never let a subagent edit the
   main checkout or the job branch directly.
3. When the subagent finishes, call `piece finish --job <id> --piece <n>`.
   This records the diff, runs the piece's proof and sets the attempt status.
   It refuses work committed outside that worktree/branch.

A correction works as with `dispatch`: once a correction is recorded, the next
`piece start --native` starts from the last ok attempt's branch, so the earlier
work is there; `--base <n|branch>` and `--fresh` choose the start explicitly.

Reviews are never native. `pick --review` answers an executor (never the
executor's model) and the review runs with `dispatch --review`, as in every
other mode.

The subagent's work lands only by ACC merging its branch into the job's branch
after review, grade and accept (same `accept` as every other piece). A job
delivers exactly one pull request from the job branch to main at the end.

In `own` and `free_first` modes, native subagents are forbidden for code, tests
and reviews.

## Safety

- **Never put secrets or real client data in a brief.** The brief travels into a
  worktree and into the executor's context. `dispatch` refuses a brief that looks
  like a secret (key/token patterns) or points at a `sensitive_paths` entry; the
  refusal is recorded as `brief_refused`. Paraphrase instead of pasting.
- **Check `destructive_suspect` and `outside_writes` before you accept.** An
  attempt that wrote outside its worktree, or whose diff looks destructive, is
  never clean: inspect the worktree the dispatch left behind, then fix,
  re-dispatch or discard it.
- **Pass the project's domain skills into the brief.** When the project has
  skills under `.agents/skills/` or `.claude/skills/`, name the relevant ones in
  the brief and quote the constraints that matter (style, test command, API
  rules). The executor does not read them unless you say so.
- Never delete or edit a scoreboard row or a job record to hide a failure.

## Reporting back

- Reply in the user's language.
- Show the result: what changed, the score, what the review flagged, and the
  `close` output (commit message and issue comment).
- Report what each attempt cost, per piece and executor: steps, tokens (say
  when they are estimated) and time, from the `dispatch` output or the job
  record.
- Include the economy line and any alert `close` printed (see **Spec-driven
  work**), and say what you did about it.
- Say plainly what was not done and what you discarded.
