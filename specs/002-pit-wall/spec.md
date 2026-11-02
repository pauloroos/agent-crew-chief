# The Pit Wall method

**Created**: 2026-11-01
**Status**: Draft

## What the user wants

ACC's own way of running a job, so that a cheap worker does most of the work
and risky changes get independent eyes before they are accepted. It is themed
on motor racing, like the name "crew chief". The user picks a **pace** for the
request. The coordinator marks each job's **risk** in the plan, and the user
can change it at the approval stop.

The trigger was the 0.6 audit of Contratada Fácil. ACC's own grades (70-94)
let a critical account takeover through, and only an independent review found
it. Retries also ran in a worktree that still held the failed attempt's
edits.

## The method

Every job runs as a **Stint**. One worker does the job in its lane worktree,
and the coordinator checks it (step 5, as today).

- **Fresh tyres.** Before every new attempt of a job, the lane worktree goes
  back to the commit it had before the job:
  `git -C <worktree> reset --hard <start>` and `git -C <worktree> clean -fd`.
  The next worker never inherits a failed attempt.
- **Relay.** A failed stint hands the job over. The next attempt uses another
  model (`ACC pick --exclude <failed>`), on fresh tyres, with the findings in
  its spec. The second failure moves one cost tier up. After three failed
  attempts, the coordinator fixes the job itself or stops the lane and
  reports it.
- **Scrutineering** (pt-BR "Vistoria"). This applies to high-risk jobs:
  security, auth and permissions, money, data loss or migrations,
  concurrency. A stint that passes the coordinator's check is not yet
  accepted.
  1. Two **scrutineers** review the job's diff at the same time. They are
     different models, never the worker's own model. They come from different
     families, read only (they never edit or commit), and each one writes its
     findings to a file.
  2. The coordinator merges the findings, drops duplicates and any it cannot
     confirm, and sends the confirmed ones back to the **same worker model**
     in a fresh session (a **pit stop**).
  3. After at most 2 pit stops, the job passes or goes to Relay.
  4. Each scrutineer is scored as `review` work: confirmed findings count for
     it, false alarms against it.

**Risk** (`low` / `normal` / `high`) is set per job in the plan. Only `high`
adds Scrutineering. The pace can raise that.

**Pace** is set per request, for example "ACC, do X, pace push". The default
is `race`.

| Pace | Workers | Scrutineering | Scrutineers |
| --- | --- | --- | --- |
| `eco` | free only | high risk | free only |
| `race` | cheapest good (pick) | high risk | free + included |
| `push` | may start at included | every job except low risk | free + included |

Paid models are used only when the user says so, at any pace.

## The team

ACC always starts in a chat with an agent. That agent is the **crew chief**
and stays on the pit wall:

- It writes the spec, plans lanes, picks models, holds the approval stop,
  merges the lanes and delivers.
- It never runs workers itself.
- The only exception is a small request run without a spec, where the crew
  chief may engineer its one lane itself.

For every lane, the crew chief dispatches a **race engineer**, a coordinator
agent that runs that lane:

- **Starting one:** use the crew chief's own subagent mechanism when it has
  one (for example Claude Code's Agent tool). Otherwise run a background CLI
  session with the engineer brief, the same way a worker is started in plain
  mode.
- **Brief:** `templates/race-engineer.md`, filled with the lane, its jobs
  with kind, risk and check, the pace, the spec folder and the rules.
- **Work:** the engineer starts the **drivers** (workers), runs Stint, Relay,
  Fresh tyres and Scrutineering, and records every driver and scrutineer
  grade with `ACC record`. It commits only through its drivers, on its lane
  branch. It never merges, pushes or opens a pull request.
- **Report:** one short lane report to the crew chief: each job with its
  models, attempts, outcome and scores, the scrutineer files and the lane
  branch head.
- **Checks on the engineer:** the crew chief checks each lane report. It
  runs the lane's checks itself and reads the scrutineer findings of high-risk
  jobs. It then grades the engineer as `coordinate` work (a new ledger kind),
  so engineer models get a score too.

Engineers run in parallel, one per lane, at most 4.

## Stories

### User Story 1 - Risky work gets scrutineers (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a request with a job marked `high`, **When** its stint passes
   the coordinator's check, **Then** two scrutineers review it in parallel,
   confirmed findings go back to the same worker model, and the job is
   accepted only after that.
2. **Given** a scrutineer finding the coordinator cannot reproduce or confirm,
   **When** the findings are merged, **Then** it is dropped and counts against
   that scrutineer's review score.

### User Story 2 - Retries start clean (Priority: P1)

1. **Given** a failed attempt that left edits and commits, **When** the next
   attempt starts, **Then** the worktree is at the job's start commit with no
   untracked files, and the new model is not the failed one.

### User Story 3 - One word sets the pace (Priority: P2)

1. **Given** "pace eco", **When** the plan is made, **Then** only free models
   are picked (`--max-cost free`) and the approval stop shows pace, risk and
   pattern per job.

### Edge cases (decided)

- Fewer than two eligible scrutineers (by pace, `models.toml` or missing
  CLIs): use the one available plus the coordinator's own review, and say so
  in the report.
- Scrutineers run in the lane worktree but must not write there. In plain
  mode, findings go to `../<repo>-acc-logs/<lane>-<job>-scrutineer-<n>.md`.
  In Orca mode, they come back in the worker_done message.
- A small request without a spec still gets risk and pace. There is no stop,
  so the coordinator states them in the short plan.

## Requirements

- **FR-001**: `ACC pick --exclude <agent:model>` (repeatable) drops those
  executors from the candidates, both with and without `models.toml`.
  Scrutineers are picked with `--kind review --exclude <worker>`, then
  `--exclude` again for the first scrutineer.
- **FR-002**: The method lives in a new `skill/crew-chief/pit-wall.md`
  (Stint, Fresh tyres, Relay, Scrutineering, pit stop, risk, pace table,
  commands). SKILL.md points to it at steps 1, 2 and 5 and stays at or under
  180 lines.
- **FR-003**: `skill/crew-chief/templates/scrutineer.md` is the scrutineer's
  prompt.
  - It is read-only and lists findings as `severity | file:line | failing
    scenario`, ending with `VERDICT: pass|fail`.
  - It checks for security holes (authz, injection, secrets), data loss,
    races, broken error paths, missing tests for the risky path, and
    behavior that differs from the spec.
- **FR-004**: The approval stop and the final report show each job's risk,
  pattern, models, attempts and scrutineer scores.
- **FR-005**: The README and README.pt-BR have a section "The Pit Wall
  method" / "O método Pit Wall" (Stint, Relay, Fresh tyres / Pneus novos,
  Scrutineering / Vistoria, pace). The tutorial names the pace, CHANGELOG
  [0.10.0] is added, and `__version__ = "0.10.0"`.

## Done when

- **SC-001**: `--exclude` tests pass. Docs tests check that SKILL.md and both
  READMEs name Stint, Relay, Fresh tyres, Scrutineering and pace, that
  `pit-wall.md` and `templates/scrutineer.md` exist, and that SKILL.md is at
  most 180 lines. CI is green on all 6 jobs.
- **SC-002**: A dry run on a copy of `examples/hello-crew` exercises
  Scrutineering end to end in plain mode. The job is one marked high, for
  example input validation in `greet`. It shows two scrutineer files, the
  merged findings, one pit stop and the `review` grades in `ACC score --kind
  review`.
