# The Pit Wall method

ACC's way of running a job: cheap workers do most of the work, risky changes
get independent eyes before they are accepted. Themed on motor racing.

## Stint

One worker does one job in its lane worktree. The coordinator checks it (step 5
of SKILL.md). That run is a **Stint**.

## Fresh tyres

Before every new attempt of a job, reset the lane worktree to the commit it had
before the job started:

```text
git -C <worktree> rev-parse HEAD   # record as <start> before the first attempt
git -C <worktree> reset --hard <start>
git -C <worktree> clean -fd
```

The next worker never inherits a failed attempt's edits or untracked files.

## Relay

A failed stint hands the job over:

1. Attempt 2: pick another model with `ACC pick --kind <k> --exclude <failed>`;
   same cost tier is allowed. Run on fresh tyres with the findings in the spec.
2. Attempt 3: one cost tier up (`--max-cost` raised by one step), still on
   fresh tyres.
3. After three failed attempts, the coordinator fixes the job itself or stops
   the lane and reports it.

## Scrutineering

Applies to **high-risk** jobs (and per pace below). A stint that passes the
coordinator's check is not yet accepted.

### When it applies

- Risk `high`: always.
- Pace `push`: every job except `low` risk.
- Pace `eco` / `race`: only `high` risk.

### How

1. Pick two scrutineers: `ACC pick --kind review --exclude <worker>`, then
   `--exclude <first-scrutineer>` too. Prefer a different model family
   (`models.toml` notes say which). They run in parallel in the lane worktree,
   read-only.
2. Each scrutineer reads the diff range `<start>..HEAD`, the job spec path and
   writes findings using the prompt in `templates/scrutineer.md`.
   - Plain mode: output to `../<repo>-acc-logs/<lane>-<job>-scrutineer-<n>.md`.
   - Orca mode: findings come back in the `worker_done` message.
3. The coordinator merges findings, drops duplicates and any it cannot confirm.
4. Confirmed findings go back to the **same worker model** in a fresh session
   (a **pit stop**), on fresh tyres. At most 2 pit stops, then pass or Relay.
5. Score each scrutineer as `--kind review`: confirmed findings count for it,
   false alarms against it.

Fewer than two eligible scrutineers (by pace, `models.toml` or missing CLIs):
use the one available plus the coordinator's own review, and say so in the
report.

## Risk

Set per job in the plan. Shown at the approval stop and in the report.

| Level | Examples |
| --- | --- |
| `low` | docs, typos, comments, pure refactor with tests |
| `normal` | features, bug fixes, new tests |
| `high` | auth/authz, money, data loss or migrations, concurrency, secrets handling, input validation on trust boundaries |

Only `high` adds Scrutineering by default; pace can raise that.

## Pace

Set per request, e.g. "ACC, do X, pace push". Default is `race`. Paid models
are used only when the user says so, at any pace.

| Pace | Workers | Scrutineering | Scrutineers | Implied `--max-cost` |
| --- | --- | --- | --- | --- |
| `eco` | free only | high risk | free only | `free` |
| `race` | cheapest good (`ACC pick`) | high risk | free + included | `included` |
| `push` | may start at included | every job except low risk | free + included | `cheap` |

The coordinator passes the implied `--max-cost` to `ACC pick` unless the user
set one explicitly.