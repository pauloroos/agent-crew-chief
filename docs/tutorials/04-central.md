# 04-central · Coordinators in their own worktrees

This tutorial installs the `acc` command, launches a coordinator in its own
worktree, watches it from the central screen, talks to it, pauses and stops
it, recovers it after a failure and integrates its branch yourself. It builds
on tutorials 01–03 and uses the small
[`examples/hello-crew/`](../../examples/hello-crew/) project.

Everything here works on Windows, macOS and Linux with only Python 3.11+, git
and one supported coordinator CLI (`claude` or `codex`). No tmux, pty,
curses or ConPTY is needed: the screen and `attach` use the terminal you
already have.

## 1. Install the `acc` command

Clone the release once and install only the user-scoped command wrapper:

```bash
git clone --branch v0.5.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
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

## 9. Quota hold

When the coordinator CLI stops on a quota or rate limit (an HTTP 429, "usage
limit", "rate limit"), the coordinator is not marked failed: it moves to
`quota` and holds. Its worktree, job records and session stay as they are.

```bash
acc resume COORDINATOR_ID
```

`resume` continues from the same records once the limit has reset. To switch
CLIs instead, stop it and launch the same request again with another `--cli`.
The maestro (tutorial 05) does this for every coordinator it runs.

## 10. Limits

- Terminal only: `acc central` is the screen, `acc central --json` prints one
  snapshot for scripts and the maestro.
- The owner integrates: review the coordinator branch and merge it yourself
  as in section 8. ACC never merges or pushes, and never removes a
  coordinator worktree automatically.

## 11. Update check

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
