# 04 · The maestro: one coordinator per front

Here the agent you talk to becomes the **maestro**: it splits a goal into
fronts, launches one coordinator per front, keeps them unblocked and hands you
one PR per front. Each coordinator runs the full job cycle (free executors
first, review by a different model family, grades, scoreboard) inside its own
worktree, and every round it runs is timed and counted.

```
you ─▶ maestro (your agent session)
          ├─▶ coordinator A  (own worktree + branch) ─▶ executors / reviewers ─▶ PR
          ├─▶ coordinator B  (own worktree + branch) ─▶ executors / reviewers ─▶ PR
          └─▶ coordinator C  ...
```

It builds on tutorials 01–03 and uses the
[`examples/hello-crew/`](../../examples/hello-crew/) project.

## 1. Install the `acc` command

```bash
git clone --branch v0.6.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py --command
acc --version
```

The installer writes `acc` (`acc.cmd` on Windows) into your user bin directory
and prints where; add it to `PATH` if it is not there.

## 2. Choose the coordinators

List them in `docs/crew-chief/preferences.toml`, in the order they are tried,
and commit the file (each coordinator works from a new worktree of `HEAD`):

```toml
[team]
coordinators = ["opencode:omni/chefe", "claude:opus", "codex:gpt-6.1-sol"]
```

Any configured tool works (`claude`, `codex`, `opencode`, `antigravity`, a
`generic` tool). When the first hits a quota or fails, the next takes over the
same worktree and job on its own.

## 3. Give the maestro a goal

Open your agent in the project and ask for the maestro with a goal that has
independent parts:

```text
Use the crew-chief maestro. Goal: add a `farewell(name)` function with tests,
and add a `--shout` option to the CLI that upper-cases the greeting.
```

It answers with the fronts before launching anything: one independent outcome
per PR, no two fronts on the same files. Approve once.

## 4. Launch and watch

For each front the maestro runs:

```bash
acc launch --request-file fronts/01-farewell.md
acc launch --request-file fronts/02-shout.md --coordinator codex:gpt-6.1-sol
```

and then, every few minutes:

```bash
acc status
```

```text
3f9a1c2e  01-farewell  running  coordinator opencode:omni/chefe  rounds 1 (switches 0)  12m40s  coordinator tokens 48211
    job 20261029-farewell: free work: 100% of pieces, 81% of tokens · coordinator: 19% · 1.5 attempts/piece · mean score 88
```

| State | What the maestro does |
|---|---|
| `question` | answers with `acc resume ID --answer "..."` when the goal decides it; otherwise asks you and relays your answer |
| `quota` | every coordinator of the list hit a limit: `acc resume ID` once one resets |
| `failed` | reads `last_error` and the logs in `<project>.crew/coordinators/ID/`; resumes only after the cause is fixed |
| `completed` | notes the PR |

## 5. Merge

The maestro reports every front with its PR, rounds, time, tokens, accepted
pieces and score. Review and merge each PR yourself; ACC never merges, pushes
to the base branch or tags.

## Limits

- Fronts must be independent; overlapping files mean conflicting PRs.
- A coordinator gets six rounds per run; `acc resume` gives it six more.
- `acc stop ID` stops a coordinator; its worktree and branch
  (`maestro/<front>-<id>`) stay for you.
