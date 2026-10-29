# 05 · The maestro: several coordinators at once

In tutorial 04 you launched one coordinator yourself. Here the agent you talk
to does it for you: it becomes the **maestro**, splits a goal into fronts,
launches one coordinator per front, keeps them unblocked and hands you one PR
per front. Each coordinator still runs the full job cycle (free executors
first, review by a different model family, grades, scoreboard) inside its own
worktree.

```
you ─▶ maestro (your agent session)
          ├─▶ coordinator A  (own worktree + branch) ─▶ executors / reviewers ─▶ PR
          ├─▶ coordinator B  (own worktree + branch) ─▶ executors / reviewers ─▶ PR
          └─▶ coordinator C  ...
```

It builds on tutorial 04: install the `acc` command and ACC in the project
first, and commit the install.

## 1. Give the maestro a goal

Open your agent (Claude Code or Codex) in the project and ask for the
maestro with a goal that has independent parts:

```text
Use the crew-chief maestro. Goal: add a `farewell(name)` function with tests,
and add a `--shout` option to the CLI that upper-cases the greeting.
```

## 2. Approve the fronts

The maestro answers with a plan before launching anything. A front is one
independent outcome that fits one PR and touches files no other front
touches:

```text
Front 1  farewell(name) + tests        src/hello_crew/farewell.py, tests/   claude
Front 2  --shout option + tests        src/hello_crew/cli.py, tests/cli/   codex
```

Work that depends on another front waits for that front's PR; two fronts never
share files. Approve once and the maestro writes each front's request to its
own file.

## 3. Launch

For each front the maestro runs one launch from the project root:

```bash
acc launch --repo . --request-file fronts/01-farewell.md --mode raw-request --authorize-raw --cli claude
acc launch --repo . --request-file fronts/02-shout.md --mode raw-request --authorize-raw --cli codex
```

Each launch prints `launched coordinator <ID>`, with its own worktree and
branch. Your checkout is never touched.

## 4. Watch

The maestro reads the state of every coordinator from one JSON snapshot,
every few minutes:

```bash
acc central --json
```

You can watch the same coordinators yourself with `acc central`. What the
maestro does with each state:

| State | What the maestro does |
|---|---|
| a pending question | answers it with `acc message ID --reply-to QUESTION_ID --text "..."` when the goal decides it; otherwise asks you and relays your answer |
| `quota` | the coordinator CLI hit a limit and is held: `acc resume ID` once it resets, or `acc stop ID` and relaunch the front with another `--cli` |
| `paused` (no progress) | reads the last round, sends guidance with `acc message`, then `acc resume ID` |
| `failed` | reads `last_error`; `acc recover ID` only after the cause is fixed |
| `completed` | notes the branch and the PR |

## 5. Merge

The maestro reports every front with its PR (or branch), accepted pieces and
score, and anything left. Review and merge each PR yourself; ACC never merges,
pushes to the base branch or tags.

## Limits

- Fronts must be independent. Overlapping files mean conflicting PRs; split
  differently or run the fronts one after another.
- The maestro is your agent session: it needs to stay open to keep answering
  questions. The coordinators keep working without it and their questions
  wait in `acc central`.
- There is no automatic coordinator rotation: a quota holds the coordinator
  until it is resumed or relaunched.
