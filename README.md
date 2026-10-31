<div align="center">

<img src="assets/logo.svg" alt="agent-crew-chief logo" width="128" height="128">

# agent-crew-chief

**Parallel agents through Orca, and a score for every model.**

You give your agent a request. It splits the work into lanes, one branch each, runs the lanes at the same time as [Orca](https://github.com/stablyai/orca) workers, watches them, reviews and scores every job, and delivers one pull request. The scores tell it which model to pick next time.

[![CI](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/pauloroos/agent-crew-chief?color=f59e0b)](https://github.com/pauloroos/agent-crew-chief/releases/latest)
[![Python](https://img.shields.io/badge/python-3.11%2B-0f766e)](#install)
[![Dependencies](https://img.shields.io/badge/dependencies-none-0f766e)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-1e1b4b)](LICENSE)

**English** · [Português](README.pt-BR.md)

</div>

---

## How it works

1. **Lanes.** The coordinator (the agent you talk to) splits the request into
   jobs and groups them into lanes. Jobs that touch the same files share a lane
   and run in order; lanes that do not overlap run in parallel. Each lane is
   one branch in one Orca worktree.
2. **Pick.** For each job it asks `ACC pick --kind <kind>`: the cheapest model
   that scored well at least twice for that kind of work.
3. **Run and watch.** Orca starts the workers (`orca orchestration
   worker-start`) and the coordinator waits on their reports (`check --wait`).
4. **Review and score.** The coordinator reads every commit, runs the job's
   check and records a score from 0 to 100 with `ACC record`. A failed job goes
   to another model with the findings.
5. **Deliver.** The lanes are merged into the feature branch, tested, pushed
   and opened as one pull request. You merge it.

Orca does the running and the monitoring. ACC is the method (the skill) plus a
small scoreboard (the CLI), with no dependencies.

## The scoreboard

Scores live in `~/.crew-chief/ledger.jsonl`, outside every repository: nothing
shows up in your pull requests, and a model graded in one project helps the pick
in the next. Each score is a mean of the job grades in which recent jobs weigh
more (half after 30 days) and one imaginary grade of 50 keeps a single lucky
100 from looking perfect.

```text
ACC pick --kind code [--max-cost free|included|cheap|paid]
ACC record --kind code --agent opencode --model <model> --cost free --outcome pass --score 85 --minutes 12
ACC score [--kind code] [--project <name>]
ACC import /path/to/project     # grades kept by ACC 0.6 in docs/crew-chief/jobs
```

`ACC` is `python -I .agents/skills/crew-chief/scripts/crew-chief.py` in a
project where the skill is installed.

## Install

You need [Orca](https://github.com/stablyai/orca) running and Python 3.11+.

```bash
git clone --branch v0.7.1 https://github.com/pauloroos/agent-crew-chief
python agent-crew-chief/install.py /path/to/project
```

No git? Download `agent-crew-chief-v0.7.1.zip` from the
[latest release](https://github.com/pauloroos/agent-crew-chief/releases/latest),
unzip it and run `python install.py /path/to/project` from the unzipped folder.

`install.py` copies the skill into `.claude/skills/crew-chief/`,
`.agents/skills/crew-chief/` and `.gemini/skills/crew-chief/`, and adds a short
snippet between `<!-- BEGIN:agent-crew-chief -->` and
`<!-- END:agent-crew-chief -->` to `AGENTS.md` and `CLAUDE.md`, to `GEMINI.md`
(only if it exists) and to `.cursor/rules/agent-crew-chief.mdc` (only if
`.cursor/` exists). A `CLAUDE.md` that only contains `@AGENTS.md` is left
alone; `--force-snippet` puts back a snippet whose markers you removed.

For every repository on the machine at once:

```bash
python -I agent-crew-chief/skill/crew-chief/scripts/install_machine_wide.py
```

Then tell your agent "ACC, do X".

## Coming from 0.6

0.7 drops the maestro, the dispatch engine, the worktree per attempt, the
interview, spec-driven mode, the kits and the per-project preferences: Orca
runs the agents now. Your old `docs/crew-chief/` folder is left as it was;
`ACC import <project>` brings its grades into the new scoreboard, and you can
delete the folder afterwards.

## License

MIT. See [LICENSE](LICENSE) and [CHANGELOG.md](CHANGELOG.md).
