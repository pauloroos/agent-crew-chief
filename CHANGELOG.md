# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Install a tagged version
(`git clone --branch vX.Y.Z ...`), not `main`.

Versions up to 0.4.4 were developed in the repository now archived as
[`agent-crew-chief-legacy`](https://github.com/pauloroos/agent-crew-chief-legacy),
which keeps their full history, releases and changelog.

## [0.5.0] - 2026-10-29

The first release of this repository: the maestro -> coordinator ->
subagent hierarchy, seeded from 0.4.4 plus the base kit, without the parts
that did not serve it.

### Added
- **Maestro mode** in the skill: the agent you talk to splits a goal into
  independent fronts, launches one coordinator per front with `acc launch`,
  watches them with `acc central --json`, answers or relays their questions,
  holds or relaunches a coordinator that hit a quota, and reports one PR per
  front. Tutorial: `docs/tutorials/05-maestro.md`.
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
