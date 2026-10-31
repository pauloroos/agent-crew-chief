# Tutorial: your first ACC run

**English** · [Português](tutorial.pt-BR.md)

This walk-through runs ACC v0.8.0 end to end on `examples/hello-crew`, a tiny
Python project that ships with ACC. It has two independent modules, each with
its own test:

```text
src/hello_crew/greet.py   greet("crew") -> "Hello, crew!"      tests/test_greet.py
src/hello_crew/cli.py     main(["crew"]) prints the greeting    tests/test_cli.py
```

You will paste one fixed request that needs a spec and splits into two
parallel lanes, one per module, then check the result with commands whose
output you know in advance. A second, small request shows the path without a
spec.

## What you need

- Python 3.11+ with `pytest` (`python -m pip install pytest`) and git 2.28+.
- A coordinator CLI: Claude Code, Codex or OpenCode, logged in.
- At least one worker CLI (`claude`, `codex` or `opencode`), logged in. It can
  be the same as the coordinator.
- Optional: [Orca](https://github.com/stablyai/orca) running, for Orca mode.
- Optional: the GitHub CLI `gh`, already logged in, to see a real pull request.

Commands are the same in bash and PowerShell unless two blocks are shown.

## 1. Make a scratch project

Copy the example **out of** the ACC checkout. Inside it, the lanes and `acc/*`
branches would land in the ACC repository itself.

```bash
git clone --branch v0.8.0 https://github.com/pauloroos/agent-crew-chief ~/acc
mkdir -p ~/scratch && cp -R ~/acc/examples/hello-crew ~/scratch/hello-crew
cd ~/scratch/hello-crew
git init -b main
git add . && git commit -m "Start hello-crew"
python -m pytest -q
```

```powershell
git clone --branch v0.8.0 https://github.com/pauloroos/agent-crew-chief $HOME\acc
New-Item -ItemType Directory -Force $HOME\scratch | Out-Null
Copy-Item -Recurse $HOME\acc\examples\hello-crew $HOME\scratch\hello-crew
Set-Location $HOME\scratch\hello-crew
git init -b main
git add .; git commit -m "Start hello-crew"
python -m pytest -q
```

Expected: `3 passed`.

## 2. Install ACC with the kit

Install, then commit the installed files so every lane worktree gets the
skills too.

```bash
python ~/acc/install.py .
git add . && git commit -m "Install ACC 0.8.0"
alias ACC='python -I .agents/skills/crew-chief/scripts/crew-chief.py'
ACC score --kind code
```

```powershell
python $HOME\acc\install.py .
git add .; git commit -m "Install ACC 0.8.0"
function ACC { python -I .agents/skills/crew-chief/scripts/crew-chief.py @args }
ACC score --kind code
```

The installer prints one line per copy (`copied skill to .claude/skills/crew-chief`,
...) and the snippet it added to `AGENTS.md` and `CLAUDE.md`. `ACC score` prints
`No graded jobs yet.` on a machine that never ran ACC, or your existing scoreboard.

**Optional, to see a real pull request:** give the project a scratch GitHub
repository now, before the run (uses your existing `gh` login).

```bash
gh repo create hello-crew-scratch --private --source . --push
```

Without a remote, the run stops at the merged local branch. Both are fine.

## 3. Open the coordinator

- **Orca mode:** add `~/scratch/hello-crew` to Orca and open a terminal of the
  project in Orca. `orca status --json` there should show Orca running.
- **Plain mode:** any terminal (or VS Code), in `~/scratch/hello-crew`. If Orca
  is running and you still want plain mode, add "Without Orca." to the request.

Start your CLI there: `claude`, `codex` or `opencode`.

## 4. Paste the request

Paste this exactly:

```text
ACC, add two features to hello-crew.

1. greet: greet(name, lang="en") gains a lang option. lang="en" keeps
   "Hello, <name>!"; lang="pt" returns "Olá, <name>!". Change only
   src/hello_crew/greet.py and tests/test_greet.py.
2. cli: a --shout flag prints the greeting in uppercase, so
   main(["--shout", "crew"]) prints "HELLO, CREW!". Without the flag the
   output stays the same. Change only src/hello_crew/cli.py and
   tests/test_cli.py.

The two changes are independent: each touches only its own module and its own
test file, so run them as two parallel lanes, one per module. Each lane adds
tests for its change, and python -m pytest must pass. If the repository has
no remote, stop at the merged local branch.
```

## 5. The interview, the spec and the one stop

The request leaves real questions open, so the coordinator asks them first,
in one message. **Example** (yours will differ in wording):

```text
Before I write the spec, two questions:
1. greet(name, lang="xx") with an unknown language: raise ValueError, or fall back to English?
2. Should --shout work on either side of the name ("--shout crew" and "crew --shout")?
```

Answer, for example: `1. Raise ValueError. 2. Either side.` Or answer
`decide for me`: the coordinator then writes its assumptions into `spec.md`.

It then writes the spec on a new branch `acc/<slug>` and commits it.
**Example** of `specs/001-greet-lang-and-shout/tasks.md`:

```markdown
# Tasks: greet lang and cli --shout

## Phase 3: User Story 1 (P1)
- [ ] T001 [P] [US1] Add lang ("en", "pt"; unknown raises ValueError) to greet() in src/hello_crew/greet.py, tests in tests/test_greet.py
- [ ] T002 [P] [US2] Add --shout (uppercase output) to main() in src/hello_crew/cli.py, tests in tests/test_cli.py
```

`spec.md` sits next to it (what the user wants, stories, requirements, "done
when"). `plan.md` is usually absent here: the design is obvious.

Then the **only stop** of the run. **Example:**

```text
Spec: specs/001-greet-lang-and-shout/ (committed on acc/greet-lang-and-shout)
Lanes, in parallel:
  greet  T001  code  opencode:<model>  free      check: pytest tests/test_greet.py
  cli    T002  code  codex:<model>     included  check: pytest tests/test_cli.py
Approve, and I run both lanes to the end.
```

Reply `approved`. From here the coordinator does not stop until it reports.

## 6. Watch the lanes

**Orca mode:** the Orca panel shows two workers, one per lane worktree. From
another terminal:

```text
orca worktree ps
```

**Plain mode:** worktrees and logs sit next to the project, never inside.
Lane names are the coordinator's choice; these use `greet` and `cli`.

```bash
ls ../hello-crew-acc-logs
tail -n 20 ../hello-crew-acc-logs/*.log
git -C ../hello-crew-acc-greet log --oneline -3
git -C ../hello-crew-acc-cli status --porcelain
```

```powershell
Get-ChildItem ..\hello-crew-acc-logs
Get-Content ..\hello-crew-acc-logs\*.log -Tail 20
git -C ..\hello-crew-acc-greet log --oneline -3
git -C ..\hello-crew-acc-cli status --porcelain
```

`ls` shows a `.md` job spec, a `.log` and a `.pid` per lane. `claude -p` writes
its log only at the end, so an empty log is normal while it works; new commits
and changed files are the progress signal.

## 7. Review and scores

When a worker finishes, the coordinator reads its commits, runs the check
itself and records a grade. You will see lines like:

```text
recorded opencode:<model> code pass 88
```

Look at the scoreboard and the next pick:

```text
ACC score --kind code
ACC pick --kind code
```

After a first run each model has `n` 1, so `pick` adds `- not proven yet, try
another too`: a model needs a score of 70 or more over 2 jobs to be "proven".

## 8. Delivery

The coordinator merges both lanes into `acc/<slug>` with `--no-ff`, runs
`python -m pytest`, removes the lane worktrees and branches, and then:

- **with the scratch remote:** pushes `acc/<slug>` and opens a pull request.
  `gh pr view --web` opens it. Merge or close it yourself.
- **without a remote:** stops on the merged local branch `acc/<slug>`.

The final report lists each job with its model, outcome and score.

## 9. Did it work?

Set the slug to the branch name in the report (here, the example's):

```bash
SLUG=greet-lang-and-shout
```

```powershell
$SLUG = "greet-lang-and-shout"
```

| Command | Expected output |
| --- | --- |
| `git log --oneline --first-parent "main..acc/$SLUG"` | 3 lines: two `Merge branch 'acc/<slug>-…'` lines and, at the bottom, the spec commit. Drop `--first-parent` to also see the workers' commits. |
| `git ls-tree -r --name-only "acc/$SLUG" specs` | `specs/001-<slug>/spec.md` and `specs/001-<slug>/tasks.md` |
| `git diff --stat main "acc/$SLUG"` | the two spec files, `greet.py`, `cli.py`, `test_greet.py`, `test_cli.py`; nothing else |
| `git branch --list "acc/*"` | one line: `acc/<slug>` (with `*` if checked out); no lane branch, except one whose folder the report says is held by another program |
| `git worktree list` | one line: the project folder; a held lane folder shows as `prunable` until you remove it (see below) |
| `ACC score --kind code` | the models you used, `last` = today's date (UTC), one more in `n` per job |
| `git ls-remote origin "refs/heads/acc/*"` | one line ending `refs/heads/acc/<slug>`; no lane branch (workers never push). Skip without a remote. |
| `python -m pytest -q` (on `acc/<slug>`) | `N passed`, N ≥ 5, no failures |
| `ls .claude/skills` | `crew-chief`, `ponytail`, `ponytail-review`, `systematic-debugging`, `test-driven-development`, `verification-before-completion` |
| `ls .claude/skills/*/.installed-by-agent-crew-chief` | 5 paths, one per kit folder (`crew-chief` is ACC's own skill and has none) |

In PowerShell, the last two are:

```powershell
(Get-ChildItem .claude\skills).Name
(Get-ChildItem .claude\skills\*\.installed-by-agent-crew-chief -Force).Directory.Name
```

A folder "open in another program" can block the lane cleanup on Windows (a
worker's leftover process, often an MCP server, holds it): the coordinator
leaves it and says so. Once that process ends (or after a reboot), run
`git worktree prune` and `git branch -D acc/<slug>-<lane>`.

## 10. A small request, without a spec

Start from `main` and open a new coordinator session:

```text
git switch main
```

Paste:

```text
ACC, make greet() strip spaces around the name, so greet("  crew ") returns
"Hello, crew!". Change only src/hello_crew/greet.py and tests/test_greet.py.
If the repository has no remote, stop at the merged local branch.
```

One job, one file area, no open question: no interview, no spec files and no
approval stop. The coordinator tells you the plan in a few lines and runs one
lane. Set `SLUG2` to the new branch name, then:

| Command | Expected output |
| --- | --- |
| `git log --oneline --first-parent "main..acc/$SLUG2"` | 1 line: the lane merge; no spec commit |
| `git diff --stat main "acc/$SLUG2"` | only `greet.py` and `test_greet.py`; no `specs/` |
| `ACC score --kind code` | the model used shows `n` one higher and `last` today |

To force this path on a bigger request, say "run directly" (or "sem spec").

## Clean up

Delete `~/scratch/hello-crew` and, if you made one, the `hello-crew-scratch`
repository on GitHub. The grades stay in `~/.crew-chief/ledger.jsonl`; that is
the point.
