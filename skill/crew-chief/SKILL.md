---
name: crew-chief
description: Run a request through several coding agents in parallel and score every model on every job, so the next pick is better. Writes a short spec first when the request is not small, splits the work into lanes (one branch each), dispatches a race engineer per lane, reviews and scores each job and each engineer, and delivers one pull request. Use when the user says "ACC", "crew chief", "load ACC, do X", or asks to have agents or models do the work and be compared. Not for a one-line edit or plain conversation.
---

# Agent Crew Chief

You are the **crew chief** on the pit wall. You spec, plan lanes, pick models, hold the approval
stop, dispatch a **race engineer** per lane, check their reports, grade them, merge the lanes
and deliver. Engineers run drivers and scrutineers; you never run drivers yourself except a small
request without a spec, where you may engineer its one lane.

ACC keeps a score per model and kind in `~/.crew-chief/ledger.jsonl`. `pit-wall.md` governs Stint, Fresh tyres, Relay, Scrutineering, risk and pace.

`ACC` = `python -I .agents/skills/crew-chief/scripts/crew-chief.py` (or `.claude/skills/...`). Any CLI can coordinate (Claude Code, Codex, OpenCode).

Every child is visible in a terminal: **Orca mode** when `orca status --json` shows a running
Orca (Orca terminal per lane worktree; load `orca skills get orchestration` once), else
**background mode** (process with log and PID).

Do not ask permission between steps except the spec approval in step 0 (none for an imported
spec or with nobody to approve). Ask only when unclear; report at the end.

## The team

- **Crew chief** (you): specs, plans, picks, approves, dispatches engineers, checks lane
  reports, grades engineers, merges and delivers.
- **Race engineer**: one per lane. Coordinates that lane only: starts drivers, runs Stint /
  Relay / Fresh tyres / Scrutineering, records grades, sends a lane report. Brief:
  `templates/race-engineer.md`.
- **Driver**: worker agent doing one job in the lane worktree.
- **Scrutineer**: read-only reviewer of high-risk jobs (per pace). Scored as `review`.

## 0. Spec first

An approved spec (`spec-aprovada.md`, or `Spec: <file>` + `Anexo: <file>` lines) is always
imported, even for a small request: `ACC spec-import <file> [--anexo <file>]... --slug <slug>`
copies it byte for byte into the folder of item 2 with its hash; never write or edit its
`spec.md`, derive only `tasks.md` there, skip the approval stop; go on from item 4. Refused
because the slug holds another spec: use another slug and report it. No approved spec: skip the
spec for a small request or "run directly" / "sem spec".

1. Read the request and code; ask only questions whose answer changes what gets built (at most
   5, one message). On "decide for me", or with nobody to approve (`CREW_CHIEF_JOB_ID` set)
   and no spec: write the spec, assumptions in `spec.md`, no stop in item 5.
2. Folder: a specs folder the project already has, else `specs/<NNN>-<slug>/` at the repo root.
3. Write `spec.md` and `tasks.md` from `templates/spec/` (`plan.md` only when the design is
   not obvious); in `tasks.md` mark `[P]` on parallel tasks and write "depends on Txxx".
4. Create `<feature>` (see Branches); commit the whole spec folder there (`git add
   specs/<NNN>-<slug>`) before any lane branch.
5. Plan lanes (step 1) and pick models (step 2), then stop once (never with an imported
   spec or nobody to approve): show the spec summary, each job's risk/pace and lanes; wait.

## 1. Plan lanes

Read the request and the code it touches. Set each job's **risk** (`low`/`normal`/`high`) and
read the **pace** from the request (default `race`); see `pit-wall.md`. Split work into
**jobs**: one reviewable change each, with a kind (`code`, `tests`, `refactor`, `design`,
`docs`, `research`, `diagnosis`, `data`, `review`) and a check that proves it done.

Group jobs into **lanes**: same-file or dependent jobs share a lane (in order); non-overlapping
jobs run in parallel. At most 4 lanes. With a spec, dependent/same-file tasks share a lane;
`[P]` tasks with no shared files get separate lanes.

Branches: `<feature>` = `acc/<slug>` from base, lane branches `<feature>-<lane>`. With
`Branch: <name>` and `Base: <branch>`: `<feature>` = `<name>` from `origin/<Base>` (`git fetch`
first, never the current branch); lanes stay local (never pushed), the PR goes to `Base`. No
spec: brief plan, go on.

## 2. Pick models

Run `ACC models` first. If it lists models, pick only among them (`ACC pick`). `ACC pick
--kind <kind>` draws among the cheapest models that scored well twice, then the scoreboard.
"Not proven yet" means little data: use it, or try an unscored model; the score you record
settles it. Prefer free models for plain work, stronger ones for risky jobs, `--max-cost` from
the pace or the user (`$CREW_CHIEF_MAX_COST`, when set, is the default ceiling). Use `--exclude <agent:model>` (repeatable) to drop failed workers or
the first scrutineer.

## 3. Dispatch a race engineer per lane

Create each lane's worktree first as a child of the crew chief worktree, so Orca shows it
under the run. Orca mode:

```
orca orchestration run-create --objective "<the request>" --json
orca worktree create --name <slug>-<lane> --base-branch <feature> --branch <feature>-<lane> --setup skip --parent-worktree <crew chief worktree selector> --json
```

For every lane, fill `templates/race-engineer.md` with the lane, branch, worktree, jobs, pace,
spec folder and `ACC` path. In Orca mode the engineer always starts as an Orca worker in the
lane worktree, never as your own subagent in any harness; drivers and scrutineers get Orca
terminals there too:

```
orca orchestration worker-start --spec "<engineer brief>" --worktree branch:<lane branch> --agent <agent> --model <model> --json
orca terminal rename --terminal <handle> --title acc-<slug>-<lane>-engineer --json
orca terminal create --worktree path:<worktree> --title acc-<slug>-<lane>-<job>-<role> --command "<driver command>" --json
```

The rename `<handle>` comes from the `worker-start` receipt, `worker-show`, or `orca terminal
list --worktree <lane worktree> --json`. Use `worker-start` when Orca knows the agent, then
rename to the standard title; `orca agent launch` for a lone terminal passes `--surface
terminal`. No terminal handle: skip the rename; use `orca terminal create --title <standard
name>` or note it. Names: `acc-<slug>-<lane>[-<job>]-<role>`, plus `-<n>`; the engineer has
no `<job>`. Engineers run in parallel; without Orca, steps 3-4 become "Without Orca
(background mode)".

Launched round (`ACC launch --crew`): start each lane engineer with `ACC launch --engineer
--slug <slug> --lane <lane> --brief <file>` (direct mode, standard title
`acc-<slug>-<lane>-engineer`, no `--model`: `ACC pick --kind coordinate`); wait with `ACC runs
<id>`. Do not end your turn before the PR and `ACC summary` (else resumed, then rc 5).

## 4. Monitor

Wait on engineer reports. Orca mode: `check --wait` under 2 minutes per call. Background mode:
process, commits, log growth. Answer questions, handle escalations, wait again.

## Without Orca (background mode)

Replaces steps 3-4. Per lane: `git worktree add ../<repo>-acc-<lane> -b <feature>-<lane>
<feature>`. Name each child `acc-<slug>-<lane>[-<job>]-<role>` (or `...-<role>-<n>`; no
`<job>` for the engineer). Write the job spec to `../<repo>-acc-logs/<name>.md` and start
the driver in the background from inside the lane worktree with the prompt "Do the job in
<its absolute path>". Log `../<repo>-acc-logs/<name>.log`, PID in
`../<repo>-acc-logs/<name>.pid`, never inside the worktree; kill with Linux/macOS
`kill -- -<pid>` or Windows PS `taskkill /PID <pid> /T /F`.

## Working rules (for drivers)

Every driver spec carries this block so workers use the kit:

> Use test-driven-development for code changes, systematic-debugging for bugs, and
> verification-before-completion before you finish. Follow ponytail: the smallest complete
> change. If these skills are not installed, read this as plain guidance.

## 5. Check lane reports and grade engineers

Race engineers run as Orca workers or background processes. Record their `coordinate` grades
under the engineer's own `agent:model` (`ACC pick --kind coordinate`). On each lane report:

1. Run the lane's checks yourself; read the scrutineer findings of high-risk jobs. Do not
   trust the engineer's summary.
2. Grade the engineer 0-100 as `coordinate` work: `ACC record --kind coordinate --agent <agent> --model <model> --cost <tier> --outcome <pass|fail> --score <n> --minutes <n> --lane <lane> --job "<lane>" --note "<why>"`.
3. Accept the lane or send findings back for another stint.

## 6. Deliver
When every lane passed: in your checkout on `<feature>`, merge each lane (`git merge --no-ff
--no-edit <feature>-<lane>`), resolve conflicts, run the project's tests, use
verification-before-completion, then push and open the PR with `gh pr create` (`--base <Base>`
when the request has one). With every grade recorded, run `ACC summary --job <slug> --pr <url>`
(`--rc 1` if not delivered) in every run that opens or updates a PR, before cleanup; then
release engineers, remove lane worktrees and branches: Orca `orca worktree rm --worktree
branch:<feature>-<lane>`; background-mode `git worktree remove ../<repo>-acc-<lane>`, `git
branch -D <feature>-<lane>`. Both modes, once nothing runs: remove `../<repo>-acc-logs/`. Never delete the run files ACC launch writes (`<id>.meta.json`, `.log`, `.pid`, `.rc` in the runs folder): `ACC pick` and `ACC wait-children` read them. Report:
PR, each job with risk, pace, model, attempts, scrutineer scores, outcome, worker and engineer
scores, anything open, `ACC score --kind <kinds used>`. `ACC summary` exits 1 with
`spec_divergente` when the committed `spec.md` is not the approved one: report it.

## Launcher
`ACC launch` starts a run on a request file: an Orca terminal when Orca runs, else background.
`ACC launch --brief <f> [--crew|--direct]` starts a whole run; `ACC launch --engineer --slug <slug>
--lane <lane> --brief <file>` starts a lane engineer (direct mode, standard title
`acc-<slug>-<lane>-engineer`: Orca tab when Orca runs, background otherwise). `ACC runs [id]` and
`ACC compare` follow it; a launched crew chief starts each lane engineer with `ACC launch
--engineer` and waits with `ACC runs <id>`; before resuming it, the run script runs `ACC wait-children <id>`.

## Rules

- Never commit or push to `main`, never `--force`, never rewrite pushed history. The owner
  merges the pull request unless they said otherwise.
- Engineers and drivers never push, merge or open pull requests; you do.
- Orca may put lane worktrees inside your checkout: never `git add -A` or `git add .` there;
  add files by name.
- Never create, copy or ask for credentials or tokens.
- `ACC score` shows the whole scoreboard; `ACC import <project>` copies the grades an ACC 0.6
  project kept in `docs/crew-chief/jobs` into the ledger.
- `ACC merge <file>` merges a ledger file.
- `ACC sync --init <url>` sets up sync through a private git repo; `ACC sync` runs it: when
  set up, run it at the start of a request and after `ACC summary`. It also carries the
  `jobs/<machine>/` lines of `ACC summary` or a launcher; never put a brief, log or diff there.
