---
name: crew-chief
description: Run a request through several coding agents in parallel and score every model on every job, so the next pick is better. Writes a short spec first when the request is not small, splits the work into lanes (one branch each), runs the lanes at the same time with Orca or with plain git worktrees, monitors them, reviews and scores each job, and delivers one pull request. Use when the user says "ACC", "crew chief", "load ACC, do X", or asks to have agents or models do the work and be compared. Not for a one-line edit or plain conversation.
---

# Agent Crew Chief

You are the coordinator. You spec, plan, pick a model per job, start the
workers, watch them, review, score and deliver. Workers are agents in their
own worktrees. ACC adds a score per model and kind of work, kept in
`~/.crew-chief/ledger.jsonl`, outside every repository.

`ACC` below is `python -I .agents/skills/crew-chief/scripts/crew-chief.py`
(or `.claude/skills/...`, whichever exists).

Any CLI can coordinate (Claude Code, Codex, OpenCode, ...). Choose the mode:

- **Orca mode** when `orca status --json` shows a running Orca. Run in an Orca
  terminal of the project, so Orca knows you as the coordinator; load the
  guide from `orca skills get orchestration` once before the first Orca command.
- **Plain mode** otherwise, or when the user asks for it: steps 3 and 4 become
  "Without Orca" below; everything else stays the same.

Do not ask permission between steps; the one exception is the spec approval
in step 0. Otherwise ask only when the request is unclear; report at the end.

## 0. Spec first

Skip this step for a small request (one job, one file area, no open question)
or when the user says "run directly" / "sem spec".

1. Read the request and the code. Before the spec, ask only questions whose
   answer changes what gets built: at most 5, in one message. On "decide for
   me", decide and write your assumptions into `spec.md`.
2. Folder: look for a specs folder the project already has (spec-kit's
   `specs/`, for one) and use it; otherwise `specs/<NNN>-<slug>/` at the repo
   root, `<NNN>` the next free number.
3. Write `spec.md` and `tasks.md` from this skill's `templates/spec/`; add
   `plan.md` only when the design is not obvious. In `tasks.md`, mark `[P]` on
   tasks that can run in parallel and write "depends on Txxx" for order.
4. Create `acc/<slug>` from the base branch and commit the spec there before
   any lane branch exists, so every worker and the pull request carry it.
5. Plan lanes (step 1) and pick models (step 2), then stop once: show the spec
   summary and the lanes, and wait for approval. Then run to the end.

## 1. Plan lanes

- Read the request and the code it touches. Split it into **jobs**: one
  reviewable change each, with a kind (`code`, `tests`, `refactor`, `design`,
  `docs`, `research`, `diagnosis`, `data`, `review`) and a check that proves
  it is done (a test, a command, an output).
- Group the jobs into **lanes**. Jobs that touch the same files or need each
  other's result share a lane and run in order. Lanes that do not overlap run
  at the same time. At most 4 lanes; one lane is fine for small work. With a
  spec, the jobs are the tasks in `tasks.md`: dependent or same-file tasks
  share a lane; `[P]` tasks with no shared files go to separate lanes.
- Branches: the feature branch `acc/<slug>` from the base branch, and one
  branch per lane, `acc/<slug>-<lane>`, from the feature branch. Without a
  spec, tell the user the plan in a few lines and go on.

## 2. Pick a model per job

Run `ACC models` first. If it lists models, pick only among them (`ACC pick`);
the notes say when to use each. `ACC pick --kind <kind>` names the cheapest
model that scored well at least twice, then the scoreboard. "Not proven yet"
means little data: use it, or try a model with no score yet; the score you
record settles it. Prefer free models for plain work, a stronger one for risky
or subtle jobs, `--max-cost` when the user sets a limit. Agent = Orca agent id
or CLI (`codex`, `claude`, `opencode`, ...); model = its `--model` value.

## 3. Start the lanes

```text
orca orchestration run-create --objective "<the request>" --json
orca worktree create --name <slug>-<lane> --base-branch acc/<slug> --branch acc/<slug>-<lane> --setup skip --json
orca orchestration worker-start --spec "<job spec>" --worktree branch:acc/<slug>-<lane> --agent <agent> --model <model> [--effort <level>] --json
```

Start the first job of every lane before waiting. A job spec is
self-contained and names:

- **Target** - the files or area in scope; with a spec, its folder and task ids.
- **Change** - the result to produce.
- **Constraints** - what must not change; no secrets, `.env` or private data.
- **Acceptance** - the check that must pass.
- **Working rules** - always this text: "Use test-driven-development for code
  changes, systematic-debugging for bugs, and verification-before-completion
  before you finish. Follow ponytail: the smallest complete change. If these
  skills are not installed, read this as plain guidance."
- **Finish** - commit the work on the current branch with a clear message; do
  not push, merge or open a pull request; send `worker_done` (plain: exit).

## 4. Monitor

```text
orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 100000 --json
```

Keep each wait under 2 minutes: your CLI kills longer commands. An empty
result only means wait again. Answer questions, handle every `worker_done`,
ack, and wait again until every lane is done. Follow the Orca guide for empty
waits and stuck workers.

A worker can miss its spec on a cold start: after 5 minutes with no commit,
look at `orca orchestration worker-read --dispatch <id> --source auto --json`.
An empty prompt means it never started: `worker-stop` it and start the job
again, recording nothing.

## Without Orca (plain mode)

Replaces steps 3 and 4. `<repo>` is the repository folder's name; worktrees
and logs sit next to it, never inside. Per lane, add the worktree from your
checkout. Per job, write the job spec to `../<repo>-acc-logs/<lane>-<job>.md`
and start the worker in the background from inside the worktree with the
prompt "Do the job in <its absolute path>" (a spec on the command line breaks
on Windows), log `../<repo>-acc-logs/<lane>-<job>.log`, PID in `../<repo>-acc-logs/<lane>.pid`
(never inside the worktree):

```text
git worktree add ../<repo>-acc-<lane> -b acc/<slug>-<lane> acc/<slug>
claude -p "<prompt>" --model <model> --dangerously-skip-permissions   # refused as root
codex exec -C <worktree> -s workspace-write -m <model> "<prompt>"
opencode run --standalone --auto -m <model> "<prompt>"
# Linux/macOS: start (job control off, so $! is the worker), then kill its group
set +m; setsid nohup <cmd> > "<log>" 2>&1 & echo $! > "<pidfile>"
kill -- -<pid>
# Windows (PowerShell): start, then kill the whole tree
$p = Start-Process cmd -ArgumentList '/c <cmd>' -WorkingDirectory <worktree> -PassThru -WindowStyle Hidden -RedirectStandardOutput "<log>" -RedirectStandardError "<log>.err"; Set-Content "<pidfile>" $p.Id -Encoding ascii
taskkill /PID <pid> /T /F
```

Monitor, each wait under 2 minutes: process alive, new commits
(`git -C <worktree> log --oneline acc/<slug>..`), changed files (`git -C
<worktree> status --porcelain`, newest mtime) and log growth (`<log>` and
`<log>.err`, where opencode writes its progress; `claude -p` prints only at
the end). Process gone means the job ended: review it (step 5);
the exit code does not decide the outcome, the commits and the check do.
30 minutes with no commit, file change or log growth: kill the tree; it is a
failed attempt. A plain worker cannot ask you anything: keep specs self-contained.

## 5. Review and score every job

On each `worker_done` (plain mode: when the worker's process ends):

1. Read the lane's new commits (`git -C <lane worktree> log -p <before>..HEAD`)
   and run the job's check yourself. Do not trust the worker's summary.
2. Score the worker 0-100: 90+ done and clean; 70-89 done with small fixes;
   40-69 partly done or needed real fixes; below 40 wrong, broken or nothing.
   Score what the worker delivered, even when you fix it afterwards.
3. Record it, always, pass or fail:
   `ACC record --kind <kind> --agent <agent> --model <model> [--effort <e>] --cost <free|included|cheap|paid> --outcome <pass|fail> --score <n> --minutes <n> --lane <lane> --job "<title>" --note "<why>"`
4. Pass: release the worker and start the lane's next job in the same
   worktree. Fail: start the job again with another model (next in
   `ACC pick`, or one tier up), with the review findings added to the spec.
   After two failed retries, fix it yourself or stop the lane and report it.

## 6. Deliver

- When every lane passed: in your checkout on `acc/<slug>`, merge each lane
  (`git merge --no-ff --no-edit acc/<slug>-<lane>`), resolve conflicts, run the
  project's tests, use verification-before-completion, then push the branch
  and open the pull request with `gh pr create`.
- Release every worker; remove lane worktrees and branches: Orca mode `orca
  worktree rm --worktree branch:acc/<slug>-<lane>` (deletes the branch too),
  plain mode `git worktree remove ../<repo>-acc-<lane>`, `git branch -D
  acc/<slug>-<lane>` and, once no worker runs, `../<repo>-acc-logs/`. A folder
  "open in another program" is held by a leftover process: leave it, report it.
- Report to the user: the pull request, each job with its model, outcome and
  score, anything left open, and `ACC score --kind <kinds used>`.

## Rules

- Never commit or push to `main`, never `--force`, never rewrite pushed
  history. The owner merges the pull request unless they said otherwise.
- Workers never push, merge or open pull requests; you do.
- Orca may put the lane worktrees inside your checkout: never `git add -A`
  or `git add .` there; add files by name.
- Never create, copy or ask for credentials or tokens.
- `ACC score` shows the whole scoreboard; `ACC import <project>` copies the
  grades an ACC 0.6 project kept in `docs/crew-chief/jobs` into the ledger.
