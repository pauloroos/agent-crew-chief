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
  not push, merge or open a pull request; send `worker_done` (plain: exit).

When the driver is done (Orca: `worker_done`; plain: its process ended):

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
   `--exclude <first-scrutineer>` too. Prefer a different model family
   (`models.toml` notes say which). They run in parallel in the lane worktree,
   read-only.
2. Each scrutineer reads the diff range `<start>..HEAD`, the job spec path and
   writes findings using the prompt in `templates/scrutineer.md`.
   - Plain mode: output to `../<repo>-acc-logs/<lane>-<job>-scrutineer-<n>.md`.
   - Orca mode: findings come back in the `worker_done` message.
3. Merge findings, drop duplicates and any you cannot confirm.
4. Confirmed findings go back to the **same driver model** in a fresh session
   (a **pit stop**). It fixes on top of its work: no reset here. At most 2 pit
   stops, then pass or Relay.
5. Score each scrutineer as `--kind review`: confirmed findings count for it,
   false alarms against it.

Fewer than two eligible scrutineers (by pace, `models.toml` or missing CLIs):
use the one available plus your own review, and say so in the lane report.

## Starting and watching drivers

A driver runs in the lane worktree on the lane branch. Its prompt is the job
spec.

### Orca mode

```text
orca orchestration worker-start --spec "<job spec>" --worktree branch:<lane branch> --agent <agent> --model <model> [--effort <level>] --json
orca orchestration check --wait --types "worker_done,escalation,question" --timeout-ms 100000 --json
```

Keep each wait under 2 minutes: CLIs kill longer commands. An empty result only
means wait again. Answer questions, handle every `worker_done`, ack it
(`check --ack <delivery>`), then release the worker. A driver can miss its spec
on a cold start: after 5 minutes with no commit, look at `orca orchestration
worker-read --dispatch <id> --source auto --json`. An empty prompt means it
never started: `worker-stop` it and start the job again, recording nothing.

### Plain mode

Write the job spec to `../<repo>-acc-logs/<lane>-<job>.md` and start the
driver in the background from inside the lane worktree with the prompt "Do the
job in <its absolute path>" (a spec on the command line breaks on Windows).
Log `../<repo>-acc-logs/<lane>-<job>.log`, PID in
`../<repo>-acc-logs/<lane>-<job>.pid`, never inside the worktree:

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

Monitor, each wait under 2 minutes: process alive, new commits (`git -C
<worktree> log --oneline <start>..`), changed files (`git -C <worktree> status
--porcelain`, newest mtime) and log growth (`<log>` and `<log>.err`, where
opencode writes its progress; `claude -p` prints only at the end). Process
gone means the job ended: review it (Stint). 30 minutes with no commit, file
change or log growth: kill the tree; it is a failed attempt. A plain driver
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