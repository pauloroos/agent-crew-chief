# Your first job in 10 minutes

This tutorial uses a tiny Python project and a real ACC job. You will ask a free executor to add `shout(text)`, have a different free model review it, then accept the result.

## 1. Copy the practice project

From the ACC repository, copy the example to a new directory and enter it:

```bash
cp -R examples/hello-crew ~/projects/hello-crew
cd ~/projects/hello-crew
git init
git add .
git commit -m "Start hello-crew practice project"
```

You should see a new Git repository and a first commit. If Git asks for your name and email, configure them for this practice repository, then repeat the commit. ACC needs its own Git repository because it uses isolated worktrees. On Windows, use the [PowerShell on Windows](#powershell-on-windows) section instead of the Bash blocks.

## 2. Install ACC

Clone a tagged ACC release somewhere outside the practice project, then install it into the copy:

```bash
git clone --branch v0.3.4 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py .
```

You should see the skill copied to `.claude/skills/crew-chief` and `.agents/skills/crew-chief`, plus the project files created by the installer. If the clone fails, download the release archive instead and run its `install.py`.

The commands below use this short form for the CLI:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

## 3. Set up the cost tiers

Choose a preferences file inside the practice project. This keeps the tutorial isolated from your normal ACC settings. Before **every** ACC command, export this variable:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC setup --yes --project .
```

You should see a tier summary like `your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 66`, from one test machine. Those numbers are an example, not a promise: the counts change as providers add or remove models, and they differ on every machine because they depend on the CLIs installed there. What matters is that the free count is above zero. `--yes` accepts the safe defaults: no subscription assumption, a zero cheap-model budget, and no training permission.

If your summary has no free OpenCode executor, stop here and configure one before dispatching. Do not use an included, cheap, or paid executor in this tutorial.

## 4. Discover executors

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC discover --project .
```

You should see free OpenCode entries such as `opencode:opencode-go/beam-free`, followed by lines for skipped tools and the work types. The exact model list changes over time. If you see `run setup to configure cost tiers`, use the same preferences path for `setup`; a different filename is ignored.

## 5. Create a job

Write the request exactly as you want it recorded:

```bash
printf '%s\n' 'Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test.' > .crew-chief-request.md
```

Register it:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
```

You should see a job id such as `20261020-1437-add-shout-helper`. Your id will differ. Save it as `JOB` in your shell, or replace `JOB` below with the printed id.

## 6. Split the work

This request is small enough for one piece:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

You should see `1`. That is the piece number. If the request grows, add one piece per independently reviewable change.

## 7. Pick a free executor

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

This OpenCode execution pick prints two lines:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

The first line is the executor. The second explains why it was chosen and confirms its tier. Use the exact executor id printed on the first line. A third line, `effort: low`, is printed only when the pick has an effort value: ACC prints the effort line when the chosen executor has one. OpenCode tools have no effort flag by default, so an OpenCode pick shows the two lines above. A tool configured with an effort flag, such as Codex or Antigravity, can show that third line (step 10 shows one). If `pick` selects a non-OpenCode free model for execution, pass `--exclude` for it and pick again. Never dispatch an included, cheap, or paid model in this tutorial.

## 8. Write the brief

The brief is the whole contract for the executor:

```bash
printf '%s\n' 'Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run the existing test command.' > .crew-chief-brief.md
```

Mention the allowed files, the expected behavior, and the test command. Do not put secrets or personal paths in the brief.

## 9. Dispatch

Replace `EXECUTOR` with the first line from `pick`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

You should see `status: ok`, a `branch`, a `worktree`, and duration and token lines. If you see `status: timeout`, read the recorded attempt before retrying. Pick another free OpenCode model with `--exclude`, then dispatch again.

## 10. Review with a different free model

Pick a reviewer first:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review
```

The output should name a free executor from a different family and explain `reviewer outside the family of`. A review pick for a tool that takes an effort flag has three lines:

```text
antigravity:claude-opus-5-5-low
unrated, no history (antigravity:claude-opus-5-5-low); tier free; reviewer outside the family of: opencode:opencode-go/minimax-m3.1-flash-free
effort: low
```

The third line is the effort level. It shows only for tools that take an effort flag, so a review pick for an OpenCode model has the first two lines alone. Use the exact id from the first line as `REVIEWER`. Write the review brief:

```bash
printf '%s\n' 'Review the last successful attempt for the shout(text) request. Check the diff and tests. Report only concrete missing requirements or defects; do not edit files.' > .crew-chief-review.md
```

Dispatch the review using the `worktree` printed by the successful implementation dispatch:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

You should see `status: ok` and an `answer:` line. If review fails with quota, pick another free reviewer and use `--exclude` for the failed one.

## 11. Grade, accept, and close

If the review found no corrections, grade the work like this:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 4 --missing 0 --extra 0
```

You should see `score: 100`. If the reviewer found a real defect, record it with `correction`, dispatch only that adjustment from the last successful attempt, review again, and grade each executor that worked on the piece.

Accept the last successful attempt:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

You should see `accepted` and a branch name. Close the job:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC close --project . --job JOB --type feat
```

You should see a commit message, the executor and score, the record path, and an economy block. The economy line looks like this:

```text
free work: 100% of pieces, 100% of recorded tokens · coordinator: not recorded · 2.0 attempts/piece · mean score 100
```

With no usage entries, the summary shows coordinator: not recorded; this is not a measured zero. The free percentage covers recorded tokens only and does not establish the total session cost. Use usage add to record coordinator usage; an explicit zero-token entry retains coordinator: 0% when there are execution tokens.

The exact numbers depend on your run. Read it as a health check: how much work stayed free, how much coordinator usage was recorded, how much retrying happened, and the average score.

> **What just happened?** ACC recorded the request, isolated the implementation in a Git worktree, chose a free OpenCode executor, ran a second model as a reviewer, recorded a grade, merged the accepted branch, and printed a delivery record plus economy information. The coordinator still decides what to ask for and whether the review is right.

## Troubleshooting

### `__pycache__` appears in the job

The example includes a `.gitignore` with `__pycache__/` and `.pytest_cache/`. If you copied only the source files, copy the whole example again, including `.gitignore`, and commit it before installing ACC.

### `run setup to configure cost tiers`

ACC did not find the managed setup block in the preferences file. Use the exact same path for every command:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC setup --yes --project .
```

A file named `preferences.toml` is not the same as the `global-prefs.toml` of this practice project. Do not use your normal home preferences file for this tutorial.

### `review requires --target` or `review requires --brief-file`

A review needs both the successful attempt's worktree and a review brief. Run `pick --review`, save the successful `worktree` path from the implementation dispatch, create `.crew-chief-review.md`, and pass both `--target WORKTREE` and `--brief-file .crew-chief-review.md`.

### `no free executor left`

Execution work must stay with free executors. Run `discover` again, then use `pick` with `--exclude` for failed executors. Do not add `--strong` for this tutorial.

### Review returns `status: quota`

The reviewer hit its own rate limit, so the review never ran. Read the recorded attempt to see which reviewer failed and which implementation worktree is the good one. The job record keeps every attempt:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
cat docs/crew-chief/jobs/JOB.json
```

The `attempts` list of piece 1 has one entry per try. Look for the `exec` attempt with `"status": "ok"` and copy its `worktree` value, and for the `review` attempt with `"status": "quota"` and copy its `executor`. Then pick another free reviewer, leaving the failed one out, and dispatch the review again against the same successful worktree:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude FAILED_REVIEWER
$CC dispatch --project . --job JOB --piece 1 --executor NEW_REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

Use the exact id printed by `pick` as `NEW_REVIEWER`, and the worktree of the successful implementation as `WORKTREE`.

Some reviewers share one quota pool: the Antigravity models of a vendor draw from the same pool, and a subscription model shares its quota with every other model of that CLI. When one reviewer of a pool fails, another of the same pool can fail too. In that case, exclude the whole pool and review with a free OpenCode model instead. ACC also pauses a reviewer that returned `quota` for a while, so a fresh `pick` reports it as `skipped for quota` and moves on by itself.

### Tests do not run

The example has no runtime dependencies, but the test command needs `pytest`. Install it in your own environment, then run `pytest -q`. If your shell does not expose the `pytest` executable, use the Python environment where you installed it.

## PowerShell on Windows

Every command block above is Bash. On PowerShell, copy the example with `Copy-Item -Recurse` instead of `cp -R`:

```powershell
Copy-Item -Recurse examples/hello-crew (Join-Path $HOME 'projects/hello-crew')
Set-Location (Join-Path $HOME 'projects/hello-crew')
git init
git add .
git commit -m "Start hello-crew practice project"
if ($LASTEXITCODE -ne 0) { throw 'Commit failed; configure repository-local identity and retry before install.py.' }
```

If the commit failed, set the identity for this repository only and repeat the commit:

```powershell
git config user.name "Your Name"
git config user.email "you@example.com"
git commit -m "Start hello-crew practice project"
```

PowerShell does not expand a `$CC` string into a command, so call the installed CLI directly with `python -I`. Set the preferences path before **every** ACC call, with the same path the rest of this tutorial uses:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
```

After that, every other ACC command follows the same shape, for example:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
```

The variable lives in the current session only. Set it again in every new window, and never point it at your normal ACC preferences file.
