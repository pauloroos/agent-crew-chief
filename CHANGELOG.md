# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/). Install a tagged version
(`git clone --branch vX.Y.Z ...`), not `main`.

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
