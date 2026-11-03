---
name: crew-chief
description: Run a request through several coding agents in parallel and score every model on every job, so the next pick is better. Writes a short spec first when the request is not small, splits the work into lanes (one branch each), dispatches a race engineer per lane, reviews and scores each job and each engineer, and delivers one pull request. Use when the user says "ACC", "crew chief", "load ACC, do X", or asks to have agents or models do the work and be compared. Not for a one-line edit or plain conversation.
---

# Agent Crew Chief

You are the **crew chief** on the pit wall. You spec, plan lanes, pick models,
hold the approval stop, dispatch a **race engineer** per lane, check their
reports, grade them, merge the lanes and deliver. Engineers run drivers
(workers) and scrutineers; you never run drivers yourself except a small
request without a spec, where you may engineer its one lane yourself.

ACC keeps a score per model and kind in `~/.crew-chief/ledger.jsonl`, outside
every repository. The Pit Wall method (`pit-wall.md`) governs Stint, Fresh
tyres, Relay, Scrutineering, risk and pace.

`ACC` = `python -I .agents/skills/crew-chief/scripts/crew-chief.py` (or
`.claude/skills/...`). Any CLI can coordinate (Claude Code, Codex, OpenCode).

Every child of the crew is visible in a terminal, in one of two modes:
**Orca mode** when `orca status --json` shows a running Orca (an Orca terminal
each in the lane worktree; load `orca skills get orchestration` once), and
**background mode** without Orca (e.g. Windows without WSL: a process with log
and PID).

Do not ask permission between steps except the spec approval in step 0. Ask
only when the request is unclear; report at the end.

## The team

- **Crew chief** (you): specs, plans, picks, approves, dispatches engineers,
  checks lane reports, grades engineers, merges and delivers.
- **Race engineer**: one per lane. Coordinates that lane only: starts drivers,
  runs Stint / Relay / Fresh tyres / Scrutineering, records grades, sends a
  lane report. Brief: `templates/race-engineer.md`.
- **Driver**: the worker agent that does one job in the lane worktree.
- **Scrutineer**: read-only reviewer of high-risk jobs (per pace). Scored as
  `review` work.

## 0. Spec first

Skip for a small request (one job, one file area, no open question) or when
the user says "run directly" / "sem spec".

1. Read the request and the code. Before the spec, ask only questions whose
   answer changes what gets built: at most 5, in one message. On "decide for
   me", decide and write assumptions into `spec.md`.
2. Folder: use a specs folder the project already has; otherwise
   `specs/<NNN>-<slug>/` at the repo root, `<NNN>` the next free number.
3. Write `spec.md` and `tasks.md` from `templates/spec/`; add `plan.md` only
   when the design is not obvious. In `tasks.md`, mark `[P]` on parallel tasks
   and write "depends on Txxx" for order.
4. Create `acc/<slug>` from the base branch and commit the spec there before
   any lane branch exists.
5. Plan lanes (step 1) and pick models (step 2), then stop once: show the spec
   summary, each job's risk and pace, and the lanes, and wait for approval.

## 1. Plan lanes

Read the request and the code it touches. Set each job's **risk**
(`low`/`normal`/`high`) and read the **pace** from the request (default
`race`); see `pit-wall.md`. Split work into **jobs**: one reviewable change
each, with a kind (`code`, `tests`, `refactor`, `design`, `docs`, `research`,
`diagnosis`, `data`, `review`) and a check that proves it done.

Group jobs into **lanes**. Jobs that touch the same files or need each other's
result share a lane and run in order; non-overlapping jobs run in parallel. At
most 4 lanes. With a spec, dependent or same-file tasks share a lane; `[P]`
tasks with no shared files get separate lanes.

Branches: feature branch `acc/<slug>` from base, one branch per lane
`acc/<slug>-<lane>` from the feature branch. Without a spec, tell the user the
plan in a few lines and go on.

## 2. Pick models

Run `ACC models` first. If it lists models, pick only among them (`ACC pick`).
`ACC pick --kind <kind>` names the cheapest model that scored well at least
twice, then the scoreboard. "Not proven yet" means little data: use it, or try
a model with no score yet; the score you record settles it. Prefer free models
for plain work, stronger ones for risky jobs, `--max-cost` from the pace or
the user. Use `--exclude <agent:model>` (repeatable) to drop failed workers or
the first scrutineer.

## 3. Dispatch a race engineer per lane

Create each lane's worktree first as a child of the crew chief worktree, so Orca shows it under the run. Orca mode:

```text
orca orchestration run-create --objective "<the request>" --json
orca worktree create --name <slug>-<lane> --base-branch acc/<slug> --branch acc/<slug>-<lane> --setup skip --parent-worktree <crew chief worktree selector> --json
```

For every lane, fill `templates/race-engineer.md` with the lane, branch,
worktree, jobs, pace, spec folder and `ACC` path. In Orca mode the engineer
always starts as an Orca worker in the lane worktree, never as your own
subagent in any harness; drivers and scrutineers get Orca terminals there too:

```text
orca orchestration worker-start --spec "<engineer brief>" --worktree branch:<lane branch> --agent <agent> --model <model> --json
orca terminal rename --terminal <handle> --title acc-<slug>-<lane>-engineer --json
orca terminal create --worktree path:<worktree> --title acc-<slug>-<lane>-<job>-<role> --command "<driver command>" --json
```

The rename `<handle>` comes from the `worker-start` receipt, `worker-show`,
or `orca terminal list --worktree <lane worktree> --json`. Use `worker-start`
when Orca knows the agent, then rename the terminal to the standard title;
`orca agent launch` for a lone terminal always passes `--surface terminal`.
No terminal handle (a worker without a terminal, or a Chat UI that hides it):
skip the rename; use `orca terminal create --title <standard name>` or note it
in the lane report. Names: `acc-<slug>-<lane>[-<job>]-<role>`, plus `-<n>` for
the n-th attempt; the engineer has no `<job>`. Engineers run in parallel;
without Orca, steps 3–4 become the "Without Orca (background mode)" section.

## 4. Monitor

Wait on engineer reports. In Orca mode, `check --wait` under 2 minutes per call;
in background mode, process, commits and log growth. Answer questions, handle
escalations, wait again.

## Without Orca (background mode)

Replaces steps 3–4. Per lane: `git worktree add ../<repo>-acc-<lane> -b
acc/<slug>-<lane> acc/<slug>`. Name each child
`acc-<slug>-<lane>[-<job>]-<role>`, or `...-<role>-<n>` for the n-th (no
`<job>` for the engineer). Write the job spec to `../<repo>-acc-logs/<name>.md`
and start the driver in the background from inside the lane worktree with the
prompt "Do the job in <its absolute path>". Log `../<repo>-acc-logs/<name>.log`,
PID in `../<repo>-acc-logs/<name>.pid`, never inside the worktree; kill with
Linux/macOS `kill -- -<pid>` or Windows PS `taskkill /PID <pid> /T /F`.

## Working rules (for drivers)

Every driver spec carries this block so workers use the kit:

> Use test-driven-development for code changes, systematic-debugging for bugs,
> and verification-before-completion before you finish. Follow ponytail: the
> smallest complete change. If these skills are not installed, read this as
> plain guidance.

## 5. Check lane reports and grade engineers

Race engineers run as Orca workers or background processes. Record their
`coordinate` grades under the engineer's own `agent:model`
(`ACC pick --kind coordinate`). On each lane report:

1. Run the lane's checks yourself; read the scrutineer findings of high-risk
   jobs. Do not trust the engineer's summary.
2. Grade the engineer 0-100 as `coordinate` work:
   `ACC record --kind coordinate --agent <agent> --model <model> --cost <tier> --outcome <pass|fail> --score <n> --minutes <n> --lane <lane> --job "<lane>" --note "<why>"`
3. Accept the lane or send findings back for another stint.

## 6. Deliver

When every lane passed: in your checkout on `acc/<slug>`, merge each lane
(`git merge --no-ff --no-edit acc/<slug>-<lane>`), resolve conflicts, run the
project's tests, use verification-before-completion, then push and open the
pull request with `gh pr create`. Release engineers; remove lane worktrees and
branches: Orca `orca worktree rm --worktree branch:acc/<slug>-<lane>` (deletes
the branch too); background-mode `git worktree remove ../<repo>-acc-<lane>`, `git branch
-D acc/<slug>-<lane>` and, once nothing runs, `../<repo>-acc-logs/`. A folder
"open in another program" is held by a leftover process: leave it, report it.
Report: PR, each job with risk, pace, model, attempts, scrutineer
scores, outcome, worker and engineer scores, anything open, and
`ACC score --kind <kinds used>`.

## Rules

- Never commit or push to `main`, never `--force`, never rewrite pushed
  history. The owner merges the pull request unless they said otherwise.
- Engineers and drivers never push, merge or open pull requests; you do.
- Orca may put lane worktrees inside your checkout: never `git add -A` or
  `git add .` there; add files by name.
- Never create, copy or ask for credentials or tokens.
- `ACC score` shows the whole scoreboard; `ACC import <project>` copies the
  grades an ACC 0.6 project kept in `docs/crew-chief/jobs` into the ledger.
- `ACC merge <file>` merges a ledger file copied from another machine.
- `ACC sync --init <url>` sets up sync through a private git repo; `ACC sync` runs it: when set up, run it at the start of a request and after the last grade. It also carries `jobs/<machine>/` summaries written by a launcher; never put a brief, log or diff there.
