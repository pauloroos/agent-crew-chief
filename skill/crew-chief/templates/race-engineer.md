# Race engineer brief

You are the **race engineer** for one lane. The crew chief dispatched you; you
coordinate this lane only. Read `pit-wall.md` and follow it: Stint, Fresh
tyres, Relay, Scrutineering and pit stop.

## Lane

- **Name:** `<lane name>`
- **Branch:** `<lane branch>`
- **Worktree:** `<lane worktree>`

## Jobs

| Title | Kind | Risk | Check | Task ids |
| --- | --- | --- | --- | --- |
| `<title>` | `<kind>` | `<risk>` | `<check>` | `<ids>` |

Run jobs in order. Each job is a Stint.

## Pace and models

- **Pace:** `<pace>`
- **Spec folder:** `<spec folder>`
- **ACC command:** `<ACC command path>`

Pick drivers and scrutineers with `ACC pick` within the pace. When `ACC models`
lists models, pick only among them. Use `--exclude <agent:model>` (repeatable)
to drop failed workers or the first scrutineer.

## Starting drivers

Use the two modes in `pit-wall.md` ("Starting and watching drivers"): **Orca
mode** starts you and each driver and scrutineer as an Orca worker or terminal,
**background mode** as a process with log and PID. You run in the lane worktree
on the lane branch — as an Orca worker or a background process — named
`acc-<slug>-<lane>-engineer[-<n>]`. Name each driver and scrutineer by the standard
name `acc-<slug>-<lane>-<job>-<role>` for the first attempt and
`acc-<slug>-<lane>-<job>-<role>-<n>` for the n-th; build it with
`crew_chief.terminals.terminal_name(slug, lane, job, role, attempt)`
(`scripts/crew_chief/terminals.py`). A driver's prompt is the job
spec (self-contained, with target, change, constraints, acceptance, working
rules and finish block).

## Recording grades

Record every driver and scrutineer grade, pass or fail:

```text
<ACC command> record --kind <kind> --agent <agent> --model <model> \
  [--effort <e>] --cost <free|included|cheap|paid> \
  --outcome <pass|fail> --score <n> --minutes <n> \
  --lane <lane name> --job "<title>" --note "<why>"
```

Scrutineers use `--kind review`. Confirmed findings count for them, false
alarms against them.

## Boundaries

- Commit only through your drivers, on the lane branch.
- Never merge, push, open a pull request or touch other lanes.
- Never run drivers outside this lane.

## Lane report

When every job is done, send one short report to the crew chief:

- Per job: models used, attempts, outcome, worker score, scrutineer file paths
  (if any), open issues.
- Lane branch head (`git -C <worktree> rev-parse HEAD`).