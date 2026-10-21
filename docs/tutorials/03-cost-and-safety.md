# Cost and safety without surprises

This tutorial builds on [01-first-job.md](01-first-job.md). It uses the same small practice project, but focuses on choosing models, limiting spend, and protecting files.

## Before you start

Choose a lab root: a directory of your own that holds the isolated global preferences file of this tutorial. Nothing here touches your normal ACC preferences.

The examples below use `CC` for the installed CLI:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

Before **every** ACC command, export the preferences path of the lab. Do not point it at your normal preferences file.

```bash
export LAB_ROOT="$HOME/projects/hello-crew-lab"
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
```

Every command block below repeats that export, so a new shell only needs `LAB_ROOT` set once.

On Windows, use the PowerShell section of tutorial 01 for every command here. Replace `$HOME` with your designated lab root, set `$env:AGENT_CREW_CHIEF_GLOBAL_PREFS` before every ACC command and invoke `python -I` directly: PowerShell does not expand a `$CC` string into a command. The [PowerShell on Windows](#powershell-on-windows) section at the end of this tutorial repeats every command of this tutorial in PowerShell form.

Two placeholders appear below. `JOB` is the id of the practice job you create in step 3, and `DEMO_JOB` is a second job used only for the ladder demonstrations of step 3. Replace both with the ids your `job new` printed.

Every real dispatch in this tutorial uses a free OpenCode executor: an id of the form `opencode:opencode/*-free` or `opencode:opencode-go/*-free`. Check the tier of the exact id in `discover` before every dispatch. A pick that returns anything else is a demonstration only — do not dispatch it.

## 1. Start with safe defaults

If you have not made the practice project yet, follow [01-first-job.md](01-first-job.md) through the first commit and installation. Then run setup with `--yes`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC setup --yes --project .
```

You should see a tier summary like this from the lab run:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 66
```

The exact counts change with the CLIs and models on your machine. `--yes` answers no to the subscription questions it cannot infer, sets a cheap-model budget of `0`, and does not allow free models that train on your traffic. Credentials found on the machine still count: a CLI whose account is already signed in can be classified as `included` without any question. So `--yes` does not restrict every candidate to `free` — read the `discover` output of step 2 before you pick.

Setup writes one managed block in the global preferences file, between `# >>> acc setup >>>` and `# <<< acc setup <<<`. This is the block of the lab run; yours lists the CLIs your machine has:

```toml
# >>> acc setup >>>
cheap_budget_usd_day = 0
forbid_training = true

[[models]]
match = "codex:*"
cost = "included"

[[models]]
match = "claude:*"
cost = "included"
# <<< acc setup <<<
```

The scalar settings sit above the `[[models]]` tables on purpose. In TOML a key written after a table belongs to that table, so a budget key pasted at the end of the file would be read as part of the last rule and ignored. Keep every scalar setting — the budget keys, `forbid_training`, `sensitive_paths`, `ignore_home_entries` — above the first `[[models]]` or `[[tools]]` table.

Without `--yes`, `setup` asks one question at a time: whether you pay for a Claude subscription, how much per day you allow for cheap models, and whether free models that train may be used. Run that form in a real terminal. If there is no terminal, setup exits without writing; use `--yes`.

## 2. Discover the cost of each executor

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC discover --project .
```

The lab printed entries in this form:

```text
opencode:opencode-go/beam-free  cost=free  trains=no
opencode:opencode-go/minimax-m3.1-flash-free  cost=free  trains=no
claude:sonnet  cost=included  trains=no
skipped: codex: no models listed
skipped: cursor: no models listed

models: baseline: 91 executors recorded

work types: opencode: all types, first on execution work
```

The complete output also listed paid OpenCode models and free Antigravity models. Do not copy the counts or model list into a permanent document: providers change them.

`discover` is your price check. `free` means no money and no subscription. `included` means a subscription you already pay for. `cheap` is pay-per-use within a budget. `paid` is the strong paid fallback. `forbidden` is not on the ladder.

Before every real dispatch in this tutorial, check two things in this output: the `cost=` field of the exact id, and the shape of the id itself. Only `opencode:opencode/*-free` and `opencode:opencode-go/*-free` are used for real dispatches here.

## 3. Let `pick` climb only when you allow it

`pick` climbs a ladder, cheapest first: `free`, then `included` (a subscription you already pay for), then `cheap` (pay per use, only while the budget allows) and last `paid`. A tier is left only when nobody is available in it — never because a free executor merely failed.

- For hands-on work (`code`, `tests`, `refactor`, `docs` and `data`), `pick` tries the free executors first. When no free executor is left it can still select an `included` executor, or a `cheap` one while the budget allows, without `--strong`. Only `paid` execution work needs `--strong`.
- For thinking work (`review`, `design`, `diagnosis` and `research`), `pick` keeps climbing the same ladder and can reach `paid` on its own.

In this tutorial, treat every non-free selection as a demonstration only. Do not dispatch it.

### Two jobs: the real one and the demonstration one

`pick` records every pick in the job, and a `--strong` pick records the escalation too, even when no dispatch follows. `close` can later print that escalation in the delivery comment. To keep the delivery record of the real job clean, run the ladder experiments on a separate demonstration job.

The demonstration job needs its own request file:

```bash
printf '%s\n' 'Demonstration only: compare the cost tiers of pick. Do not dispatch this piece.' > .crew-chief-demo-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC job new --project . --title "Cost ladder demo" --request-file .crew-chief-demo-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC piece add --project . --job DEMO_JOB --title "Show the cost ladder" --type code --risk low
```

`job new` prints the id: use it as `DEMO_JOB` from now on. `piece add` prints `1`, the piece number of the demonstration job.

### The normal job

The practice job is created here, with the same request as tutorial 01. If you already created it there, reuse its id as `JOB` and skip these two commands; otherwise run them and use the printed id as `JOB`:

```bash
printf '%s\n' 'Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test.' > .crew-chief-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

`job new` prints the id: use it as `JOB` from now on. `piece add` prints `1`, the piece number used by every pick and dispatch below.

### The normal pick

Pick the piece of the real job:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
```

The lab picked a free OpenCode executor, with the tier on the second line:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use the exact executor id printed on the first line. That is the id a dispatch must use.

### The `--strong` demonstration

`--strong` leaves every free executor out of the pool and is the only way execution work reaches a paid executor. It tries `included` and `cheap` before `paid`. This is an exception, not a retry switch, and it belongs to the demonstration job:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job DEMO_JOB --piece 1 --strong
```

The lab printed a shape like this:

```text
claude:opus
second chance: unrated claude:opus; tier included; climbed: free exhausted
```

`climbed: free exhausted` says the pick had to leave the free tier. It does not mean the free tier was the last one available: `included` comes next, then `cheap` while the budget allows, then `paid`. No dispatch follows this command, and the escalation is now recorded in `DEMO_JOB`.

Use `--coordinator <tool>` when you want ACC to know which CLI is coordinating the job. The coordinator's own CLI, or its subscription family, is placed last among `included` choices. `--allow-own-cli` removes that preference.

### Excluding executors

Each `--exclude` adds ids, so repeating the option accumulates them: `--exclude ID_1 --exclude ID_2` leaves out both. A single option with several ids works too (`--exclude ID_1 ID_2`). Copy every free executor id printed by `discover` when you want to see what the next tier would be:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job DEMO_JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

If this reaches an included or cheap executor, treat that output as a demonstration only. Do **not** dispatch it in this tutorial. Real dispatches here use only `opencode/*-free` or `opencode-go/*-free`.

### Reviewers stay outside the author's family

`pick --review` treats the choice as thinking work and leaves out the quota family of every executor with an `ok` attempt on the piece. For OpenCode, the two routes to the same model are one family: `opencode:opencode/minimax-m3.1-flash-free` and `opencode:opencode-go/minimax-m3.1-flash-free` name the same model, so ACC leaves both out by itself. You do not have to exclude the author's alias by hand to get a reviewer from another model.

## 4. Understand the spend controls

Set these in the global preferences file when you intentionally permit cheap models:

```toml
cheap_budget_usd_day = 2
cheap_budget_usd_job = 1
```

Keep them above the first `[[models]]` or `[[tools]]` table of that file, as the setup block of step 1 does. A budget key pasted after a table changes its scope instead of setting the budget.

`cheap_budget_usd_day` is shared by all projects. `cheap_budget_usd_job` limits one job. A model rule can add its own limit:

```toml
[[models]]
match = "opencode:openrouter/*"
cost = "cheap"
cap_usd_day = 1
```

A zero daily cheap budget closes the cheap tier only. It does not prohibit included or paid executors. A zero per-job cheap budget means no additional job ceiling. For this tutorial, verify both the free tier in discover and the opencode/*-free or opencode-go/*-free model id before every real dispatch.

Every `cheap` or `paid` attempt appends one JSON line to `spend.jsonl`, next to the global preferences file. With the lab path above, that is `$LAB_ROOT/spend.jsonl` only as a neutral example of the location; your actual path is the directory containing the global preferences file. A line has the JSONL shape recorded by ACC, including the executor, project, job, cost tier, and amount. Do not copy a paid-run line from this tutorial: no paid dispatch was run here.

The check happens before the attempt. One attempt can exceed the daily ceiling by at most its own cost, and concurrent attempts can each pass before their combined spend is known. An unknown-cost cheap model gets one probe per executor per day unless you set `cap_usd_day`.

The economy alerts are different from model budgets. `close` and `economy` can alert when the coordinator used more than 50% of job tokens, when execution averages more than 2.5 attempts per piece, or when a job lasts more than 180 minutes. Set `alert_coordinator_share`, `alert_attempts_per_piece`, or `alert_job_minutes` to change them; `0` disables one alert. They also alert when the day's cheap spend reaches 80% of `cheap_budget_usd_day` (only when that budget is above 0). Read every `alert:` line before reporting the job.

## 5. Forbid models and training

A `[[models]]` rule can forbid a model. `forbid_training = true` also turns any matching rule with `trains = true` into `forbidden`. Add these rules to the project's `docs/crew-chief/preferences.toml`, with the scalar setting above the first table:

```toml
forbid_training = true

[[models]]
match = "opencode:opencode-go/beam-free"
cost = "forbidden"
trains = false

[[models]]
match = "*-preview-free"
cost = "free"
trains = true
```

Put exact model rules before broader rules that also match them: among ordinary cost rules, the first matching rule wins. An explicit `cost = "forbidden"` rule always wins, even if a broader free rule appears first or another preferences file calls the model free. The `*-preview-free` rule above does not match `beam-free`; it marks matching preview models as training. After changing project preferences, run discovery again:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC discover --project .
```

Then pick the piece of the real job. The forbidden model is removed from the candidates, not merely placed later in the list. An illustrative output, with a model that is free and does not train on your traffic:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
```

```text
opencode:opencode-go/minimax-m3.1-flash-free
unrated, no history (opencode:opencode-go/minimax-m3.1-flash-free); tier free
```

That is the useful safety property: a forbidden model is removed from the candidates.

With `forbid_training = true`, any model that a matching rule marks `trains = true` is forbidden too, so `pick` will not select it; the `*-preview-free` rule above is one such rule. This guard is rule- and flag-based: check the `trains=` field in `discover` for the flag ACC actually holds for a model. It does not by itself assert how a provider treats every request, only that ACC excludes the models it has flagged as training on your traffic.

## 6. Hide sensitive paths

Project preferences live in `docs/crew-chief/preferences.toml`. Add paths that an executor must not see, above the first `[[models]]` or `[[tools]]` table of that file:

```toml
sensitive_paths = ["secret.txt", ".env", "secrets/*"]
```

A path prefix also covers related names such as `.env.example`. Tracked matches are hidden from the execution worktree. ACC checks again after the run.

Create a harmless tracked fixture for this test, then mention it in a brief. `FREE_EXECUTOR` is the exact id from the fresh pick at the end of step 5, which already ran after the model policy edit; if you changed any `[[models]]` rule since, run that pick again and use the new id. The `--strong` and `--exclude` results of step 3 belong to `DEMO_JOB` and are not dispatch candidates. Check in `discover` that the id has `cost=free` and the form `opencode:opencode/*-free` or `opencode:opencode-go/*-free`, then copy it exactly:

```bash
printf '%s\n' 'Harmless tutorial fixture; no real secret.' > secret.txt
git add secret.txt
git commit -m "Add harmless sensitive-path fixture"
printf '%s\n' 'Read secret.txt and implement the requested shout(text) helper.' > .crew-chief-sensitive.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-sensitive.md --timeout 300
```

The lab dispatch produced this real result:

```text
status: brief_refused
branch: -
worktree: -
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 0.0s
steps: -
tokens: -
```

The status is the important part. ACC refused the brief before starting an executor because it named a sensitive path. Never put the contents of a secret in a brief.

A sensitive file should also be absent from the executor's worktree. If a run creates or restores it, the post-run guard notes that on the attempt. Inspect `outside_writes`, `destructive_suspect`, and the recorded attempt before accepting any result.

## 7. Ignore harmless home-directory entries

Some apps write configuration or cache files in your home directory while they run. ACC already ignores its known agent-CLI folders, and the ones the system updates by itself. Add another harmless app entry when needed, above the first `[[models]]` or `[[tools]]` table of the project preferences:

```toml
ignore_home_entries = [".someapp"]
```

This affects the outside-write check only. It does not make the app sandboxed.

Now the real dispatch. Write the brief, pick a fresh free OpenCode executor, copy the exact id it prints, and dispatch it:

```bash
printf '%s\n' 'Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run python -m pytest -q.' > .crew-chief-brief.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Use the exact executor id from the last `pick`, and check in `discover` that it is a free OpenCode model of the form `opencode:opencode/*-free` or `opencode:opencode-go/*-free`. If the dispatch ends as `timeout`, read `docs/crew-chief/jobs/JOB.json` to see the attempt, then pick again with `--exclude` for that executor and dispatch the next free OpenCode model.

Never accept an attempt that ended as `outside_write` or `destructive_suspect`: inspect the worktree the dispatch left behind first. A clean exit from the executor is not enough to accept the attempt.

The lab history shows why you inspect the result. An older free dispatch was rejected as `outside_write` with `outside_writes: home: Recent`. That entry is ignored by ACC now, and `.docker` (written by Docker Desktop on its own) is ignored too; other writes are still reported.

## 8. Add your own CLI

For a Claude-Code-like CLI, add a tool entry and keep the secret in an environment variable:

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

`bin` must be on `PATH`. ACC records the variable name, never its value. Add a `[[models]]` rule if the CLI has a free model. A generic CLI can use `run_cmd` and `list_cmd`; see the README's **Adding a custom CLI** section.

This is an optional configuration example. If you have no such CLI on PATH, do not install a proxy or enter credentials for this tutorial. Discovery will skip the placeholder, with a line like `skipped: my-claude-proxy: bin 'my-claude-proxy' not found on PATH`, and that is expected.

After adding the optional entry, check discovery again:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC discover --project .
```

## What to remember

- Run `setup --yes` first, using the same lab global preferences path every time.
- Read `discover` before picking. It shows each executor's cost and training flag.
- The ladder is free, then included, then cheap within the budget; only `paid` execution needs `--strong`, while thinking work can reach `paid` on its own.
- Use free OpenCode executors for real dispatches in this tutorial; a non-free pick is a demonstration.
- Use `--strong` only when you intentionally permit paid escalation.
- Keep cheap budgets explicit, and inspect `spend.jsonl` next to the global preferences file.
- Treat `brief_refused`, `outside_write`, and `destructive_suspect` as reasons to stop and inspect.

## Tutorial report

- **Files written:** this tutorial and `03-cost-and-safety.pt-BR.md`.
- **Commands run:** the recorded lab run copied and initialized the example, made the first commit, installed ACC, ran `setup --yes`, ran `discover`, created a job and a piece, ran `pick`, ran free OpenCode dispatches, and tested a forbidden model and a sensitive-path brief. This revision was not re-run live. Outputs introduced as lab or recorded are quoted from that run; the post-policy `pick` in step 5 and the `--strong` shape in step 3 are illustrative examples of the format and tier line, not verbatim recorded output.
- **Product behavior to notice:** a brief naming a sensitive path is correctly rejected as `brief_refused`, before any executor starts; a free dispatch can also end as `outside_write`, and the recorded attempt must be read before the result is accepted. Setup requires a terminal for interactive mode, so use `--yes` in non-interactive automation, and `--yes` does not forbid `included` executors that the machine's own credentials prove.

## PowerShell on Windows

Every command block above is Bash. On PowerShell, set the lab root and the preferences path before **every** ACC command, and call the installed CLI with `python -I` — PowerShell does not expand a `$CC` string into a command.

```powershell
$LabRoot = Join-Path $HOME 'projects/hello-crew-lab'   # your designated lab root
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
```

Setup and discovery:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
```

The normal job (write the request file first, then create the job and the piece). `job new` prints the id: use it as `JOB`; `piece add` prints `1`:

```powershell
$Request = "Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-request.md'), "$Request`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

The demonstration job:

```powershell
$DemoRequest = "Demonstration only: compare the cost tiers of pick. Do not dispatch this piece."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-demo-request.md'), "$DemoRequest`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py job new --project . --title "Cost ladder demo" --request-file .crew-chief-demo-request.md
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py piece add --project . --job DEMO_JOB --title "Show the cost ladder" --type code --risk low
```

`[IO.File]::WriteAllText` with `UTF8Encoding($false)` writes UTF-8 without a byte-order mark. `Set-Content -Encoding UTF8` adds a BOM on Windows PowerShell 5.1, and a BOM in the first line of a brief is noise you do not need.

The picks:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job DEMO_JOB --piece 1 --strong
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job DEMO_JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

After the model policy edits of step 5, run discovery again and the fresh normal pick. Check the exact id in `discover` is `cost=free` and has the form `opencode:opencode/*-free` or `opencode:opencode-go/*-free`, then copy it as `FREE_EXECUTOR`:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
```

The sensitive-path fixture and dispatch:

```powershell
$Fixture = "Harmless tutorial fixture; no real secret."
[IO.File]::WriteAllText((Join-Path (Get-Location) 'secret.txt'), "$Fixture`n", (New-Object System.Text.UTF8Encoding($false)))
git add secret.txt
git commit -m "Add harmless sensitive-path fixture"
$Sensitive = "Read secret.txt and implement the requested shout(text) helper."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-sensitive.md'), "$Sensitive`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-sensitive.md --timeout 300
```

The real dispatch of step 7:

```powershell
$Brief = "Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run python -m pytest -q."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-brief.md'), "$Brief`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

After adding the optional CLI entry of step 8, check discovery again:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
```

The variable lives in the current session only. Set `$LabRoot` and the preferences path again in every new window, and never point the preferences path at your normal ACC preferences file.
