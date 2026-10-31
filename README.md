<div align="center">

<img src="assets/logo.svg" alt="agent-crew-chief logo" width="128" height="128">

# agent-crew-chief

**Parallel coding agents, a spec first, and a score for every model.**

You give your agent a request. It writes a short spec when the request is not small, splits the work into lanes (one branch each), runs the lanes at the same time, with [Orca](https://github.com/stablyai/orca) or with plain git worktrees, reviews and scores every job, and delivers one pull request. The scores tell it which model to pick next time.

[![CI](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/pauloroos/agent-crew-chief?color=f59e0b)](https://github.com/pauloroos/agent-crew-chief/releases/latest)
[![Python](https://img.shields.io/badge/python-3.11%2B-0f766e)](#install)
[![Dependencies](https://img.shields.io/badge/dependencies-none-0f766e)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-1e1b4b)](LICENSE)

**English** · [Português](README.pt-BR.md)

</div>

---

## What ACC is

Two things, both installed into your project:

- **A skill** (`crew-chief`) for the coding agent you already use: Claude
  Code, Codex, OpenCode, Gemini CLI or Cursor. The agent you talk to becomes
  the coordinator; other agents, in their own worktrees, are the workers.
- **A model scoreboard**, a small Python CLI with no dependencies. Every job
  gets a grade from 0 to 100, and the next pick uses those grades.

New here? Run the [tutorial](docs/tutorial.md) on a tiny example project
that ships with ACC.

## How it works

```text
request
  │
  ├─ small? ──────────────────────────────┐
  ▼                                       │
spec: questions (≤5) → spec.md, tasks.md  │
  │   commit on acc/<slug>                │
  ▼                                       ▼
lanes: one branch each, acc/<slug>-<lane>
  │
  ▼
pick a model per job (ACC pick) ── your approval (only with a spec)
  │
  ▼
run lanes in parallel ── watch: Orca, or logs + git
  │
  ▼
review every commit, run the check, ACC record a score
  │   fail → retry with another model
  ▼
merge lanes into acc/<slug> → tests → push → pull request (you merge)
```

1. **Spec first.** For a request that is not small, the coordinator asks the
   questions whose answer changes what gets built (at most 5, in one message),
   then writes `specs/<NNN>-<slug>/spec.md` and `tasks.md` (and `plan.md` when
   the design is not obvious) and commits them on the feature branch. Small =
   one job, one file area, no open question: that skips the spec.
2. **Lanes.** Jobs that touch the same files or depend on each other share a
   lane and run in order; tasks marked `[P]` with no shared files get their own
   lanes and run in parallel. At most 4 lanes.
3. **Pick.** `ACC pick --kind <kind>` per job (see [the scoreboard](#the-scoreboard)).
4. **One stop.** With a spec, the coordinator shows the spec summary, the
   lanes and the models, and waits for your approval. That is the only stop;
   after it, it runs to the end.
5. **Run and watch.** Workers start in their worktrees; the coordinator waits
   in short checks (under 2 minutes each) until every lane is done.
6. **Review and score.** It reads every commit, runs the job's check itself
   and records a grade with `ACC record`, pass or fail. A failed job goes to
   another model with the findings.
7. **Deliver.** Lanes are merged into `acc/<slug>`, the project's tests run,
   the branch is pushed and one pull request is opened. You merge it. Workers
   never push, merge or open pull requests.

## Two modes

The coordinator chooses at the start of every run:

| | Orca mode | Plain mode |
| --- | --- | --- |
| When | `orca status --json` shows a running Orca | otherwise, or when you ask for it |
| Where you run the coordinator | an Orca terminal of the project | any terminal, VS Code, any CLI |
| Lanes | Orca worktrees | `git worktree add ../<repo>-acc-<lane>` |
| Workers | `orca orchestration worker-start` | `claude -p`, `codex exec` or `opencode run` in the background |
| Watching | Orca panel, `check --wait`, worker questions | process alive, new commits, changed files, `../<repo>-acc-logs/*.log` |

Plain mode has no question channel: a worker that needs an answer stops or
guesses, so specs must be self-contained. A job with 30 minutes of no commit,
no file change and no log growth is killed and counts as a failed attempt.

## The skills kit

The installer also copies five skills next to `crew-chief`, and every job spec
tells workers to use them:

| Skill | From | Used for |
| --- | --- | --- |
| `test-driven-development` | superpowers | code changes: test first |
| `systematic-debugging` | superpowers | bugs: find the cause before fixing |
| `verification-before-completion` | superpowers | run the check before saying "done" (the coordinator too, before the PR) |
| `ponytail` | ponytail | the smallest complete change |
| `ponytail-review` | ponytail | a quality review of a change |

Each copy carries an `.installed-by-agent-crew-chief` marker file. A folder
with the same name but no marker is yours and is never touched. Skip a kit with
`--no-superpowers` or `--no-ponytail` (copies already there are left as they
are). A worker without the skills reads the rule as plain guidance.

## The scoreboard

Grades live in `~/.crew-chief/ledger.jsonl` (or `$CREW_CHIEF_HOME`), outside
every repository: nothing shows up in your pull requests, and a model graded in
one project helps the pick in the next.

```text
## code

| model | score | n | pass | minutes | cost | last |
| --- | --- | --- | --- | --- | --- | --- |
| opencode:<model> | 81.4 | 6 | 83% | 9.5 | free | 2026-10-30 |
| codex:<model> (high) | 78.2 | 3 | 100% | 14.0 | included | 2026-10-31 |
```

- **score**: a weighted mean of the grades. A grade weighs half after 30 days,
  a quarter after 60, so a model that changed upstream recovers; one
  imaginary grade of 50 is always added, so a single lucky 100 shows as 75.
- **n**: graded jobs. **pass**: share of jobs with outcome `pass`.
  **minutes**: mean minutes per job. **last**: date of the latest grade.
- **cost**: the tier of the last job: `free`, `included` (a plan you already
  pay for), `cheap`, `paid`. A model with no recorded cost counts as the dearest.

**How `pick` chooses:** among the models with score ≥ 70 over ≥ 2 jobs, the
cheapest tier wins (the higher score inside a tier). If none qualifies yet, it
names the best score so far (a tie goes to the cheaper) and says "not proven
yet". `--max-cost` drops every model above that tier first.

## Using it

Talk to your agent; start with "ACC". Example prompts:

- Small fix (skips the spec): *"ACC, fix the off-by-one in `paginate()` in
  `src/api/list.py` and add a test."*
- Feature (spec, one approval stop): *"ACC, add CSV export to the reports page:
  a button, an endpoint and tests."*
- Research or review: *"ACC, review the auth module for security issues and
  compare two models on it."* / *"ACC, research how we could cache the search
  results; no code changes."*

Steering a run:

- **Skip the spec:** say "run directly" (or "sem spec").
- **Force plain mode:** say "without Orca" (or "plain mode").
- **Limit cost:** say "free models only" or "nothing above included"; the
  coordinator passes `--max-cost free|included|cheap|paid` to `ACC pick`.

The four commands. `ACC` is `python -I .agents/skills/crew-chief/scripts/crew-chief.py`
(or `.claude/skills/...`) in a project where the skill is installed:

```text
ACC pick --kind code [--max-cost free|included|cheap|paid] [--json]
ACC record --kind code --agent opencode --model <model> --cost free --outcome pass --score 85 --minutes 12 [--effort <e>] [--lane <lane>] [--job "<title>"] [--note "<why>"]
ACC score [--kind code] [--project <name>] [--json]
ACC import /path/to/project     # grades kept by ACC 0.6 in docs/crew-chief/jobs
```

Kinds: `code`, `tests`, `refactor`, `design`, `docs`, `research`,
`diagnosis`, `data`, `review`.

## Install

You need Python 3.11+, git, and at least one agent CLI (`claude`, `codex` or
`opencode`). [Orca](https://github.com/stablyai/orca) is optional.

```bash
git clone --branch v0.8.0 https://github.com/pauloroos/agent-crew-chief
python agent-crew-chief/install.py /path/to/project
```

No git? Download `agent-crew-chief-v0.8.0.zip` from the
[releases](https://github.com/pauloroos/agent-crew-chief/releases/tag/v0.8.0),
unzip it and run `python install.py /path/to/project` from the unzipped folder.

`install.py` copies the skill and the kit into `.claude/skills/`,
`.agents/skills/` and `.gemini/skills/`, and adds a short snippet between
`<!-- BEGIN:agent-crew-chief -->` and `<!-- END:agent-crew-chief -->` to
`AGENTS.md` and `CLAUDE.md`, to `GEMINI.md` (only if it exists) and to
`.cursor/rules/agent-crew-chief.mdc` (only if `.cursor/` exists). A
`CLAUDE.md` that only contains `@AGENTS.md` is left alone; `--force-snippet`
puts back a snippet whose markers you removed. Commit the installed files, so
every lane worktree has the skills too.

For every repository on the machine at once (into `~/.claude`, `~/.agents`,
`~/.gemini`; same `--no-superpowers` / `--no-ponytail` flags, plus `--dry-run`):

```bash
python -I agent-crew-chief/skill/crew-chief/scripts/install_machine_wide.py
```

Then tell your agent "ACC, do X".

## Update

```bash
git -C agent-crew-chief fetch --tags
git -C agent-crew-chief checkout v0.8.0
python agent-crew-chief/install.py /path/to/project
```

The installer replaces its own copies and its snippet; files that differ only
in line endings are kept. The ledger in `~/.crew-chief/` is never touched.

## Coming from 0.6 or 0.7

- **From 0.7:** run the installer again. You get the spec stage, the kit and
  plain mode; the scoreboard is the same file.
- **From 0.6:** the maestro, dispatch engine, slots and per-project
  preferences are gone for good. Kit copies 0.6 installed carry the same
  marker and are updated in place. Bring the old grades into the scoreboard
  once with `ACC import <project>`; then the old `docs/crew-chief/` folder can
  be deleted.

## Credits

The kit is vendored under its own MIT licenses (versions in each `VERSION` file):

- [superpowers](https://github.com/obra/superpowers) v7.0.1 by Jesse Vincent:
  see [vendor/superpowers/LICENSE](vendor/superpowers/LICENSE).
- [ponytail](https://github.com/DietrichGebert/ponytail) v5.3.2 by
  DietrichGebert: see [vendor/ponytail/LICENSE](vendor/ponytail/LICENSE).

## License

MIT. See [LICENSE](LICENSE) and [CHANGELOG.md](CHANGELOG.md).
