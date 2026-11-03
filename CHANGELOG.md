# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Install a tagged version
(`git clone --branch vX.Y.Z ...`), not `main`.

Versions up to 0.4.4 were developed in the repository now archived as
[`agent-crew-chief-legacy`](https://github.com/pauloroos/agent-crew-chief-legacy),
which keeps their full history, releases and changelog.

## [0.13.0] - 2026-11-03

### Added
- **`machine` on every grade.** `ACC record` writes `$CREW_CHIEF_MACHINE`,
  else the host name, so a synced ledger tells PC, notebook and server apart.
  Older grades have no `machine` and still count.
- **`ACC sync` carries `jobs/<machine>/*.jsonl`**, one summary line per job
  written by a launcher, so grades and job summaries live in one private
  repository. `.gitignore` and `.gitattributes` from an older setup get the
  `jobs/` lines on the next sync; user edits to them are kept.

### Changed
- **Secret filter on the ledger.** Every text field of a grade (note, job,
  lane, model...) passes through `ledger.redact()` before it is written:
  GitHub, OpenAI-style, Slack and AWS tokens, JWTs, PEM private keys,
  `password=...` pairs and long runs mixing letters and digits become
  `[redacted]`.

## [0.12.1] - 2026-11-02

### Changed
- **The race engineer's terminal has no job segment.** One engineer covers a
  whole lane, so its name is `acc-<slug>-<lane>-engineer[-<n>]`; drivers and
  scrutineers keep `acc-<slug>-<lane>-<job>-<role>[-<n>]`.
  `terminal_name(slug, lane, None, "engineer")` builds it; passing a job for
  the engineer, or no job for a driver or scrutineer, raises `ValueError`.
  Spec 0120 carries the amendment.
- **Race engineers' `coordinate` grades are recorded under the engineer's own
  `agent:model`** (`ACC pick --kind coordinate`). Since 0.12.0 the engineer
  is a separate Orca worker, not the crew chief, so the 0.11.0 rule of
  recording it as the crew chief no longer applies.
- **Scrutineers started with `orca terminal create`** send no `worker_done`:
  `pit-wall.md` now says the engineer waits for
  `../<repo>-acc-logs/<name>.md` to end with its `VERDICT:` line, as in
  background mode.
- SKILL.md step 3 now names the Chat UI case (no terminal handle: skip the
  rename), as 0.12.0 already described.
- `templates/race-engineer.md` points engineers at `crew_chief.terminals`
  for driver and scrutineer names.

### Fixed
- Tutorial: the race engineer, not the crew chief, makes its own pass over
  the risky path and runs the pit stop.
- Tutorial PT brought back in step with EN (naming convention, extra
  sentences).
- READMEs and tutorials: lane reports come back through `worker_done` in Orca
  mode or the engineer's own log in background mode; the race engineer's
  name is now given in "Following a run"; "plain mode" is now
  "background mode" in the "Coming from 0.7" section.
- Tutorial: "Two related rules" listed three; it now says three.

### Notes
- tmux was dropped before the 0.12.0 merge; the PR #15 description now says
  so.

## [0.12.0] - 2026-11-02

### Added
- **Visible terminals for every crew member.** In Orca mode, race engineers,
  drivers and scrutineers start as Orca workers or terminals with a standard
  title (`acc-<slug>-<lane>-<job>-<role>[-<n>]`), so every agent is reachable
  from the Orca UI or CLI. Without Orca, behaviour falls back to background
  process with log and PID, unchanged from 0.11.x.
- **`crew_chief.terminals` module.** `terminal_name(slug, lane, job, role,
  attempt)` builds the standard name including the job slug for uniqueness
  across parallel jobs and scrutineers. `log_file(logs_dir, name)` returns
  the standard log path used in background mode (Orca-mode workers produce
  no `.log` files).
- **Lane worktrees are children of the crew chief worktree.** `orca worktree
  create` passes `--parent-worktree <crew chief worktree selector>`, so Orca
  shows every lane under the run.

### Changed
- **SKILL.md step 3** now requires starting the race engineer as an Orca
  worker (`orca orchestration worker-start`) in the existing lane worktree,
  never as a subagent of the crew chief, regardless of harness. The worktree
  already exists, so `--name` is not passed; the standard title is applied
  with `orca terminal rename` afterwards (skipped when the Chat UI does not
  expose a terminal handle). Drivers and scrutineers follow the same rule:
  supervised worker when Orca knows the agent, otherwise `orca terminal
  create` with the standard title.
- **pit-wall.md "Starting and watching drivers"** documents two modes (Orca
  worker/terminal and background) with real commands and the standard naming
  convention.
- **templates/race-engineer.md** points engineers at the new pit-wall
  section and the `terminals` helper for consistent naming.

## [0.11.1] - 2026-11-01

### Changed
- Docs brought in step with 0.10/0.11: following a run and its logs, the pace
  table, the current Pit Wall rules, role names (crew chief, race engineers,
  drivers, scrutineers), and the tutorial with engineers, scrutineers, a pit
  stop and sync.
- AGENTS.md: docs rule, every version keeps README, tutorial, skill and
  changelog in step.
- `pit-wall.md` edge cases from the engineers' reports: job and pit-stop file
  names, partly done work, review after a pit stop, a stalled scrutineer,
  scrutineer choice at pace push, docs jobs, cross-lane test breaks.

## [0.11.0] - 2026-11-01

### Added
- **`ACC merge <file>`**: merges a ledger file copied from another machine; new
  grades are appended, duplicates skipped, rerun adds 0.
- **`ACC sync --init <url>`** and **`ACC sync`**: keeps `ledger.jsonl` in sync
  across machines through a private git repository of your own. Uses git's
  union merge so concurrent appends never conflict; uses the git access you
  already have, no tokens asked for, stored or created. Optional: without
  setup, nothing changes.
- **`ACC score` collapses untried models.** With a `models.toml`, rows with
  `n` 0 are replaced by one line per kind: `not tried yet (N): <executor>, ...`.
  JSON output is unchanged.
- **`ACC models` hints at graded-but-not-listed executors.** After the table it
  prints `graded but not listed: <agent:model> (<kind> <score>, n <n>)` for
  executors that have grades but are not in `models.toml`, so the user can add
  them.

### Changed
- **Race engineers are recorded as the crew chief.** Engineers are by default
  the crew chief's own subagents; their `coordinate` grades are recorded under
  the crew chief's own `agent:model`. `ACC pick --kind coordinate` is used only
  to choose an engineer that is not the crew chief itself.
- **SKILL.md:** when `ACC sync` is set up, the crew chief runs it at the start
  of a request and after the last grade. `ACC merge` and `ACC sync` are named
  in the Rules section.
- **Ledger dedupe.** `ledger.read()` returns each canonical entry once, which
  makes duplicates harmless everywhere. Existing ledgers work unchanged.

## [0.10.0] - 2026-11-01

### Added
- **The Pit Wall method** (`skill/crew-chief/pit-wall.md`): Stint, Fresh tyres,
  Relay, Scrutineering and pit stop patterns; risk per job (`low`/`normal`/high`);
  pace per request (`eco`/`race`/`push`) with implied `--max-cost`.
- **The team**: crew chief dispatches a race engineer per lane; new ledger kind
  `coordinate` grades engineers. Brief: `templates/race-engineer.md`.
- **Scrutineer template** (`skill/crew-chief/templates/scrutineer.md`): read-only
  prompt for independent review of high-risk jobs, findings as
  `severity | file:line | failing scenario`, ending in `VERDICT: pass|fail`.
- **`ACC pick --exclude <agent:model>`** (repeatable): drops those executors
  from the candidates, both with and without `models.toml`.

### Changed
- Retries now start on Fresh tyres: the lane worktree is reset to the job's
  start commit (`git reset --hard <start>` + `git clean -fd`) before every
  new attempt, so the next worker never inherits a failed one's edits.
- SKILL.md steps 1, 2 and 5 point to `pit-wall.md` for risk, pace, Relay,
  Fresh tyres and Scrutineering; step 0.5 and the report show risk, pace,
  attempts and scrutineer scores.

## [0.9.1] - 2026-11-01

### Fixed
- Output (and the files ACC reads and writes) in UTF-8 on Windows, where
  `python -I` ignored PYTHONUTF8 and printed accented notes as broken
  characters.

## [0.9.0] - 2026-11-01

You declare the models you have; ACC no longer guesses from the scoreboard
alone.

### Added
- **`models.toml`**, global (`~/.crew-chief/models.toml`) and per project
  (`.crew-chief/models.toml` at the repository root, shared by every
  worktree). Names only, never keys. The project file can switch a global
  model off (`enabled = false`) or ignore the global list (`inherit = false`).
- **`ACC models [--json]`**: the effective list, where each model came from,
  its cost, kinds, scores and notes.
- `examples/models.toml`, a commented file to copy.

### Changed
- `ACC pick` with a list picks only among the listed models that take the
  kind, using the declared cost; an untried model of the cheapest tier can be
  picked ("not tried yet"). `ACC score` also shows listed models with no
  grade. A broken file stops these commands with `<path>: <problem>`.
  Without a file nothing changes.
- SKILL.md step 2: run `ACC models` first and pick among the listed models.
- SKILL.md step 6: plain mode also deletes `../<repo>-acc-logs/` once no
  worker runs.

## [0.8.0] - 2026-10-31

Brings back what 0.7 cut without asking: the owner had decided on 2026-10-29
to keep spec-driven work with the interview and the skills kit. And not
everyone runs Orca, so lanes now also run on plain git worktrees.

### Added
- **Spec stage.** A request that is not small gets an interview (at most 5
  questions, in one message), then `specs/<NNN>-<slug>/spec.md` and
  `tasks.md` (plus `plan.md` when needed), committed on the feature branch
  before any lane. Lanes come from the `[P]` tasks. Showing the spec and the
  lanes is the one stop of the run; "run directly" / "sem spec" skips it.
- **Skills kit in both installers.** superpowers (test-driven-development,
  systematic-debugging, verification-before-completion) and ponytail
  (ponytail, ponytail-review), with the `.installed-by-agent-crew-chief`
  marker; `--no-superpowers` and `--no-ponytail`. Job specs carry a fixed
  "Working rules" block that tells workers to use them.
- **Plain mode, without Orca.** Lanes on `git worktree add`, workers started
  in the background with `claude -p`, `codex exec` or `opencode run`, watched
  through commits, changed files and logs, killed as a process tree after 30
  idle minutes.
- **Tutorial and example project:** `docs/tutorial.md` (+ pt-BR) on
  `examples/hello-crew`, with a "did it work?" checklist of commands and
  expected outputs.

### Changed
- SKILL.md grows to about 180 lines (spec first, working rules, plain mode).
- README rewritten around the 0.8 flow, the two modes, the kit and how to
  read the scoreboard, with a "Using it" section.

## [0.7.1] - 2026-10-31

### Fixed
- **Waiting on workers fits every coordinator CLI.** SKILL.md waited 15
  minutes in one `check --wait`, longer than Claude Code (10 minutes) or
  OpenCode (2 minutes) let a command run; it now waits under 2 minutes and
  waits again.

### Changed
- SKILL.md says any CLI can coordinate, from an Orca terminal of the project,
  and to stop when Orca is not running.

## [0.7.0] - 2026-10-31

A restart around the one thing ACC adds: a score per model. Two real runs on
Contratada Fácil showed the 0.6 machinery cost more than it delivered (46% of
attempts ended `ok`, many lost to ACC itself), so the running and monitoring
move to Orca and ACC keeps the method and the scoreboard.

### Changed
- **Lanes instead of a worktree per attempt.** The coordinator groups jobs
  into lanes, one branch and one Orca worktree each; lanes run in parallel
  and a lane's jobs run in order. Orca starts and watches the workers
  (`orca orchestration worker-start`, `check --wait`).
- **The CLI is four commands:** `pick`, `record`, `score` and `import`.
- **Scores live outside the repository**, in `~/.crew-chief/ledger.jsonl`,
  shared by every project on the machine: no records in pull requests. A score
  is a mean of grades in which recent jobs weigh more, with one imaginary 50
  so a single 100 is not a perfect score.
- **SKILL.md is about 100 lines**, and the project snippet sends the agent to
  the skill only when the user asks for ACC.
- **`install.py` installs the skill and the snippet, nothing else.**

### Added
- **`ACC import <project>`** copies the grades an ACC 0.6 project kept in
  `docs/crew-chief/jobs` into the ledger, once.

### Removed
- The maestro, dispatch and its sidecar, executor slots, the worktree per
  attempt, git delivery, the interview, spec-driven mode, preferences,
  discovery, models-seen, economy and spend, changelog and decisions files,
  the superpowers and Ponytail kits, the `acc` command wrapper, the tutorials
  and the example project.

## [0.6.3] - 2026-10-30

### Fixed
- **Updating on Windows no longer marks every installed file as modified.**
  A checkout with `core.autocrlf` keeps CRLF copies; the installer used to
  delete and rewrite them with LF, so git listed the whole skill and the kits
  as changed with no diff. It now rewrites only files whose text changed and
  removes only files the new version dropped.

## [0.6.2] - 2026-10-30

Delivery with checked quality, from the review of the first real maestro runs.

### Added
- **A review closes every front.** Once a front's job is closed, the same
  `tool:model` reviews it in a clean session and a checkout of its own: a
  checklist of each requested item (delivered, partial or missing), the test
  results and the problems, ending in a verdict and a score. It is written to
  `review-N.md` in the coordinator's folder and posted on the PR. A failed
  review sends the coordinator back to fix it on the same PR, up to three
  reviews; `status` shows the verdicts and the last score.
- **A completed front cleans up after itself.** Its worktree, its attempts'
  checkouts and their `crew/` branches go; its folder keeps a copy of the job
  record, so `status` keeps its numbers. `clean` does the same for completed
  fronts left from before and, named by ID, for a stopped or failed one.

### Fixed
- Toolchain caches in the home folder (`.cargo`, `.rustup`, `.yarn`,
  `.pnpm-store`, `.gradle`, `.m2`, `.nuget`) no longer count as an attempt
  writing outside its worktree: every Rust build was thrown away.
- An OpenCode Zen free model that hits the shared free limit pauses every Zen
  free model, instead of the next one hitting the same limit.
- The coordinator's runner no longer opens a terminal window on Windows for
  each git or gh it runs, and worktrees with deep paths (`node_modules`) are
  removed on Windows.

## [0.6.1] - 2026-10-30

Fixes from the first real maestro runs (Contratada Fácil and Project Vard).

### Fixed
- `status` keeps a front's economy after its worktree is removed: it reads
  the job record merged into the repository.
- `status` prints the whole last error on one line instead of its last line
  (which could be just `)`).
- The skill tells the maestro to merge a front's PR only once `status` shows
  it `completed`; merging earlier left the coordinator's tokens out of the
  job record.

## [0.6.0] - 2026-10-29

A lighter ACC: the maestro, its coordinators and their numbers, without the
machinery around them.

### Added
- **Any tool as coordinator, with automatic fallback.** `[team].coordinators`
  takes `tool:model` entries of any configured tool (claude, codex, opencode,
  antigravity, generic). `acc launch --request-file F` starts the first in its
  own worktree and branch; on a quota or a failure the next one takes over
  the same worktree and job, with no command from you.
- **`acc status`** per coordinator: state, current `tool:model`, rounds and
  switches, elapsed time, the coordinator's tokens and its job's economy
  (executor tokens by tier, attempts per piece, mean score). The
  coordinator's tokens are booked in the job's `usage`, so `economy` and the
  scoreboard count them.
- `acc resume ID [--answer TEXT]` runs a held, asking or failed coordinator
  again; `acc stop ID` stops it. A launched coordinator asks by writing
  `question.md`. `coordinator_timeout_s` (default 14400) bounds one round.
- `acc interview --job ID --answer KEY=VALUE` answers the interview from the
  CLI.

### Removed
- The central layer (supervisor, registry, terminal screen, inbox and
  messages, attach, pause, recover, keys, preflight): `acc central`,
  `acc message`, `acc attach`, `acc pause`, `acc recover`.
- The MCP server and the end-of-job webhooks (`acc mcp`, `notify_*`).
- The update check (`check_updates`).
- The rtk download in the installer (`--with-rtk`); briefs still use an rtk
  already on PATH.
- The old central preferences (`coordinator_default`, `coordinator_fallback`,
  `stall_minutes`, `on_coordinator_quota`, `central_*`), which are now
  ignored.
- `--projects-dir` is accepted only by `acc interview`; the global
  `projects_dir` preference is ignored.
- The usage role `central` (`usage add --role coordinator` only).
- The merge-gate proposal docs.

### Fixed
- The maestro branch is `maestro/<front>-<id>`, so `job new` inside the
  coordinator's worktree keeps it instead of switching away.
- A multi-line prompt is no longer cut at the first line by Windows `.cmd`
  tools.
- A coordinator whose CLI is missing falls back to the next one of the list.
- A blocked delivery stops the coordinator instead of looping round after
  round.
- The coordinator's tokens are committed with the job record.
- `acc stop` no longer races the runner or overwrites a `completed`
  coordinator.
- Held fronts stop the clock.
- `acc status` shows the job's economy alerts.
- The Antigravity effort flag no longer conflicts with its model.
- `acc interview` status and end mode follow the launched coordinator, and
  the interview no longer switches your checkout to an unused job branch.
- The PR body no longer claims the tests passed unconditionally.

## [0.5.0] - 2026-10-29

The first release of this repository: the maestro -> coordinator ->
subagent hierarchy, seeded from 0.4.4 plus the base kit, without the parts
that did not serve it.

### Added
- **Maestro mode** in the skill: the agent you talk to splits a goal into
  independent fronts, launches one coordinator per front with `acc launch`,
  watches them with `acc central --json`, answers or relays their questions,
  holds or relaunches a coordinator that hit a quota, and reports one PR per
  front. Tutorial: `docs/tutorials/05-maestro.md` (now `04-maestro.md`).
- **Quota hold:** a coordinator whose CLI stops on a quota or rate limit
  moves to `quota` instead of `failed`; `acc resume` continues it from the
  same records, or the maestro relaunches the front with another `--cli`.
- **`acc central --json`** prints one machine-readable snapshot of every
  coordinator, its questions and totals.
- **Base kit:** ponytail (concise prompts), rtk-oss (optional command prefix)
  and superpowers skills (test-driven-development, systematic-debugging,
  verification-before-completion) are delivered through the prompt, never
  hooks or plugins, to coordinators, executors and reviewers. The installer
  sets up ponytail and superpowers by default (`--no-ponytail`,
  `--no-superpowers` skip them); rtk-oss v1.1.0 (Apache-2.0) is downloaded
  only with `--with-rtk`, pinned and verified against the manifest and
  checksums.txt. RTK preference is `auto`/`on`/`off`; superpowers preference
  and `--superpowers` flag on `job new`, `piece add`, `spec import` and
  `dispatch`. Tool keys `superpowers_flag` and `reseller`.

### Removed
- The browser view, live tail and history (`acc central --web`); `acc
  central` is the terminal screen.
- The Orca backend (`--backend orca`).
- Automatic coordinator retry and rotation through `[team].coordinators`,
  which the public launch path never used; replaced by the quota hold.

### Fixed
- The base kit reaches the coordinator's real round prompt, not only a
  command builder the launch path did not call.
- `prepare_job_branch` never switches a checkout that sits on a feature
  branch; that branch becomes the job branch.
- Kit tests no longer depend on whether rtk is installed on the host.
