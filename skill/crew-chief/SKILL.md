---
name: crew-chief
description: Coordinate several executor CLIs on any non-trivial task in a project where this skill is installed - planning, coding, refactoring, tests, docs, research, diagnosis, data work and code review. Use it whenever the request is more than a one-line answer: split the work into pieces, dispatch each piece to a free executor first, have the result reviewed by a different model family, grade it and keep the scoreboard. Not for trivial edits or plain conversation.
---

# crew-chief

You (the CLI the user opened) are the strong coordinator: you think, split the
work, write the briefs and judge. The hands-on work goes to **free** executors;
review and findings may go to any model, free ones included, and reach a more
capable (paid) model only when that is needed. Other CLI agents ("executors")
do the typing; you dispatch, judge and report. Dispatch without asking
permission first: split the request, run the cycle, then show the result at
the end.

Work types come in two groups:

- **Execution** - `code`, `tests`, `refactor`, `docs`, `data`: free executors
  only. `pick` tries the tools in `exec_first` (default `["opencode"]`) first,
  then any other free executor, and never escalates to a paid one on its own.
- **Thinking** - `review`, `design`, `diagnosis`, `research`: any free
  executor (OpenCode's cheap models too), ranked by score; when no free one is
  available, or after 2 free failures, `pick` escalates to a paid one.

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

Seven steps, in order.

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
   (`code`, `tests`, `refactor`, `docs`, `data`) only free executors are
   candidates, the `exec_first` tools (OpenCode by default) ahead of the other
   free ones; on thinking work (`review`, `design`, `diagnosis`, `research`)
   every free executor competes on score and paid ones are the fallback.
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
   `status`, `branch`, `worktree`, `destructive_suspect`, `outside_writes`,
   `duration`, `steps` (agent turns, `-` when the tool does not say) and
   `tokens` (`~` and "estimated" when the tool reported none and the count is
   characters / 4), and exits `0` only on `status: ok`. Any other status
   (`no_change`, `failed`, `timeout`, `quota`, `outside_write`,
   `brief_refused`) is recorded in the job with the executor's output as a
   note - read it before you retry. `no_change` means the executor exited
   cleanly but left the worktree exactly as it found it (the brief and output
   files do not count); it is a failure like the others. A review never
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
   there, commits nothing and leaves no brief file behind. Ask the reviewer to
   check the request item by item, not to rewrite the code. See **Review**.

6. **Grade.** Record every correction, then score the piece:

   ```bash
   CC correction --project . --job <id> --piece 1 --reason "missed the empty case" --executor <tool>:<model>
   CC grade --project . --job <id> --piece 1 --executor <tool>:<model> --corrections 1 --done 4 --missing 0 --extra 0
   ```

   `--penalty` is repeatable and takes `destructive`, `outside_write`,
   `false_done` or `model_mismatch`. `grade` recomputes
   `docs/crew-chief/scoreboard.md`. See **Grading** for the inputs.

7. **Land it.**

   ```bash
   CC accept --project . --job <id> --piece 1
   CC discard --project . --job <id> --piece 1
   CC close --project . --job <id>
   ```

   `accept` merges the piece's last `ok` attempt into the current branch;
   `discard` drops it. `close` prints the commit message for the delivery and
   the issue comment. Commit with the printed message and show the user the
   result plus the `close` output.

`CC scoreboard --project .` regenerates and prints the scoreboard when the user
asks how the executors are doing.

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
`pick` takes the first paid tool in `strong_order`. The escalation is recorded
in the job, so do not force it by hand.

**Execution work** (`code`, `tests`, `refactor`, `docs`, `data`) never escalates
on its own. After free failures `pick` keeps offering the free executors not
yet tried on the piece; when none is left it prints
`no executor available: no free executor left for <type>; split the piece ...`
and exits `1`. Split the piece into smaller ones first; if it truly cannot be
split, do it yourself.

`--strong` is the rare exception: complex work that cannot be split. It leaves
every free executor out and is the only way execution work reaches a paid
executor. Use it deliberately and say so in the report.

If `pick` prints `no executor available` and exits `1`, no executor satisfied
the rules. Follow the reason (split, do it yourself) or report it to the user
instead of retrying blindly.

## Review

Every piece that changes files gets a review by a **different model family**
than the executor - the same family tends to bless its own habits. Use the
`review_default` preference as the executor id when it is set; otherwise run
`CC pick --job <id> --piece <n> --review`, which leaves out the executor's
quota family. Review is thinking work: any free model may do it, OpenCode's
cheap ones included, and a paid model only when no free one is available or
you pass `--strong`. The reviewer must not share a quota family with the
executor that wrote the code.

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
- Say plainly what was not done and what you discarded.
