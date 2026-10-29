# 04-central · Coordinators in their own worktrees

This tutorial installs the `acc` command, launches a coordinator in its own
worktree, watches it from the central screen, talks to it, pauses and stops
it, recovers it after a failure and integrates its branch yourself. It builds
on tutorials 01–03 and uses the small
[`examples/hello-crew/`](../../examples/hello-crew/) project.

Everything here works on Windows, macOS and Linux with only Python 3.11+, git
and one supported coordinator CLI (`claude` or `codex`). No Orca, tmux, pty,
curses or ConPTY is needed: the screen and `attach` use the terminal you
already have.

## 1. Install the `acc` command

Clone the release once and install only the user-scoped command wrapper:

```bash
git clone --branch v0.4.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py --command
```

The installer writes `acc` (or `acc.cmd` on Windows) into your user bin
directory (`~/.local/bin`, or `%LOCALAPPDATA%\agent-crew-chief\bin` on
Windows; `--bin-dir DIR` picks another one) and prints where it went. It never
edits `PATH` or your preferences: add that directory to `PATH` yourself if it
is not there yet. The old `python -I .../crew-chief.py` entry keeps working.

Check it:

```bash
acc --version
acc --help
```

## 2. Prepare the project

A coordinator runs in a new worktree created from the project's `HEAD`, so the
project must have ACC installed **and committed** at the same version as the
`acc` command:

```bash
python -I ~/tools/agent-crew-chief/install.py .
git add -A
git commit -m "chore: install agent-crew-chief"
```

If you skip this, `launch` stops with "no worktree ACC is installed"; pass
`--install-worktree-acc` to let it install ACC only inside the new worktree.

## 3. Launch a coordinator

Write the request in a file, then launch. Every launch states how much it is
authorized to do:

```bash
acc launch --repo . --request-file request.md --mode raw-request --authorize-raw
```

- `raw-request` + `--authorize-raw`: the coordinator may plan from the raw
  request.
- `direct-spec` + `--spec-dir specs/001-feature`: work from an existing spec.
- `approved-spec` + `--spec-dir specs/001-feature --approved`: a spec you have
  already approved.

```bash
acc launch --repo . --request-file request.md --mode approved-spec --spec-dir specs/001-feature --approved
```

Useful options: `--cli claude|codex` picks the coordinator CLI (default: your
`coordinator_default` preference, else the CLI you are in), `--restricted`
keeps the coordinator from bypassing permission prompts, `--base REV` starts
the branch elsewhere than `HEAD`.

`launch` prints `launched coordinator <ID>`. The coordinator gets its own
branch and worktree; your checkout is never touched, merged or pushed. A
detached supervisor runs it in bounded rounds.

The central registry lives in `~/.config/agent-crew-chief/central/` and every
`acc` command finds it from any directory. `--central-dir DIR` (or the
`AGENT_CREW_CHIEF_CENTRAL_DIR` variable) points at another one.

## 4. Monitor

Open the central screen in a second terminal:

```bash
acc central
```

Each row shows the repository, CLI, state, accepted/total pieces and score,
with active pieces underneath and pending questions highlighted. Keys: arrows
select, `Enter` attaches, `m` replies, `p` pauses/resumes, `d` toggles
details, `Esc` closes details, `q` quits (quitting never stops a coordinator).

For a script or a redirected terminal, print one plain snapshot:

```bash
acc central --once
```

## 5. Message and answer questions

Messages are durable files: they wait in the coordinator's inbox and are read
between rounds, with IDs and acknowledgments so nothing is lost or read twice.

```bash
acc message COORDINATOR_ID --text "Keep the public API unchanged."
acc message COORDINATOR_ID --text "Use SQLite." --reply-to QUESTION_ID
```

`--reply-to` answers a question the coordinator asked; only the work that
depends on that question waits for it.

## 6. Attach, pause and resume

Take over the coordinator's session in your terminal:

```bash
acc attach COORDINATOR_ID
```

The supervisor yields after the current round. If the CLI cannot resume a
session or your terminal is redirected, `attach` says so and keeps the
background session; it never starts a substitute session.

```bash
acc pause COORDINATOR_ID
acc resume COORDINATOR_ID
```

`pause` lets the current round finish and then holds; `resume` makes the
coordinator eligible again.

## 7. Stop and recover

```bash
acc stop COORDINATOR_ID
```

Stop is cooperative: the supervisor stops after the current round. No process
is killed by a stored PID.

A coordinator that failed (or that you stopped) is held until you decide:

```bash
acc recover COORDINATOR_ID
```

`recover` moves a `failed` or `stopped` coordinator back to `created`; nothing
is retried silently. Its messages and worktree are kept.

## 8. Integrate the work yourself

The coordinator uses the same job cycle as before (pick, brief, dispatch,
review, grade, accept, close) inside its own worktree and leaves the result on
its branch. Review that branch like any other and merge it when you are
satisfied:

```bash
git log --oneline main..BRANCH
git merge BRANCH
```

ACC never merges or pushes for you.

## 9. Inspect in a browser (v0.4.1)

The browser shows the same records as the terminal: cards, questions, job
history, model token charts, scoreboard, totals and diagnostics.

```bash
acc central --web
acc central --web --port 8080
```

The page is loopback only at `http://127.0.0.1:<port>/` (default: an
ephemeral port) and is served from the packaged static assets
`skill/crew-chief/web/index.html`, `skill/crew-chief/web/central.css` and
`skill/crew-chief/web/central.js`. There is no CDN and no external script
or stylesheet; untrusted text renders through `textContent` only, never as
markup.

Local actions (message, reply to a question, pause, resume, stop, terminal
entry) are authorized per server: the page first fetches a CSRF token, then
sends it with a retry id on every POST, and the server enforces strict
Origin/Host checks with no CORS. Buttons stay disabled until the token
arrives. The toolbar acts on the coordinator you select; if that
coordinator disappears, the page asks you to choose again. Each question
has its own Reply, and Stop asks for confirmation naming the coordinator.

Terminal entry is controlled: Attach queues an intent for an active
terminal view, which performs the same handoff as `Enter`. The page shows
the request as queued (it may wait while another session is open), then
open while you work in the terminal, then closed with the session's exit
code, or why it failed. With no available terminal bridge, or after a
failure, the page shows a copyable instruction instead, quoted for your
shell (PowerShell on Windows). When you started the view with
`--central-dir`, the instruction carries the same directory as an
absolute path: under your home it is shown as `~/...`; outside your home
it is shown in full, because without it the command would open another
registry, and the page is served only to your own loopback browser:

```bash
acc attach COORDINATOR_ID
```

It does not claim it opened the session; run that command in your own
terminal.

Token counts say "includes estimates" or "partial" when some usage was
estimated or not reported. If the page loses the connection it keeps the
last data, marked stale, and retries.

Reproducible fake-browser smoke (synthetic fixtures only, no live
coordinator, no real registry):

```bash
python tests/test_central_web_assets.py --smoke
```

1. It prints an ephemeral loopback URL and serves synthetic snapshots only.
2. Open `http://127.0.0.1:<port>/` and confirm cards, questions, history,
   charts, scoreboard and totals render from `/api/snapshot`.
3. Confirm HTML-like demo text appears literally, never as markup.
4. Confirm no external scripts or stylesheets load, then stop the server and
   confirm the listening socket is released.

## 10. Optional Orca backend (v0.4.1)

Default mode never needs Orca. Pass `--backend orca` only when you want
Orca to create the worktree:

```bash
acc launch --repo . --request-file request.md --mode raw-request --authorize-raw --backend orca
```

Orca does one thing: it creates the coordinator's worktree. Everything after
that (supervisor, rounds, messages, controls, attach and recovery) is the same
core as in default mode.

The `probe` runs only at launch: `acc launch --backend orca` checks the
`orca` executable on `PATH` (`--version`, `--help` and the machine-readable
`agent-context --json` capabilities) before anything is created, and again
right before it asks Orca for the worktree. A missing executable, an
unsupported version or a missing capability fails the launch explicitly,
with no fallback to the default backend and no switch mid-launch.

The backend is stored with the coordinator and is immutable. `acc message`,
`acc attach`, `acc pause`, `acc resume`, `acc stop` and `acc recover`, and the
same actions in `acc central` and in the browser, route by that stored name
and never call Orca. If Orca is removed or upgraded later, you can still
message, attach, pause, stop and recover an Orca coordinator:

```bash
acc stop COORDINATOR_ID
```

A coordinator whose stored backend is neither `default` nor `orca` is refused
with an `unknown backend` error; it is never handled as another backend.

## 11. v0.4.1 limits

- Loopback only (`127.0.0.1`), ephemeral port by default; no remote server
  or shared-user access, and no browser shell is ever embedded.
- The owner integrates: review the coordinator branch and merge it yourself
  as in section 8. ACC never merges or pushes, and never removes a
  coordinator worktree automatically.

## 12. Update check

ACC can check for a newer stable release in the background. This is ACC's
**only external network access** — the browser and all other commands are
loopback-only. The check runs at most once per rolling 24 hours (including
failures), never blocks your command, and is completely silent when offline,
on timeout, or when the cache is stale or invalid. No setup is required.

**Preference.** `check_updates` is a strict boolean (default `true`; a strict
`false` opts out). Set `check_updates = false` in your project or global
`preferences.toml` to opt out entirely — no check is scheduled, no cached
notice is shown, and preferences are never rewritten. The existing
project-over-global precedence applies.

When a newer stable version is found, both the terminal and web central views
display exactly one line (wrapping visually on phones):

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
