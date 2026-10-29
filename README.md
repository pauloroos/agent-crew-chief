<div align="center">

<img src="assets/logo.svg" alt="agent-crew-chief logo" width="128" height="128">

# agent-crew-chief

**One maestro, many coordinators, a crew of free models.**

You give your agent a goal. As the maestro it splits the goal into fronts and launches one coordinator per front, each in its own worktree. Every coordinator splits its front into pieces, hands each piece to the best *free* model you have, gets a second opinion from a different model family, grades every delivery and ends in one pull request. The scoreboard learns who to trust in each project.

[![CI](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml/badge.svg)](https://github.com/pauloroos/agent-crew-chief/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/pauloroos/agent-crew-chief?color=f59e0b)](https://github.com/pauloroos/agent-crew-chief/releases/latest)
[![Python](https://img.shields.io/badge/python-3.11%2B-0f766e)](#install)
[![Dependencies](https://img.shields.io/badge/dependencies-none-0f766e)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-1e1b4b)](LICENSE)

**English** · [Português](README.pt-BR.md)

</div>

---

## Why you'd want this

You probably have more AI firepower than you use. There are free models in OpenCode, a quota in Antigravity, a Codex subscription, maybe a third-party key or two. Yet one agent does everything and burns the expensive quota on boilerplate.

**agent-crew-chief** turns the agent you already work in (Claude Code, Codex, OpenCode, Cursor, Antigravity…) into a **crew chief**:

| Feature | What you get |
|---|---|
| 🆓 **Free first** | Free models get the work. Paid ones step in only when the free ones can't deliver, or the task is too complex to split. |
| 🔍 **Real review** | Every delivery is read by a **different model family** before you see it. The chief has the final word. |
| 🏆 **A scoreboard that learns** | Each delivery is graded on how many corrections it needed and how closely it matched the request. Picks improve with every job, per project and per kind of work. |
| 🎲 **No model left behind** | Low scorers and newcomers still get a second chance on low-risk work. Models improve, and the scoreboard notices. |
| 🧱 **Safe by construction** | Isolated git worktrees, a secret filter on every brief, writes outside the worktree auto-rejected, mass-deletion alerts, hard timeouts. |
| 📒 **Everything on record** | Each job leaves a record in your repo, with a commit message and an issue comment ready to paste. |

## Three levels

| Level | Who | What it does |
|---|---|---|
| **Maestro** | the agent you talk to (Claude Code, Codex…) | Splits your goal into independent fronts, launches one coordinator per front with `acc launch`, watches them with `acc central --json`, answers their questions and reports the PRs. Never edits code. |
| **Coordinator** | a Claude Code or Codex session in its own worktree and branch | Runs the job cycle below for its front and opens one PR. A quota limit holds it (`quota`) instead of failing it; `acc resume` continues. |
| **Executors and reviewers** | any CLI you have: OpenCode, Antigravity, Codex, Claude… | Do the typing in isolated worktrees, free models first; a different model family reviews; every delivery is graded. |

You can also skip the maestro and use one level only: a single coordinator in
your own session (tutorials 01–03) or one `acc launch` (tutorial 04).

No dependencies: one skill plus a stdlib Python CLI that any agent can drive.
Coordinators run under a small detached supervisor per coordinator; there is
no server.

New here? Follow the [tutorials](docs/tutorials/README.md): your first job in
10 minutes, spec-driven work, cost and safety,
[coordinators in their own worktrees](docs/tutorials/04-central.md) and
[the maestro](docs/tutorials/05-maestro.md), with a practice project in
[`examples/hello-crew`](examples/hello-crew).

## How it works

```mermaid
flowchart TB
    A([Your request]) --> B[Chief plans<br/>and splits]
    B --> C{pick}
    C -->|free first| D[Executor works in<br/>an isolated worktree]
    D --> E[Different model<br/>reviews]
    E -->|corrections| D
    E --> F[Chief grades]
    F --> G[(Scoreboard)]
    G -.->|smarter picks| C
    F --> H([Merged + commit<br/>and issue text])
```

1. `job new`: your request is recorded verbatim.
2. `piece add`: it is split into pieces, each with a work type and a risk level.
3. `pick`: an executor is chosen. Hands-on work (`code`, `tests`, `refactor`, `docs`, `data`) starts with free models, OpenCode first; when no free model is left, `pick` can still take an `included` one or a budget-allowed `cheap` one, and the `paid` tier needs `--strong`. Thinking work (`review`, `design`, `diagnosis`, `research`) climbs the same ladder and reaches `paid` only when needed. Within that, decayed score decides, with an occasional second chance. Ties are spread over the job, and a model that just hit its quota sits out for a while.
4. `dispatch`: the brief runs in a fresh worktree with a hard timeout. Every attempt is recorded, whatever its status, with its duration, agent steps and tokens. An attempt that exits cleanly without changing anything is `no_change`, a failure, not `ok`. Independent pieces can be dispatched in parallel.
5. `pick --review` + `dispatch --review`: a model outside the executor's quota family reviews the work.
6. `correction` + `grade`: the script computes the score, and the scoreboard is rewritten. After a correction the next `dispatch` starts from the last `ok` attempt's branch, so the brief only describes the adjustment and the diff shows only that (`--fresh` starts over from `HEAD`, `--base <n|branch>` picks the starting attempt). Every executor that worked on the piece keeps its own grade.
7. `accept` / `discard` + `close`: the work lands or is dropped, and you get the commit message and the issue comment, which list every executor with its score.

## Merge gate (planned)

A test-oriented merge gate with four gates, ACC-owned red/green evidence and suite, independent whole-PR Standards/Spec review, graph frontier/test seams and a safe landing policy is specified for v0.5.0. See [docs/process/merge-gate.md](docs/process/merge-gate.md) (English) and [docs/process/merge-gate.pt-BR.md](docs/process/merge-gate.pt-BR.md).

## Spec-driven work (optional)

For a feature with several requirements, the request can live in a spec folder
instead of a paragraph. crew-chief reads the [spec-kit](https://github.com/github/spec-kit)
layout (`specs/NNN-slug/` with `spec.md`, `plan.md` and `tasks.md`) and turns
its tasks into pieces. Tested with spec-kit 1.2.x. It does not need spec-kit
installed, never creates `.specify/`, and never edits `spec.md` or `plan.md`.
The grammar is reimplemented; no spec-kit template is copied.

The coordinator sizes the process to the request: a **trivial** edit gets no job
and no spec, a **small** request gets only a job, and a **project** gets the
cycle below.

```bash
crew-chief spec new --title "CSV export"                    # or use an existing spec-kit folder
crew-chief spec import --job <id> --spec-dir specs/001-csv-export
crew-chief spec status --job <id>                           # progress, ready and blocked tasks
crew-chief brief render --job <id> --piece 3 --out brief.md # then pick + dispatch
crew-chief brief render --job <id> --piece 3 --review --out review.md
crew-chief brief render --job <id> --converge --out converge.md
crew-chief ruling add --job <id> --text "<decision> — <why> — <cost if wrong>"
crew-chief decision add --job <id> --from-ruling 1
crew-chief usage add --job <id> --role coordinator --tokens-in 52000 --tokens-out 9000
crew-chief economy --job <id>
```

(`crew-chief` stands for `python -I .agents/skills/crew-chief/scripts/crew-chief.py`.)

- **Import.** `spec import` turns each open task of `tasks.md` into a piece
  (`ref` = the task id; `[X]` tasks are skipped) and keeps a sidecar,
  `crew.toml`, next to it. A job created by `job new` + `spec import` records the spec phase, so `dispatch` of implementation pieces is refused until the spec tasks are approved by a review from a model distinct from the coordinator (a docs/spec-review piece is still allowed). spec-kit has no place for the facts an executor
  needs, so the sidecar holds them, one `[tasks.T012]` table per task: `type`,
  `risk`, `accept` (references such as `FR-001` or `US1/AC1` into `spec.md`),
  `files` it may change, a `proof` command, an optional `expect` text, `depends`
  and `interfaces`; a top-level `global_constraints` list applies to every task.
  `import` warns about tasks without a proof, sidecar entries and pieces whose
  task id is gone, `[P]` tasks of one phase that share files, and **stale
  pieces**: when `tasks.md` is regenerated and a task's text changed, its piece
  is refused by `dispatch` and `brief render` until you fix the spec and import
  again. Each `crew.toml` entry also keeps the `task` text it was written for;
  when spec-kit renumbers and that id now names another task, `import` prints
  `crew.toml entry T012 was written for another task; review it`, and
  `dispatch` and `brief render` refuse the task until you fix the entry and set
  its `task` to the current text. (Entries written before `task` existed cannot
  be checked.) `import` edits `crew.toml` in place, so your comments survive; an
  unknown key is an error. A job stays bound to its first spec folder.
- **Briefs.** `brief render` writes the minimum an executor needs: the task, the
  acceptance criteria it points at (their text, not the whole spec), the
  constraints, interfaces, allowed files and proof. With `--review` it writes
  the reviewer's brief, which asks for two verdicts: spec (`done`, `missing`,
  `misunderstood`, `extra`) and quality (`Critical`, `Important`, `Minor`). The
  review brief is pinned to one attempt (`--attempt N`, default the last `ok`
  one): its worktree, start and end commits, so the reviewer checks the right
  `HEAD`. `dispatch --review` prints the reviewer's answer.
  `--converge` writes the last pass, which compares the code with the whole
  spec and lists the gaps that are not tasks yet, or answers `Converged`.
- **Proof.** After an `ok` attempt, `dispatch` runs the task's `proof` in the
  worktree. A failing or timed-out proof makes the attempt `proof_failed`, with
  the output in the note, and counts as a failure. Hands-on pieces without a
  proof are refused (`--no-proof` skips it and records that). A piece whose
  dependency or earlier phase is not accepted yet is refused (`--ignore-deps`
  overrides and records that). Files changed outside the task's `files` show up
  as a `scope:` line. Pieces without a spec task work as before and need no
  proof.
- **Accept.** `accept` marks the task `[X]` in `tasks.md`; CRLF, BOM and the
  rest of the file are left alone. When `tasks.md` is tracked and has no other
  changes, the tick is its own commit (`docs(tasks): mark T012 done`);
  otherwise it stays uncommitted and `accept` says so.
- **Rulings and decisions.** `ruling add` records a one-line ruling in the job.
  `decision add` writes it, or a title you give, to `docs/decisions/NNNN-slug.md`
  as an ADR with context, alternatives and consequences.
- **Close.** `close` also adds the job to the project CHANGELOG (see the
  `changelog` preference) and prints the economy block. `close --type fix`
  (or a `fix: ...` job title) makes it a fix in the commit message and in the
  CHANGELOG.

### Economy

Delegating only pays if the free work is not eaten by the coordinator's own
spending, by rework, or by delay. `usage add` records the coordinator's tokens
(`--role coordinator`, or `central` for a session above it) when its CLI
reports them. `economy` and `close` print the free share of pieces and tokens,
the coordinator's share, attempts per piece and the mean score, and one
`alert:` line for each limit crossed. The job's duration is recorded in the job
record (the `Time` line of its `.md`) and is what `alert_job_minutes` checks:

| Preference | Default | Alert |
| --- | --- | --- |
| `alert_coordinator_share` | `0.5` | the coordinator spent more than this share of all tokens |
| `alert_attempts_per_piece` | `2.5` | more exec attempts per piece than this |
| `alert_job_minutes` | `180` | the job took longer than this |

`0` turns an alert off. The skill tells the coordinator to read them: if it
spends more than it delegates, it should split less and do the work itself.

### Cost tiers, the budget and `setup`

Every executor sits on a four-step ladder, cheapest first; `forbidden` stays
outside it:

| Tier | Meaning | When `pick` uses it |
| --- | --- | --- |
| `free` | No money, no subscription (free previews, contributor models, Antigravity quota) | First, for every piece |
| `included` | Covered by a subscription you already pay for (a Codex or Claude plan) | When no free executor is left for the piece |
| `cheap` | Pay per use (OpenRouter, OpenCode paid models) | Only while the day's spend is under `cheap_budget_usd_day` |
| `paid` | Pay per use, the strong fallback | Thinking work only, or any work with `--strong` |

`pick` climbs a step only when a tier runs out — never because a free executor
merely failed. Execution work never reaches `paid` without `--strong`; with it,
`included` and `cheap` are still tried first. Attempts start at the lowest
effort that suits the piece, and a `quota` or `brief_refused` attempt does not
raise that ceiling. Run the CLI as the coordinator with `--coordinator <tool>`
(default `$CREW_CHIEF_COORDINATOR`) so `pick` uses your own CLI — or its
subscription family — last among the `included` executors; `--allow-own-cli`
turns that off.

`setup` reads the machine and writes the tiers for you:

```bash
crew-chief setup --project .          # asks one question at a time
crew-chief setup --yes --project .    # asks nothing: subscriptions no, budget 0, no training
```

It finds the CLIs on `PATH`, their models and the kind of each login (account
or API key — never the value), asks only what it cannot deduce, prints the count
per tier and writes one managed block to
`~/.config/agent-crew-chief/preferences.toml`, between `# >>> acc setup >>>`
and `# <<< acc setup <<<`, leaving everything outside it alone. It needs a
terminal unless `--yes` is given; it never stores a key value, only the kind of
login and the names of environment variables. `--lang pt|en` sets the language
(default: the system language). While no block exists, `discover` prints
`run setup to configure cost tiers`. **For a first install, run `setup --yes`.**

Every `cheap` or `paid` attempt appends one JSON line to `spend.jsonl`, next to
the global preferences file, whichever project it ran in. `pick` and `dispatch`
read it before spending: `cheap_budget_usd_day` is the daily ceiling across
projects (`0` keeps the cheap tier closed), `cheap_budget_usd_job` caps one job,
and a `[[models]]` rule may carry `cap_usd_day` to cap one executor. Two honest
limits: one attempt can go over the daily ceiling by at most its own cost (the
check runs before the attempt), and several attempts at once can each pass the
check and together exceed it. There is no per-attempt token limit, because no
supported CLI accepts one. A cheap model whose cost is unknown and which has no
`cap_usd_day` gets one probe attempt per executor per day (only while the
budget is open); afterwards it stays closed until you set a cap.

The project's `sensitive_paths` hide files and folders from an exec worktree:
each entry is a path prefix or an fnmatch wildcard, and a prefix like `.env`
also hides `.env.example`. A brief that names a sensitive path is refused as
`brief_refused`, and the worktree is checked again after the run — a path found
then is noted on the attempt.

### Together with superpowers

If the [superpowers](https://github.com/obra/superpowers) skills are installed
too, use them to discuss and design; use crew-chief to execute the plan. The
installed snippet says so in one line.

## Ponytail

[Ponytail](https://github.com/DietrichGebert/ponytail) (MIT, by DietrichGebert)
v5.3.2 ships inside crew-chief: the two skills `ponytail` and `ponytail-review`,
vendored in `vendor/ponytail/`. Ponytail is the "lazy senior dev" rule set — the
smallest change that fully solves the task, no option nobody asked for.

`install.py` copies both skills into `.claude/skills/` and `.agents/skills/` and
marks each folder with `.installed-by-agent-crew-chief`. A `ponytail` folder
without that marker belongs to someone else: it is left alone and the installer
says so. `--no-ponytail` skips the copy and never removes an existing folder:

```bash
python install.py /path/to/project --no-ponytail
```

How much of it reaches a brief is the `ponytail_level` preference:

- `lite` — build what was asked; name a smaller option in one line.
- `full` (default) — the smallest complete change.
- `ultra` — also question the request and push back on what it does not justify;
  can make a cheap model argue instead of building.
- `off` — no Ponytail in any brief.

An executor brief gets a `## Style (ponytail <level>)` section, and a review
brief counts bloat as an `Important` finding; `off` removes both.

## RTK

[rtk-oss](https://github.com/GhostCLI/rtk) v1.1.0 (Apache-2.0, by GhostCLI) is
the base CLI that ACC can prefix commands with. The installer downloads only the
pinned Apache-2.0 edition, never the BUSL-1.1 full edition, and verifies the
archive SHA-256 against both the pinned manifest and the release `checksums.txt`.
An existing `rtk` binary on PATH is left alone; download or verification failures
are non-fatal warnings.

The preference `rtk` controls when it appears in a brief:

- `auto` (default) — use `rtk` only when the binary is found on PATH.
- `on` — always prefix supported commands with `rtk`; warns if not on PATH.
- `off` — never mention `rtk` in any brief.

When active, an executor brief gets a `## RTK` section telling the model to
prefix commands (`rtk pytest -q`, `rtk git status`) and use `rtk proxy <cmd>`
for raw output. Background CLIs including Windows receive prompt text with hooks
disabled; keep briefs compact and request concise written rationale plus
validation evidence, ending with `ACC_RESULT: <one status>`. No request for
private chain-of-thought.

Reseller warning: bare-answer reasoning is unreliable and GhostCLI may cut the
middle of long prompts.

## Superpowers

[superpowers](https://github.com/obra/superpowers) skills are vendored MIT. The
preference `superpowers` defaults to `off` and can be set per job or piece.
Supported explicit skill names are:

- `test-driven-development`
- `systematic-debugging`
- `verification-before-completion`

With `auto`, the mode resolves from the task type: `code`/`tests` →
`test-driven-development`, `diagnosis`/`bug` → `systematic-debugging`,
`docs`/`rename` → `off`. Precedence is dispatch flag > piece flag > job flag >
project preference > user preference > default `off`. A brief that activates a
skill gets a `## Superpowers (<skill>)` section with the line `use the skill
superpowers:<skill>` followed by the vendored SKILL.md content.

## Install

The installer copies the crew-chief skill together with ponytail and
superpowers (skip them with `--no-ponytail` or `--no-superpowers`). rtk-oss is
downloaded only when you ask for it with `--with-rtk`:

```bash
git clone --branch v0.5.0 https://github.com/pauloroos/agent-crew-chief
python agent-crew-chief/install.py /path/to/project
python agent-crew-chief/install.py /path/to/project --with-rtk
```

No git? Download `agent-crew-chief-v0.5.0.zip` from the
[latest release](https://github.com/pauloroos/agent-crew-chief/releases/latest),
unzip it and run `python install.py /path/to/project` from the unzipped folder.
Pin a tag (`--branch vX.Y.Z`) so you never install code in the middle of a change;
`main` may be ahead of the latest release. See [CHANGELOG.md](CHANGELOG.md).

`install.py` copies the skill into `.claude/skills/crew-chief/` and
`.agents/skills/crew-chief/`, adds a short snippet between
`<!-- BEGIN:agent-crew-chief -->` and `<!-- END:agent-crew-chief -->` to
`AGENTS.md` and `CLAUDE.md` (both created if missing), to `GEMINI.md` (only if it
exists) and to `.cursor/rules/agent-crew-chief.mdc` (only if `.cursor/` exists,
with `alwaysApply: true`). It also creates `docs/crew-chief/preferences.toml`
from the example, plus `docs/crew-chief/jobs/.gitkeep`. It prints every change.

A `CLAUDE.md` that only contains `@AGENTS.md` is left alone. A file whose markers
you removed by hand stays that way; use `--force-snippet` to put the snippet
back:

```bash
python install.py /path/to/project --force-snippet
```

Run the installed CLI:

```bash
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project /path/to/project
```

Use `.claude/skills/crew-chief/scripts/crew-chief.py` when that is the copy your
coordinator reads. Every subcommand takes `--project <path>` and `--json`.

### Machine-wide install

Install the crew-chief skill once across all supported CLI paths so any agent
can load ACC in any repo without a per-project install:

```bash
python -I skill/crew-chief/scripts/install_machine_wide.py
```

This deploys to:

* `~/.claude/skills/crew-chief/` (Claude Code)
* `~/.claude-ghost/` symlink (Claude ghost routing)
* `~/.agents/skills/crew-chief/` (Codex and OpenCode)
* `~/.gemini/skills/crew-chief/` plus `~/GEMINI.md` reference (Gemini CLI)

The install is idempotent — running it again updates existing copies. Use
`--dry-run` to preview what would be installed, or `--home DIR` to target a
different home directory. After installation, type "load ACC, do X" in any
supported CLI to start a job.

## Update

Fetch the new tag and run the installer again:

```bash
cd agent-crew-chief && git fetch --tags && git checkout vX.Y.Z
python -I install.py --command
python -I install.py /path/to/project
```

The skill files and the snippet are refreshed. Your `preferences.toml`, your
`jobs/`, your `scoreboard.md` and your `models-seen.json` are never touched.

### Update check

ACC can check for a newer stable release in the background. Apart from
webhooks you configure and `install.py --with-rtk`, this is ACC's **only
external network access**. The check runs at most once per rolling 24 hours (including
failures), never blocks your command, and is completely silent when offline,
on timeout, or when the cache is stale or invalid. No setup is required.

**Preference.** `check_updates` is a strict boolean (default `true`; a strict
`false` opts out). Set `check_updates = false` in your project or global
`preferences.toml` to opt out entirely — no check is scheduled, no cached
notice is shown, and preferences are never rewritten. The existing
project-over-global precedence applies.

When a newer stable version is found, the central terminal view displays
exactly one line:

```
ACC v0.4.4 available. Update manually in your ACC source clone: git fetch --tags; git checkout v0.4.4; python -I install.py --command; python -I install.py <project>
```

(v0.4.4 above is a HYPOTHETICAL EXAMPLE; replace both V tokens with the
version shown in the notice.)

**These are four separate manual commands. Check each succeeds and STOP on failure before proceeding.** `<project>` is an explicit user-supplied project directory path. Repeat the last command for other installed copies if desired. ACC never infers clone paths, never auto-executes these commands, and never downloads, installs, or refreshes installed skills automatically.

The four separate commands, checked in sequence and stopping on any failure:

1. `git fetch --tags`
2. `git checkout v0.4.4` (sample tag)
3. `python -I install.py --command`
4. `python -I install.py <project>`

Central views only read the local `update-check.json` cache beside the
resolved global preferences, never HTTP: a one-shot command can return before
the detached worker finishes, and the next refresh or invocation shows the
result. Defaults: strict boolean default `true`; strict `false` disables all
checks. Throttling is a rolling 24 hours including failures, with offline
silence and no notice for an already-present version.

## Hermes / MCP server

ACC provides a JSON-RPC 2.0 Model Context Protocol (MCP) server so that external
clients (such as Hermes) can start, inspect, and manage coordinator jobs.

### stdio transport

When Hermes runs on the same host, it spawns ACC directly over standard I/O:

```bash
python3 -I <install>/scripts/crew-chief.py mcp --projects-dir <dir>
```

`--projects-dir <dir>` restricts operations to a trusted allowed projects directory.

### HTTP transport for containerized clients

When Hermes or another client runs in another container, run ACC's authenticated HTTP
server. Binding to a non-loopback address (`0.0.0.0`) requires passing `--token-file <file>`
with file permissions set to `0600` (on Windows, mode bits are not enforced as `chmod` has no effect, and the server prints a warning; the default host is loopback `127.0.0.1`):

```bash
python3 -I <install>/scripts/crew-chief.py mcp --http --host 0.0.0.0 --port <port> --token-file <file> --projects-dir <dir>
```

**Host configuration:** ACC does not set up container networks or publish ports itself.
The host operator must either attach the ACC container to the client's Docker network
with a network alias (for example, alias `acc`, so the client contacts `http://acc:<port>`)
or publish the port to the host.

**Hermes MCP configuration:** Point the Hermes client to `http://acc:<port>` (the JSON-RPC
endpoint path handled by `mcp_http.py`) with the header `Authorization: Bearer <contents of the token file>`.
The server accepts `Host: acc:<port>` as well as loopback hosts. Minimal configuration example:

```json
{
  "mcpServers": {
    "crew-chief": {
      "url": "http://acc:<port>",
      "headers": {
        "Authorization": "Bearer <contents-of-token-file>"
      }
    }
  }
}
```

### Tools and interview flow

The MCP server exposes eight tools:
* `start_job`: begin an interview-mode job.
* `answer`: provide an answer to a pending interview question.
* `questions`: list the interview questions and their answers.
* `status`: inspect interview state and pending questions.
* `events`: fetch job lifecycle events using cursor-based replay (`cursor`).
* `result`: retrieve the outcome of a finished job.
* `stop`: stop an active coordinator or job.
* `list_jobs`: list jobs known to ACC.

**Interview order:** The interview collects five required fields in strict order:
1. `project`
2. `request`
3. `coordinator`
4. `executors`
5. `end_mode`

Nothing runs until all questions are answered. Once the final question is answered,
the job is queued and launched.

### End notice webhooks

ACC can notify external listeners when a job ends or encounters a blocker. In
`preferences.toml` (project or global), configure:
* `notify_url`: destination webhook URL (e.g. `https://hermes:<port>/...` or a container URL). Must be HTTPS unless HTTP is explicitly allowed.
* `notify_secret_file`: path to a regular file with mode `0600` containing the shared secret.
* `notify_allow_http`: boolean (`false` by default; set to `true` only for local test URLs or private container networks).

On every terminal transition (`pr_open`, `failed`, `stopped`, `expired`) and on the
`blocked` state (for instance, when a git push or PR creation fails), ACC POSTs a JSON payload:

```json
{
  "job_id": "<job_id>",
  "status": "<pr_open|failed|stopped|expired|blocked>",
  "pr_url": "<url or null>",
  "timestamp": "<iso8601-utc>"
}
```

signed with the HTTP header:
```
X-ACC-Signature: sha256=<hmac-sha256-hex>
```
A failed notice is stored in the job record (bounded attempts and expiry; only the secret file path is stored, never the secret) and the next ACC process that touches the job, or the running central supervisor, delivers it with a valid signature; a delivered notice is not sent again. The backoff schedule is 30s, 2m, 10m, 30m, 1h. See
`docs/crew-chief/webhook-receiver-example.py` for a complete reference receiver implementation.

### Agent quickstart

Any installed agent (such as Claude Code, Codex, or OpenCode) can use ACC directly:

```bash
python -I .agents/skills/crew-chief/scripts/crew-chief.py interview --project <project> --json
```

CLI clients are not necessarily coordinator backends. Currently supported coordinator
backends are Claude (`claude`) and Codex (`codex`); OpenCode operates as an executor and
client harness only.

The interview flow gathers job parameters interactively or via JSON, automatically skipping
any questions whose values are already configured in preferences. Zero execution occurs and
no budget or tokens are spent before all answers are supplied and validated. For automated or
containerized agent workflows, link to the existing [Hermes / MCP server](#hermes--mcp-server)
and [Tools and interview flow](#tools-and-interview-flow) examples above.

Team routing and candidate model allowlists can be declared in `docs/crew-chief/preferences.toml`.
Project-level rules override the global preferences list:

```toml
# [team]
# coordinators = [
#   "claude:claude-3-7-sonnet",
#   "codex:gpt-5-codex",
# ]
# executors = [
#   "claude:claude-3-7-sonnet",
#   "codex:gpt-5-codex",
#   "gemini:gemini-2.5-pro",
#   "opencode:antigravity",
# ]
# reviewers = [
#   "codex:gpt-5-codex",
#   "claude:claude-3-7-sonnet",
# ]
```

The ordered `coordinators` list names the coordinator CLIs the project
prefers; the first one is the default. There is no automatic rotation: a
coordinator that hits a quota limit is held in `quota`, and `acc resume` (or
the maestro relaunching the front with another `--cli`) continues it. The
`executors` and `reviewers` lists constrain eligible models within the
repository.

Every dispatch enforces strict repository isolation and git flow delivery rules:
* **One job = one branch + one PR:** Work always proceeds on an isolated feature branch and never commits directly to `main`. Jobs created by the interview, by `job new` + `spec import` and by the MCP tools all get a `job_branch`, so the incremental push after each accepted piece and the single PR happen on every public path.
* **Incremental push:** The feature branch is pushed after each accepted work item (when `job_branch` is set).
* **Fail-stop safety:** If a `git push` or pull request creation fails, the round stops immediately and reports the exact command and error.
* **Owner merges:** ACC opens the pull request; the repository owner alone merges it.

## Preferences

Preferences live in `docs/crew-chief/preferences.toml` (created from
`skill/crew-chief/templates/preferences.example.toml`; a full copy with comments
ships in the skill). A global file at
`~/.config/agent-crew-chief/preferences.toml` is always read as well — set
`AGENT_CREW_CHIEF_GLOBAL_PREFS` to point at another location, and a missing file
is fine. Scalar keys are overridden one by one (project wins), and the
`[[tools]]` / `[[models]]` tables are merged by `name` / `match` with the
project winning. A model matched by no rule is `paid`, and a rule with
`cost = "forbidden"` wins at any level: a global `forbidden` beats a project
`free`.

Executor ids look like `<tool>:<model>`, for example
`opencode:opencode-go/longcat-2.5-preview-free`. The fixed vocabularies are work
types `code, tests, refactor, design, docs, research, diagnosis, data, review`,
risk `low, medium, high` and cost `free, included, cheap, paid, forbidden`.

Example:

```toml
# Tools tried for a paid escalation, best first.
strong_order = ["my-claude-proxy", "codex", "claude"]

# Chance (0..1) that low-risk work gets a second chance.
explore_rate = 0.1

# When true, a model with `trains = true` counts as `forbidden`.
forbid_training = false

# fnmatch patterns for paths a brief must never point at.
sensitive_paths = []

# none, github, plane, linear or other.
issue_tracker = "none"

# Hard limit for one dispatch attempt, in seconds.
dispatch_timeout_s = 900

# Executor id used for reviews by default; empty means "choose one".
review_default = ""

# Hours after which pick/dispatch re-check which models are new or gone.
model_check_hours = 24

# Minutes an executor sits out of pick after an attempt ended in `quota`.
quota_cooldown_min = 60

# Tools whose free executors come first on hands-on work.
exec_first = ["opencode"]

# Extra home entries (fnmatch) the outside-write check ignores, on top of the
# built-in list of agent CLI folders (.claude, .codex, .gemini, .config, ...).
ignore_home_entries = []

[[models]]
match = "opencode:opencode/*-free"
cost = "free"

[[models]]
match = "opencode:opencode-go/*-free"
cost = "free"

[[models]]
match = "antigravity:*"
cost = "free"

[[models]]
match = "*-preview-free"
cost = "free"
trains = true

[[models]]
match = "*contributor*"
cost = "free"
trains = true
```

Every key:

| Key | Meaning |
| --- | --- |
| `strong_order` | Tools tried when the work escalates to a paid model, best first. A tool that is not listed sorts last. |
| `explore_rate` | Chance (0 to 1) that a low-risk piece goes to an unrated or below-median executor instead of the best-scoring one. Default `0.1`. |
| `forbid_training` | When true, a model whose rule sets `trains = true` is treated as `forbidden` and never dispatched. |
| `sensitive_paths` | fnmatch patterns and path prefixes for files a brief must never point at, for example `[".env", "secrets/*"]`. A brief that names one is refused as `brief_refused`; the tracked matches are also hidden from an exec worktree. A prefix like `.env` also hides `.env.example`. |
| `issue_tracker` | `none`, `github`, `plane`, `linear` or `other`; used by `close`. |
| `changelog` | `keep-a-changelog` (default) or `none`. `close` adds the job to the project CHANGELOG under `## [Unreleased]` (`Added` for feat, `Fixed` for fix, `Changed` for refactor and perf), once per job, keeping CRLF and BOM; docs, tests and chores get no entry. `close --no-changelog` skips it. |
| `changelog_path` | CHANGELOG file, relative to the project. Default `CHANGELOG.md`. |
| `alert_coordinator_share` | Alert when the coordinator spent more than this share (0 to 1) of a job's tokens. Default `0.5`; `0` turns it off. |
| `alert_attempts_per_piece` | Alert when a job averages more exec attempts per piece than this. Default `2.5`; `0` turns it off. |
| `alert_job_minutes` | Alert when a job took longer than this many minutes. Default `180`; `0` turns it off. |
| `cheap_budget_usd_day` | Daily ceiling, in US dollars, for `cheap` models across every project. `0` (default) keeps the cheap tier closed. One attempt can go over it by at most its own cost. |
| `cheap_budget_usd_job` | Ceiling, in US dollars, for `cheap` models in one job. `0` (default) means no job ceiling. |
| `dispatch_timeout_s` | Hard limit for one attempt, in seconds. On overrun the process tree is killed and the attempt is recorded as `timeout`. Default `900`. |
| `review_default` | Executor id used for reviews when the coordinator has no better idea, for example `codex:gpt-5-codex`. Empty means "choose one yourself" (`pick --review`). |
| `exec_first` | Tools whose free executors `pick` tries first on hands-on work (`code`, `tests`, `refactor`, `docs`, `data`), before the other free ones. Default `["opencode"]`; `[]` turns it off. |

`[[models]]` rules classify an executor id with fnmatch; the first matching rule
wins:

| Key | Meaning |
| --- | --- |
| `match` | Glob tested against `<tool>:<model>`. |
| `cost` | `free`, `included` (a subscription you pay for), `cheap` (pay per use), `paid` or `forbidden`. |
| `trains` | Whether the provider may train on what you send it. With `forbid_training = true`, such a model becomes `forbidden`. |
| `cap_usd_day` | Optional: the most this executor may spend in one day. It also lets an executor of unknown cost in, up to the cap. |

`[[tools]]` describes one executor CLI (the built-in tools `opencode`,
`antigravity`, `codex`, `claude` and `cursor` are always present; an entry with
the same `name` replaces the built-in):

| Key | Meaning |
| --- | --- |
| `name` | Unique name; the prefix of the executor id. |
| `kind` | `claude-code`, `opencode`, `codex`, `antigravity` or `generic`. |
| `bin` | Executable to run, looked up on `PATH`. |
| `models` | Fixed list of model ids the tool accepts. Empty means "ask the tool" with `<bin> models`. Kinds `claude-code` and `codex` always use this fixed list. |
| `effort` | Whether the tool takes an effort/reasoning flag. Defaults to true for `codex` and `antigravity`, false otherwise. |
| `env` | Required environment variable names. A name missing from the environment is read from `HKCU\Environment` on Windows. |
| `cost_default` | Cost given to a model that no `[[models]]` rule matched. |
| `run_cmd` | argv template for `kind = "generic"`; the placeholders `{worktree}`, `{model}`, `{prompt}`, `{brief_file}` and `{output_file}` are filled in. |
| `list_cmd` | argv that prints one model id per line, for `kind = "generic"`. |
| `work_types` | Work types the tool may be picked for, for example `["docs", "research"]`. Missing or empty means every type, for every kind. An unknown type is a configuration error. |

## Adding a custom CLI

A Claude-Code-like CLI behind a third-party API key:

```toml
[[tools]]
name = "my-claude-proxy"
kind = "claude-code"
bin = "my-claude-proxy"
models = ["<model-id>"]
effort = false
env = ["MY_PROXY_API_KEY"]
cost_default = "paid"
```

`bin` must be on `PATH`; `env` names the variables the tool needs (values are
never printed or stored). Add `[[models]]` rules if the proxy has free models.
Any other CLI can be driven with `run_cmd`:

```toml
[[tools]]
name = "my-generic-cli"
kind = "generic"
bin = "my-generic-cli"
run_cmd = ["my-generic-cli", "run", "--model", "{model}", "{prompt}"]
list_cmd = ["my-generic-cli", "models"]
```

## New and gone models

Providers add and drop models without notice (OpenCode rotates its `*-free`
models often). `discover` compares what it finds with
`docs/crew-chief/models-seen.json` and prints what changed after the list:

```text
models: changes since 2026-10-18T12:00:00+00:00
new free: opencode:opencode/foo-free (trains=no)
now paid: opencode:opencode/bar (was free, trains=no)
gone: opencode:opencode/old-free (was free)
```

`job new` runs the same check and prints it on stderr (the job id stays alone
on stdout); `pick` and `dispatch` run it only when the last check is older
than `model_check_hours` (24 by default), so you hear about changes at least
once a day. The first run only records a baseline. A tool that was skipped (bin
missing, env var missing, no models listed) never makes its models "gone", and
a failing check never fails the command. A new model starts unrated, and
`pick`'s second chance (`explore_rate`, low-risk work) hands it a piece to
prove itself on. `--json` adds a
`changes` object with `added`, `removed` and `changed`.

## What gets committed

Everything under `docs/crew-chief/`:

- `preferences.toml` — the resolved preferences for the project;
- `jobs/<job_id>.json` and `jobs/<job_id>.md` — the machine-readable and the
  human-readable record of every job, with attempts, corrections, escalations
  and grades;
- `scoreboard.md` — the decayed ranking per work type;
- `models-seen.json` — every executor discovery has seen, with its cost,
  `first_seen`, `last_seen` and whether it is still there.

Spec-driven jobs also leave `specs/NNN-slug/crew.toml` (the sidecar, committed
with the spec) and ADRs under `docs/decisions/`.

They belong to the project, so a teammate (or the next session) inherits the
history. Committing them is the point: the scoreboard is only useful if it
survives.

## Scoreboard math

Each graded piece becomes one number per executor that worked on it — the
same one `grade --executor <id>` stores in the job (under the piece's
`grades`, keyed by executor) and `scoreboard.md` ranks by:

```
score = 100 * done / (done + missing)
        - 15 * corrections
        - 5 * extra
        - penalties
score = clamp(round(score), 0, 100)
```

The penalties are 40 for `destructive`, 60 for `outside_write`, 40 for
`false_done` and 30 for `model_mismatch`. When `done + missing == 0` the
adherence term is a full 100.

History is weighted by age with a 30-day half-life: a grade from 30 days ago
counts half as much as today's, one from 60 days ago a quarter as much. The
score shown per executor and work type is the weighted mean of its grades.
When a piece needed a second executor after a correction, both are graded and
both count: the first with the correction it drew, the second with its own
result.

Work is split between executing and thinking. The coordinator (the CLI you
opened) is the strong model: it thinks, splits and judges. Hands-on work
(`code`, `tests`, `refactor`, `docs`, `data`) starts with free executors: the
`exec_first` tools (OpenCode by default) first, then any other free one. When
no free executor is left, `pick` can still choose an `included` executor or a
budget-allowed `cheap` one; the `paid` tier needs `--strong`. Thinking work
(`review`, `design`, `diagnosis`, `research`) climbs the same ladder and
reaches the `paid` tier (`strong_order`) once the cheaper tiers are exhausted;
2 free failures on the piece start that climb. A tool whose `work_types`
leaves a type out is never picked for it, and the reason says so
(`not for review: <tool>`). `pick --review` picks a reviewer outside the quota
family of the executor that did the piece. `discover` ends with each tool's
effective work types.

Picking is free-first: the best decayed score wins. Ties (including "nobody is
rated yet") go to the executor used least in the current job, then to a tool
the job has used least, then to the shorter mean duration. On low-risk work
only there is a 1-in-10 chance (`explore_rate`, default `0.1`) that an unrated
or below-median executor gets a second chance. An executor whose attempt ended
in `quota` within `quota_cooldown_min` minutes (default 60), in any job of the
project, sits out with its whole quota family, and the pick reason lists every
id it skipped. The family is the model without a trailing effort suffix
(`-minimal`, `-low`, `-medium`, `-high`, `-xhigh`, `-max`); on Antigravity,
which shares its quota by vendor, it is `antigravity:<model up to the first
'-'>`, so `antigravity:claude-opus-5-5-high` in quota also benches
`antigravity:claude-opus-5-5-medium` and `antigravity:claude-sonnet-5`, but not
`antigravity:gemini-*`. On other tools it is `<tool>:<model>`: one OpenCode
model in quota leaves the others alone. No model is ever dropped for good.

Next to the score, each row shows what a pass costs that executor on that work
type:

| executor | score | n | attempts/pass | mean steps | mean tokens | mean duration | last graded |
| --- | --- | --- | --- | --- | --- | --- | --- |
| opencode:opencode/deepseek-v4.1-flash-free | 85 | 4 | 1.5 | 9 | 48210 | 95s | 2026-10-19 |
| cursor:auto | 70 | 2 | 1 | — | ~3100 | 40s | 2026-10-18 |

`attempts/pass` is the executor's exec attempts divided by the pieces it got
`ok` on. Steps (agent turns) and tokens come from the tool's own output where
it reports them (OpenCode `--format json`, Claude Code, Antigravity; Codex
gives a token total only); otherwise tokens are estimated as characters / 4 of
the brief and the answer, shown with `~`, and steps stay `—`. Steps, tokens and
duration average the `ok` attempts.

## Supported coordinators

Tested: **Claude Code**, **Codex** and **OpenCode**. Expected to work, not yet
tested end to end: **Cursor** and **Antigravity**. The installer writes the
snippet to `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and the Cursor rule so any
agent that reads one of those picks it up.

## Limitations

- **No real sandbox.** Executors run with their own permissions. Files written
  outside the worktree are detected by comparing snapshots before and after the
  run, and such an attempt is rejected as `outside_write` — after the fact, not
  before. The check looks at the top level of the home directory and of
  `<project>.crew/`; the project's other worktrees there and the folders agent
  CLIs keep in your home (`.claude`, `.codex`, `.gemini`, `.config`, `.local`,
  `.cache`, ...) are ignored, so a write inside those goes unnoticed. Add your
  own with `ignore_home_entries`. In the main repo, the records crew-chief
  itself rewrites (`docs/crew-chief/jobs/`, `models-seen.json`,
  `scoreboard.md`) are ignored too, since parallel dispatches update them.
- **Quotas are text.** A quota or rate-limit failure is detected from the
  executor's own error text (`429`, `quota`, `rate limit`, `usage limit`, `too
  many requests`), so an unusual message is recorded as a plain `failed`.
- **The spend ceiling is checked before the attempt.** `cheap_budget_usd_day`
  can be passed by at most one attempt's own cost, and several attempts running
  at once can each pass the check and together exceed the ceiling. There is no
  per-attempt token limit, because no supported CLI accepts one. A cheap model
  whose cost is unknown and has no `cap_usd_day` gets a single probe attempt per
  executor per day, while the budget is open.
- **A timeout kills the process tree** (`taskkill /T` on Windows, the process
  group elsewhere) and leaves the worktree for inspection; it cannot undo side
  effects the executor already had outside the worktree.
- The scoreboard is only as good as the grading: a piece that is not graded
  teaches the coordinator nothing.

## Credits

The spec-driven flow is inspired by [spec-kit](https://github.com/github/spec-kit)
(MIT), whose `specs/` layout and `tasks.md` format it reads, and by
[superpowers](https://github.com/obra/superpowers) (MIT), whose design and
review habits it follows. No code or template was copied from either.

[Ponytail](https://github.com/DietrichGebert/ponytail) v5.3.2 (MIT, by
DietrichGebert) ships vendored in `vendor/ponytail/`, with its `LICENSE`.

## License

MIT. See [LICENSE](LICENSE).
