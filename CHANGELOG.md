# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Install a tagged version
(`git clone --branch vX.Y.Z ...`), not `main`.

## [Unreleased]

### Added
- **Model changes:** `docs/crew-chief/models-seen.json` records every executor discovery has seen (`cost`, `trains`, `first_seen`, `last_seen`, `present`, plus a top-level `last_check`). `discover` always updates it and prints, after the list, the free and paid models that are new, gone or changed cost (`new free: ...`, `gone: ...`, `now paid: ...`), or `models: no changes since <when>`. The first run only records a baseline, and a skipped tool never marks its models as gone. `--json` adds a `changes` object.
- `job new` runs the same check every time and prints it on stderr; `pick` and `dispatch` run it on stderr only when the last check is older than `model_check_hours`. A failing check never fails the command.
- `model_check_hours` preference (default `24`; `0` means every time).
- **Attempt metrics:** every attempt records `steps` (agent turns), `tokens_in`, `tokens_out`, `tokens_total` and `tokens_estimated` next to `duration_s`. Numbers come from the tool's own output (OpenCode now runs with `--format json`; Claude Code and Antigravity `num_turns`/`usage`; Codex's token total); otherwise tokens are estimated as characters / 4 and flagged. `dispatch` prints them, the job `.md` shows them per attempt, and `scoreboard.md` gains `attempts/pass`, `mean steps` and `mean tokens` columns.
- `quota_cooldown_min` preference (default `60`): an executor whose attempt ended in `quota` within that window, in any job of the project, sits out of `pick` together with its quota family; the reason ends with `skipped for quota: <ids>`. The family drops a trailing effort suffix (`-minimal|-low|-medium|-high|-xhigh|-max`) from the model; on Antigravity, which shares quota by vendor, it is `antigravity:<model up to the first '-'>` (`antigravity:claude`, `antigravity:gemini`, `antigravity:gpt`); on other tools it is `<tool>:<model>`.
- `ignore_home_entries` preference: extra home entries the outside-write check ignores, on top of the built-in list of agent CLI folders.
- `no_change` attempt status: an exec that exits with code 0, without timeout or outside write, but leaves the worktree untouched (no changed or new file apart from the coordinator's brief and output files, and no commit of its own) is `no_change` instead of `ok`. It counts as a failure everywhere: `dispatch` exits 1, `pick` leaves that executor out of the piece, it counts toward the 2 free failures that escalate, and `accept` refuses it. Reviews are unaffected.

### Fixed
- The executor prompt names the brief file and the worktree by absolute path (`Read the file <worktree>/.crew-chief-brief.md and carry out exactly what it asks. Work only inside <worktree>.`). A relative "in the current directory" was lost on tools whose file search hides dotfiles (OpenCode's glob), and the model gave up.
- Parallel `dispatch` (and `pick`, `piece add`, `correction`, `grade`) no longer lose each other's writes: every change to a job goes through a lock file next to the record and re-reads the job before saving. Parallel attempts of one piece get distinct attempt numbers and worktrees. Windows sharing violations on the record, and on the removal of the lock file, are retried.
- A new attempt skips numbers whose worktree or branch already exists (left by an attempt whose record was lost) instead of failing with `worktree path already exists`. An orphan folder at the worktree path (a removal that stopped half way on Windows) is pruned and removed before the worktree is created. A worktree that cannot be created, or is broken after the run, is recorded as a `failed` attempt with a note instead of a bare `error:` that recorded nothing.
- `outside_write` no longer fires for the project's other worktrees in `<project>.crew/` (parallel attempts) or for the config folders agent CLIs write in the home directory (`.claude`, `.claude.json`, `.codex`, `.gemini`, `.antigravity`, `.config`, `.local`, `.cache`, ...). Unknown entries and writes to the main repo are still caught.
- Parallel `dispatch` of one project no longer flags `outside_write` when another crew-chief process updates the job record, its lock file, `models-seen.json` or `scoreboard.md` in the main repo during the attempt. Creating a worktree no longer rewrites the repository's shared `info/exclude` once it lists the brief and output files (and writes it atomically when it must), which made a parallel `git status` die on Windows with `cannot use .git/info/exclude as an exclude file`; the containment snapshot also retries that `git status` for a moment.
- `pick` no longer sends every unrated piece to the same executor: ties go to the executor least used in the job, then to a different tool.

## [0.1.0] - 2026-10-17

First public release.

### Added
- `crew-chief` Agent Skill (`SKILL.md`). Any coordinator that reads Agent Skills (Claude Code, Codex, OpenCode; Cursor and Antigravity expected) uses the same rules: evaluate, plan, pick, dispatch, review, grade, integrate.
- Stdlib-only Python CLI (`crew-chief.py`) with these commands: `discover`, `job new`, `piece add`, `pick`, `dispatch`, `correction`, `grade`, `accept`, `discard`, `close` and `scoreboard`.
- **Discovery:** finds models at runtime from `opencode models` and `agy models`, uses fixed lists for Codex and Claude, and supports custom CLIs declared in preferences (kinds `claude-code`, `opencode`, `codex`, `antigravity` and `generic`). Required env vars are declared by name; on Windows, missing ones are read from the user registry.
- **Free-first picking:** a per-project, per-type scoreboard with 30-day half-life decay, plus second chances (1 in 10, on low-risk work only) for unrated or low-scored executors. Escalation to strong executors happens after 2 free failures, or with `--strong`.
- **Script-computed grade:** adherence (done/missing/extra), correction count and penalties (`destructive`, `outside_write`, `false_done`, `model_mismatch`).
- **Containment:** isolated git worktrees, a brief filter that refuses secrets and sensitive paths, outside-write detection with automatic rejection, a mass-deletion warning, timeouts that kill the process tree, quota detection, and a reported-model mismatch check.
- **Job records:** one JSON file plus a rendered Markdown file per job under `docs/crew-chief/`, committed with the delivery and carrying a ready-to-paste commit message and issue comment. `scoreboard.md` is derived from the records.
- **Installer (`install.py`):** idempotent, adds marked snippets to `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and Cursor rules, and never touches existing preferences or records.
- **Preferences:** a global file (`~/.config/agent-crew-chief/preferences.toml`, or `AGENT_CREW_CHIEF_GLOBAL_PREFS`) plus a project file. Unmatched models count as paid. A `forbidden` rule at any level always wins.
- README in English and Brazilian Portuguese. CI on Ubuntu and Windows with Python 3.11 and 3.13.

[0.1.0]: https://github.com/pauloroos/agent-crew-chief/releases/tag/v0.1.0
