# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Install a tagged version
(`git clone --branch vX.Y.Z ...`), not `main`.

Versions up to 0.4.4 were developed in the repository now archived as
[`agent-crew-chief-legacy`](https://github.com/pauloroos/agent-crew-chief-legacy),
which keeps their full history, releases and changelog.

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
