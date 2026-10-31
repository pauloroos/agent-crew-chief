---
name: crew-chief
description: Run a request through several coding agents in parallel with Orca and score every model on every job, so the next pick is better. Splits the work into lanes (one branch each), runs the lanes at the same time, monitors them, reviews and scores each job, and delivers one pull request. Use when the user says "ACC", "crew chief", "load ACC, do X", or asks to have agents or models do the work and be compared. Not for a one-line edit or plain conversation.
---

# Agent Crew Chief

You are the coordinator. You plan, pick a model per job, start the workers,
watch them, review what they did, score it and deliver. Workers are Orca agents
in their own worktrees. ACC adds one thing to Orca: a score per model and kind
of work, kept in `~/.crew-chief/ledger.jsonl`, outside every repository.

`ACC` below is `python -I .agents/skills/crew-chief/scripts/crew-chief.py`
(or `.claude/skills/...`, whichever exists). Orca commands follow the guide
from `orca skills get orchestration`: load it once before the first one.

Any CLI can coordinate (Claude Code, Codex, OpenCode, ...), and its workers
can be any Orca agent. Run in an Orca terminal of the project, so Orca knows
you as the coordinator; if `orca status --json` shows no running Orca, stop
and tell the user to open it.

Do not ask permission between steps. Ask only when the request itself is
unclear; otherwise report at the end.

## 1. Plan lanes

- Read the request and the code it touches. Split it into **jobs**: one
  reviewable change each, with a kind (`code`, `tests`, `refactor`, `design`,
  `docs`, `research`, `diagnosis`, `data`, `review`) and a check that proves
  it is done (a test, a command, an output).
- Group the jobs into **lanes**. Jobs that touch the same files or need each
  other's result share a lane and run in order. Lanes that do not overlap run
  at the same time. At most 4 lanes; one lane is fine for small work.
- Branches: the feature branch `acc/<slug>` from the base branch, and one
  branch per lane, `acc/<slug>-<lane>`, from the feature branch.
- Tell the user the plan in a few lines (lanes, jobs, models) and go on.

## 2. Pick a model per job

`ACC pick --kind <kind>` names the cheapest model that scored well at least
twice, then the scoreboard. "Not proven yet" means little data: use it, or
try a model with no score yet, and the score you record settles it. Prefer
free models for plain work; use a stronger one for risky or subtle jobs, or
with `--max-cost` when the user sets a limit. The agent is the Orca agent id
(`codex`, `claude`, `opencode`, ...) and the model its `--model` value.

## 3. Start the lanes

```text
orca orchestration run-create --objective "<the request>" --json
orca worktree create --name <slug>-<lane> --base-branch acc/<slug> --branch acc/<slug>-<lane> --setup skip --json
orca orchestration worker-start --spec "<job spec>" --worktree branch:acc/<slug>-<lane> --agent <agent> --model <model> [--effort <level>] --json
```

Start the first job of every lane before waiting. A job spec is
self-contained and names:

- **Target** - the files or area in scope.
- **Change** - the result to produce.
- **Constraints** - what must not change; no secrets, `.env` or private data.
- **Acceptance** - the check that must pass.
- **Finish** - commit the work on the current branch with a clear message; do
  not push, merge or open a pull request; then send `worker_done`.

## 4. Monitor

```text
orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 100000 --json
```

Keep each wait under 2 minutes: your CLI kills longer commands. An empty
result only means wait again. Answer questions, handle every `worker_done`, ack, and wait again until every
lane is done. Follow the Orca guide for empty waits and stuck workers.

A worker can miss its spec on a cold start: after 5 minutes with no commit,
look at `orca orchestration worker-read --dispatch <id> --source auto --json`.
An empty prompt means it never started: `worker-stop` it and start the job
again, recording nothing.

## 5. Review and score every job

On each `worker_done`:

1. Read the lane's new commits (`git -C <lane worktree> log -p <before>..HEAD`)
   and run the job's check yourself. Do not trust the worker's summary.
2. Score the worker 0-100: 90+ done and clean; 70-89 done with small fixes;
   40-69 partly done or needed real fixes; below 40 wrong, broken or nothing.
   Score what the worker delivered, even when you fix it afterwards.
3. Record it, always, pass or fail:
   `ACC record --kind <kind> --agent <agent> --model <model> [--effort <e>] --cost <free|included|cheap|paid> --outcome <pass|fail> --score <n> --minutes <n> --lane <lane> --job "<title>" --note "<why>"`
4. Pass: release the worker and start the lane's next job in the same
   worktree. Fail: start the job again with another model (next in
   `ACC pick`, or one tier up), with the review findings added to the spec.
   After two failed retries, fix it yourself or stop the lane and report it.

## 6. Deliver

- When every lane passed: in your checkout on `acc/<slug>`, merge each lane
  (`git merge --no-ff acc/<slug>-<lane>`), resolve conflicts, run the
  project's tests, push the branch and open the pull request with `gh pr create`.
- Release every worker and remove the lane worktrees (`orca worktree rm
  --worktree branch:acc/<slug>-<lane>`, which also deletes the lane branch).
  If it says the folder is open in another program, a worker's leftover
  process holds it: leave it and say so in the report.
- Report to the user: the pull request, each job with its model, outcome and
  score, anything left open, and `ACC score --kind <kinds used>`.

## Rules

- Never commit or push to `main`, never `--force`, never rewrite pushed
  history. The owner merges the pull request unless they said otherwise.
- Workers never push, merge or open pull requests; you do.
- Orca may put the lane worktrees inside your checkout: never `git add -A`
  or `git add .` there; add files by name.
- Never create, copy or ask for credentials or tokens.
- `ACC score` shows the whole scoreboard; `ACC import <project>` copies the
  grades an ACC 0.6 project kept in `docs/crew-chief/jobs` into the ledger.
