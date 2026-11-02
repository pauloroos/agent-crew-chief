# The Pit Wall method

ACC's way of running a job: cheap workers do most of the work, risky changes
get independent eyes before they are accepted. Themed on motor racing. This
file is written for the **race engineer**, who reads it and follows its rules.

## Stint

One driver does one job in its lane worktree; that run is a **Stint**. Before
it, record `<start>` (see Fresh tyres) and write the job spec. A job spec is
self-contained and names:

- **Target** - the files or area in scope; with a spec, its folder and task ids.
- **Change** - the result to produce.
- **Constraints** - what must not change; no secrets, `.env` or private data.
- **Acceptance** - the check that must pass.
- **Working rules** - the block from SKILL.md, word for word.
- **Finish** - commit the work on the current branch with a clear message; do
  not push, merge or open a pull request; never run `git config`; send
  `worker_done` (without Orca: exit). When commits need a set author, start the
  driver with `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
  `GIT_COMMITTER_EMAIL` in its environment: some CLIs refuse `git -c ...`.
- When the job changes the CLI and docs are another lane's job, say in the
  spec which docs tests may fail until the lanes merge.

When the driver is done (Orca: `worker_done`; without Orca: its background
process ended):

1. Read its commits (`git -C <worktree> log -p <start>..HEAD`) and run the
   job's check yourself. Do not trust the driver's summary; the exit code
   does not decide the outcome, the commits and the check do.
2. Score it 0-100: 90+ done and clean; 70-89 done with small fixes; 40-69
   partly done or needed real fixes; below 40 wrong, broken or nothing. Score
   what it delivered, even when you fix it afterwards.
3. Record it, always, pass or fail (`ACC record`, see the brief).
4. Pass (and Scrutineering passed, when it applies): the lane's next job
   starts in the same worktree. Fail: Relay.

## Fresh tyres

Before every new attempt of a job, reset the lane worktree to the commit it had
before the job started:

```text
git -C <worktree> rev-parse HEAD   # record as <start> before the first attempt
git -C <worktree> reset --hard <start>
git -C <worktree> clean -fd
```

The next driver never inherits a failed attempt's edits or untracked files.

## Relay

A failed stint hands the job over:

1. Attempt 2: pick another model with `ACC pick --kind <k> --exclude <failed>`;
   same cost tier is allowed. Run on fresh tyres with the findings in the spec.
2. Attempt 3: one cost tier up (`--max-cost` raised by one step), still on
   fresh tyres.
3. After three failed attempts, fix the job yourself or stop the lane, and
   say which in the lane report.

## Scrutineering

Applies to **high-risk** jobs (and per pace below). A stint that passes your
check is not yet accepted.

### When it applies

- Risk `high`: always.
- Pace `push`: every job except `low` risk.
- Pace `eco` / `race`: only `high` risk.

### How

1. Pick two scrutineers: `ACC pick --kind review --exclude <driver>`, then
   `--exclude <first-scrutineer>` too, in the `agent:model` form (e.g.
   `--exclude opencode:provider/model-a`). Neither may share the driver's
   model family (`models.toml` notes say which): when the pick does, exclude
   it and pick again. They run in parallel in the lane worktree, read-only.
2. Fill `templates/scrutineer.md` with the diff range `<start>..HEAD`, the job
   spec path and the output file, and save it per scrutineer.
   - Without Orca: prompt file `../<repo>-acc-logs/<name>-prompt.md`, started
     like a driver, as a background process, with "Do the review in <its
     absolute path>"; output `../<repo>-acc-logs/<name>.md`.
   - Orca mode with `worker-start`: the filled prompt is the spec; findings
     come back in the `worker_done` message.
   - Orca mode with `orca terminal create` (an agent Orca does not know): no
     `worker_done` comes. Prompt file and output file as without Orca; wait as
     in background mode (each wait under 2 minutes) until
     `../<repo>-acc-logs/<name>.md` ends with its `VERDICT:` line.
3. Merge findings, drop duplicates and any you cannot confirm. Then do your
   own pass over the risky path: a "pass" with no findings is not proof.
4. Confirmed findings go back to the **same driver model** in a fresh session
   (a **pit stop**). It fixes on top of its work: no reset here. At most 2 pit
   stops, then pass or Relay. Record each pit stop as its own grade, with
   "(pit stop <n>)" in the job title.
5. Score each scrutineer as `--kind review`: confirmed findings count for it;
   false alarms and real problems it missed (found by you, the other
   scrutineer or the crew chief) count against it.

Fewer than two eligible scrutineers (by pace, `models.toml` or missing CLIs):
use the one available plus your own review, and say so in the lane report.

## Edge cases

- **Names.** `<job>` in file names is a short slug of the job title. A pit
  stop's spec is `<name>-pitstop-<n>.md`, where `<name>` is the standard name
  of the job it fixes.
- **Partly done, tests green.** If the work is usable but the spec is not
  fully met, the job goes to Scrutineering (when it applies) and a pit stop
  for what is missing. Relay with Fresh tyres is for work that is wrong or
  broken.
- **After a pit stop.** Review the pit-stop diff yourself. Run the
  scrutineers again only when the pit stop changed risky code they did not
  see.
- **A stalled scrutineer.** If a scrutineer writes nothing for 15 minutes,
  kill its tree, score it as a fail and go on with the other scrutineer plus
  your own pass.
- **Picking scrutineers.** Use the pace's `--max-cost`. On a high-risk job or
  at pace `push`, at least one scrutineer must have a `review` score of 70 or
  more. If none has one yet, take a model that scored well in the job's kind.
- **Docs jobs.** A scrutineer checks every claim against the code, the skill
  and the CLI's `--help`, and checks that each translation says the same.
- **Cross-lane checks.** A version bump or a CLI change in one lane can break
  another lane's tests until the lanes merge. Name those tests in the job
  spec.

## Starting and watching drivers

A driver runs in the lane worktree on the lane branch. Its prompt is the job
spec. Lane worktrees are created as children of the crew chief worktree
(`orca worktree create ... --parent-worktree <crew chief worktree selector>`,
e.g. `active`, `branch:<crew chief branch>` or `path:<path>`), so Orca shows
them under the run. Every driver and scrutineer is named with the standard name,
`acc-<slug>-<lane>-<job>-<role>` for the first attempt and
`acc-<slug>-<lane>-<job>-<role>-<n>` for the n-th, where `<slug>` comes from
the feature branch `acc/<slug>`, `<job>` is a short slug of the job title, and
`<role>` is `driver` or `scrutineer`. The engineer covers the whole lane, so
it has no `<job>`: `acc-<slug>-<lane>-engineer[-<n>]`. The Orca tab title and
the log file name are the same. Scrutineers running in parallel use `-1` and `-2`
(or higher attempt numbers) to stay unique.

### Orca mode

The race engineer always starts as an Orca worker in the existing lane
worktree, never as the crew chief's subagent. The worktree already exists, so
do not pass `--name` to `worker-start`; rename the terminal to the standard
title afterwards. The rename `<handle>` comes from the `worker-start`
receipt, `worker-show`, or `orca terminal list --worktree <lane worktree>
--json`.

```text
orca orchestration worker-start --spec "<engineer brief>" --worktree branch:<lane branch> --agent <agent> --model <model> [--effort <level>] --json
orca terminal rename --terminal <handle> --title acc-<slug>-<lane>-engineer --json
orca terminal create --worktree path:<worktree> --title acc-<slug>-<lane>-<job>-<role> --command "<driver command>" --json
orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 100000 --json
```

Use `worker-start` when Orca knows the agent (`--agent <id>`); use
`orca agent launch --surface terminal` for a lone terminal, or
`orca terminal create` with the driver's command line when it does not.
Rename fails for workers without a terminal: fall back to
`orca terminal create --title <standard name>` or note it in the lane report,
and rely on `worker-read`. When the Chat UI does not expose a terminal handle,
skip the rename and rely on `worker-read`. Keep each wait under 2 minutes: CLIs
kill longer commands. An empty result only means wait again. Answer questions,
handle every `worker_done`, ack it (`check --ack <delivery>`), then release the
worker. A driver can miss its spec on a cold start: after 5 minutes with no
commit, look at `orca orchestration worker-read --dispatch <id> --source auto
--json`. An empty prompt means it never started: `worker-stop` it and start the
job again, recording nothing.

### Background mode

Without Orca, write the job spec to `../<repo>-acc-logs/<name>.md` and start
the driver in the background from inside the lane worktree with the prompt "Do
the job in <its absolute path>" (a spec on the command line breaks on Windows).
Log `../<repo>-acc-logs/<name>.log`, PID in `../<repo>-acc-logs/<name>.pid`,
never inside the worktree:

```text
claude -p "<prompt>" --model <model> --dangerously-skip-permissions   # refused as root
codex exec -C <worktree> -s workspace-write -m <model> "<prompt>" < /dev/null
opencode run --standalone --auto -m <model> "<prompt>"
# Linux/macOS: start (job control off, so $! is the driver), then kill its group
set +m; setsid nohup <cmd> > "<log>" 2>&1 & echo $! > "<pidfile>"
kill -- -<pid>
# Windows (PowerShell): start, then kill the whole tree
$p = Start-Process cmd -ArgumentList '/c <cmd>' -WorkingDirectory <worktree> -PassThru -WindowStyle Hidden -RedirectStandardOutput "<log>" -RedirectStandardError "<log>.err"; Set-Content "<pidfile>" $p.Id -Encoding ascii
taskkill /PID <pid> /T /F
```

**Monitor.** Each wait stays under 2 minutes (a harness that blocks long sleeps
takes a background wait that ends when the driver exits). The progress signals
work in both modes: Orca `orca orchestration worker-read --dispatch <id>
--source auto --json`; background growth of the log and its `.err`, where
opencode writes its progress (`claude -p` prints only at the end). In every
mode also watch new commits (`git -C <worktree> log --oneline <start>..`) and
changed files (`git -C <worktree> status --porcelain`, newest mtime). Process
gone means the job ended: review it (Stint). 30 minutes with no commit, file
change or log growth: kill it; it is a failed attempt. A background driver
cannot ask you anything: keep specs self-contained.

## Risk

Set per job in the plan. Shown at the approval stop and in the report.

| Level | Examples |
| --- | --- |
| `low` | docs, typos, comments, pure refactor with tests |
| `normal` | features, bug fixes, new tests |
| `high` | auth/authz, money, data loss or migrations, concurrency, secrets handling, input validation on trust boundaries |

Only `high` adds Scrutineering by default; pace can raise that.

## Pace

Set per request, e.g. "ACC, do X, pace push". Default is `race`. Paid models
are used only when the user says so, at any pace.

| Pace | Drivers | Scrutineering | Scrutineers | Implied `--max-cost` |
| --- | --- | --- | --- | --- |
| `eco` | free only | high risk | free only | `free` |
| `race` | cheapest good (`ACC pick`) | high risk | free + included | `included` |
| `push` | may start at included | every job except low risk | free + included | `included` |

Pass the implied `--max-cost` to `ACC pick` unless the user set one explicitly.