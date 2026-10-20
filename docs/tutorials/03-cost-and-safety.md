# Cost and safety without surprises

This tutorial builds on [01-first-job.md](01-first-job.md). It uses the same small practice project, but focuses on choosing models, limiting spend, and protecting files.

The examples below use `CC` for the installed CLI:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

Use a lab-only global preferences file. Before **every** ACC command, export it. Do not point it at your normal preferences file.

## 1. Start with safe defaults

If you have not made the practice project yet, follow [01-first-job.md](01-first-job.md) through the first commit and installation. Then run setup with `--yes`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC setup --yes --project .
```

You should see a tier summary like this from the lab run:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

The exact counts change with the CLIs and models on your machine. `--yes` chooses no paid subscription assumption, a cheap-model budget of `0`, and no permission for free models that train on your traffic.

Without `--yes`, `setup` asks one question at a time: whether you pay for a Claude subscription, how much per day you allow for cheap models, and whether free models that train may be used. Run that form in a real terminal. If there is no terminal, setup exits without writing; use `--yes`.

## 2. Discover the cost of each executor

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
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

## 3. Let `pick` climb only when you allow it

Create a small job and piece as in the first tutorial. In the lab, the real pick was:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

For hands-on work (`code`, `tests`, `refactor`, `docs`, and `data`), `pick` stays with free executors. It does not climb just because one free executor failed. When no free executor remains, it tells you to split the piece or do it yourself.

For thinking work (`review`, `design`, `diagnosis`, and `research`), `pick` can continue up the ladder. The cheap tier is used only while both budgets allow it. A daily budget of `0` keeps cheap models closed. When no setup block or no usable cheap budget is available, the product message is: `run setup to allow cheap models`.

To deliberately allow execution work to reach the paid ladder, use `--strong`. It tries `included` and `cheap` before `paid`. This is an exception, not a retry switch:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --strong
```

Use `--coordinator <tool>` when you want ACC to know which CLI is coordinating the job. The coordinator's own CLI, or its subscription family, is placed last among `included` choices. `--allow-own-cli` removes that preference.

### Excluding free executors

`pick` accepts more than one `--exclude`. Copy every free executor id printed by `discover` when you want to see what the next tier would be. For example, exclude the free ids from your own `discover` output, then run:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

If this reaches an included or cheap executor, treat that output as a demonstration only. Do **not** dispatch it in this tutorial. Real dispatches here use only `opencode/*-free` or `opencode-go/*-free`.

## 4. Understand the spend controls

Set these in the global preferences file when you intentionally permit cheap models:

```toml
cheap_budget_usd_day = 2
cheap_budget_usd_job = 1
```

`cheap_budget_usd_day` is shared by all projects. `cheap_budget_usd_job` limits one job. A model rule can add its own limit:

```toml
[[models]]
match = "opencode:openrouter/*"
cost = "cheap"
cap_usd_day = 1
```

Every `cheap` or `paid` attempt appends one JSON line to `spend.jsonl`, next to the global preferences file. With the lab path above, that is `~/projects/hello-crew-lab/spend.jsonl` only as a neutral example of the location; your actual path is the directory containing the global preferences file. A line has the JSONL shape recorded by ACC, including the executor, project, job, cost tier, and amount. Do not copy a paid-run line from this tutorial: no paid dispatch was run here.

The check happens before the attempt. One attempt can exceed the daily ceiling by at most its own cost, and concurrent attempts can each pass before their combined spend is known. An unknown-cost cheap model gets one probe per executor per day unless you set `cap_usd_day`.

The economy alerts are different from model budgets. `close` and `economy` can alert when the coordinator used more than 50% of job tokens, when execution averages more than 2.5 attempts per piece, or when a job lasts more than 180 minutes. Set `alert_coordinator_share`, `alert_attempts_per_piece`, or `alert_job_minutes` to change them; `0` disables one alert. They also alert when the day's cheap spend reaches 80% of `cheap_budget_usd_day` (only when that budget is above 0). Read every `alert:` line before reporting the job.

## 5. Forbid models and training

A `[[models]]` rule can forbid a model. `forbid_training = true` also turns any matching rule with `trains = true` into `forbidden`:

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

The first matching rule wins. A `forbidden` rule wins even when another preferences file calls the model free. After changing project preferences, run discovery again:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC discover --project .
```

Then pick the piece. In the lab, after forbidding `opencode:opencode-go/beam-free`, `pick` did not choose it; it selected another free OpenCode model instead:

```text
opencode:opencode-go/longcat-2.5-preview-free
unrated, no history (opencode:opencode-go/longcat-2.5-preview-free); tier free
```

That is the useful safety property: a forbidden model is removed from the candidates, not merely placed later in the list.

## 6. Hide sensitive paths

Project preferences live in `docs/crew-chief/preferences.toml`. Add paths that an executor must not see:

```toml
sensitive_paths = ["secret.txt", ".env", "secrets/*"]
```

A path prefix also covers related names such as `.env.example`. Tracked matches are hidden from the execution worktree. ACC checks again after the run.

Create a harmless tracked fixture for this test, then mention it in a brief. The lab dispatch produced this real result:

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

Some apps write configuration or cache files in your home directory while they run. ACC already ignores its known agent-CLI folders. Add another harmless app entry when needed:

```toml
ignore_home_entries = [".someapp"]
```

This affects the outside-write check only. It does not make the app sandboxed. The lab also showed why you should inspect the result: a real free dispatch ended as `outside_write` with `outside_writes: home: Recent`. A clean exit from the executor is not enough to accept the attempt.

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

## What to remember

- Run `setup --yes` first, using the same lab global preferences path every time.
- Read `discover` before picking. It shows each executor's cost and training flag.
- Use free OpenCode executors for real dispatches in this tutorial.
- Use `--strong` only when you intentionally permit paid escalation.
- Keep cheap budgets explicit, and inspect `spend.jsonl` next to the global preferences file.
- Treat `brief_refused`, `outside_write`, and `destructive_suspect` as reasons to stop and inspect.

## Tutorial report

- **Files written:** this tutorial and `03-cost-and-safety.pt-BR.md`.
- **Commands run:** copied and initialized the example, made the first commit, installed ACC, ran `setup --yes`, ran `discover`, created a job and piece, ran `pick`, ran free OpenCode dispatches, and tested a forbidden model and a sensitive-path brief.
- **Product behavior to notice:** the first free OpenCode dispatch reported an unexpected home write (`home: Recent`) and was rejected as `outside_write`; a later free dispatch timed out; a brief naming a sensitive path was correctly rejected as `brief_refused`. Setup also requires a terminal for interactive mode, so use `--yes` in non-interactive automation.
