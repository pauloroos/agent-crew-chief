# ACC central Implementation Plan

**Goal**: Coordinate repository worktrees and follow their progress, questions and recorded delegation cost from one place.

**Architecture**: File registry and supervised resumable rounds; worktree jobs stay authoritative. Terminal and web share one read-only snapshot.

**Tech Stack**: Python 3.11+ stdlib, Git, existing pytest development tests; Claude Code/Codex, optional Orca later.

**Spec**: [spec.md](spec.md)

**Status**: Approval draft; phase 1 writes/imports documentation only.

## Execution contract

Coordinate with a separate released ACC v0.3.4 installation, always targeting this
development project; never run development scripts as the coordinating tool.
After approval, render one brief from `crew.toml`, pick a free executor, dispatch
to its isolated worktree, run proof, review with a different underlying model
family, record corrections/grades and accept. Verify the review's HEAD. Only
accept checks tasks. Never bypass dependency/proof gates. The coordinator owns
the plan, briefs and judgments; executors do hands-on work.

For each behavior, create focused tests, demonstrate the intended failure, then
implement only allowed files and run its proof. Test support belongs to that
task's files. Proofs may import accepted prerequisites and baseline modules;
none may require a future task's test/fixture/module or a real CLI. Correct a
bad proof rather than expanding scope. Use small human-authored commits.

## Global Constraints

- Python 3.11+ stdlib only at runtime; Git is the only required external tool.
- Support Windows, Linux and macOS; pytest is a development-only dependency.
- No AI co-author trailers in commits or generated commit instructions.
- Read UTF-8 with optional BOM and CRLF; preserve existing BOM and newline style when editing text.
- Tests use temporary HOME/USERPROFILE, AGENT_CREW_CHIEF_GLOBAL_PREFS and AGENT_CREW_CHIEF_CENTRAL_DIR; never touch real ~/.config or invoke live model CLIs.
- No private data, client names, credentials or personal paths in committed artifacts.
- Never merge/push coordinator branches or remove their worktrees automatically.
- Preserve v0.3.4 SDD, free-first selection, spending gates and job history.
- Every task owns its proof/test support; prerequisites must already be accepted, never supplied by a later task.
- FR/SC references trace shared requirements; assess only this task's declared deliverable and verification boundary, without implementing neighboring tasks.

## Existing seams and module structure

Under `skill/crew-chief/scripts/crew_chief/`, keep flat focused modules as in the
current package. `cli.py` delegates new commands to `central_cli.py`.
`records.py` already provides job locks/atomic writes and usage. `worktree.py`
owns executor attempt worktrees, distinct from new long-lived coordinator
worktrees. `economy.py` owns existing per-job metric semantics; `tools.py` parses
executor usage. Preserve `config.py`, `scoring.py`, `tiers.py` and `dispatch.py` behavior except
the explicitly scoped preference/quota integration. `install.py` currently copies
project skills: command-only installation must be a separate route.

The new modules and public functions are assigned exactly once in `crew.toml`;
later extensions explicitly depend on their owners. Test files sit under `tests/`
and may use the existing conftest's temporary homes/global preferences. No shared
future fixture is needed. Static web assets belong under `skill/crew-chief/web/`.
Existing CI covers Windows/Linux, Python 3.11/3.13; add macOS at the core gate.

Release gates also own current-release pins in both READMEs and first-job
tutorials. `tests/test_docs.py` currently hardcodes v0.3.4: T030 must separate
current-version assertions from historical changelog coverage, make active pins
follow the package version, and retain historical release assertions. These
files are explicitly allowed at that gate so a release proof never needs an
out-of-scope documentation change.

## Delivery split and gates

| Version | Implementation phases | Delivery | Gate |
| --- | --- | --- | --- |
| 0.4.0 | 1–5 | Registry, messages, launch, Claude/Codex resume, lifecycle, metrics, terminal and `acc` installation | Core fake-CLI end-to-end proof, full regression suite, three-OS CI, owner review |
| 0.4.1 | 6–8 | Local web page and optional Orca backend | Separate owner go-ahead after core; HTTP/action/backend proofs and regression suite |

All tasks are imported for traceability. ACC enforces task/phase dependencies.
The boundary after phase 5 is an additional **manual owner gate**: stop and
report; do not dispatch T031 without separate authorization. This session stops
before T001. Input section 7's SDD shipped in v0.3.x and is not reimplemented.

## Configuration and registry

`central_config.py` resolves `--central-dir`, then
`AGENT_CREW_CHIEF_CENTRAL_DIR`, then `~/.config/agent-crew-chief/central/`.
Read existing preferences without writing them. New scalar keys:
`coordinator_default = ""`, `coordinator_unrestricted = true`,
`central_poll_s = 1.0`, `central_round_timeout_s = 900`,
`central_backend = "default"`. Launch flags override project/global scalars.
Empty coordinator default falls back to `CREW_CHIEF_COORDINATOR`; unknown is an
error. Validate finite positive timings. Reuse existing `alert_*` and
`quota_cooldown_min`; choosing a coordinator never loosens executor tier gates.

```text
central/
  registry.json                    # schema_version:1; coordinators map keyed by UUID
  registry.lock/                   # bounded mkdir lock, owner nonce and time
  quotas/<family-hash>.json        # family, observed_at, cooldown_until, source ID
  coordinators/<uuid>/
    request.md
    supervisor.json               # nonce, PID, child PID, lease and round
    rounds/<round-uuid>.json       # state, session, usage, timestamps and log refs
    logs/<round-uuid>.stdout.log
    logs/<round-uuid>.stderr.log
    inbox/<message-uuid>.json
    outbox/<message-uuid>.json
    acknowledgments/<message-uuid>.json
    control/<command-uuid>.json
```

Registry record: `schema_version`, `id`, `repo`, `worktree`, `branch`,
`base_commit`, `backend`, `cli`, `cli_version`, `acc_version`, `session_id`
(initially null), `mode`, `authorization` (approval/raw flag, request digest,
UTC time), `job_ids`, `active_job_id`, `state`, `created_at`, `updated_at`,
`last_error`, `supervisor_nonce`. Absolute paths are local runtime data and must
never enter committed examples. No credentials/environment values are stored.
Validate UUIDs, paths under root, traversal/symlink escapes and schema versions;
unknown versions produce a per-row error rather than being overwritten.

Use same-directory temp files and `os.replace` under a bounded registry lock.
Reuse `records.write_atomic`/`file_lock` where suitable. Central owns schema,
read bounds and recovery rules. Expired locks whose ownership cannot be proved
require explicit recovery: age or a PID alone does not authorize deletion or
signalling. Reads tolerate BOM/CRLF. Atomic UUID-addressed files avoid concurrent
append races. Cap registry reads at 8 MiB and per-record/event reads at 1 MiB;
report oversized data rather than blocking the other rows.

## File messages and coordinator round protocol

Envelope: `{schema_version:1, id, coordinator_id, direction, kind, created_at,
reply_to, task_refs, body}`. Direction is `in`/`out`; kinds are `message`,
`question`, `answer`, `status`, `control`. `reply_to` is nullable. Body is plain
text up to 64 KiB UTF-8, envelope up to 80 KiB. Filename must equal the validated
ID. Ack files map message ID to consumed round ID/time; preserve envelopes as
history. Delivery is at least once with idempotent consumption: replay unacked
IDs, include them in prompts, deduplicate replies/effects. Do not promise
transactional exactly-once model execution after a crash.

`coordinator_protocol.py` renders bounded context: request/mode, worktree/job
refs, existing ACC cycle, new messages and control state. Ask for a structured
final `ACC_ROUND` JSON result: `state`, `active_job_id`, `job_ids`,
`blocked_task_refs`, `questions`, `answers`, `consumed_message_ids`. Adapters
first parse CLI event envelopes, then extract that result only from assistant
output, never arbitrary tool text. Validate IDs/state. A malformed result fails
visibly, without automatic retry. Questions carry stable IDs, task refs/reason;
answers carry `reply_to`. Continue independent ready tasks and return `waiting`
only when none remains eligible.

On the first round, approved-spec/direct-spec modes create a job and import the
selected spec in the coordinator worktree using the released ACC. Approved mode
verifies the recorded approval digest covers spec.md, plan.md, tasks.md and
crew.toml, not just the request text; modifying approved scope returns a question.
Raw-request mode explicitly authorizes the coordinator to refine/write its spec
there and proceed. Proportional SDD still permits trivial/small requests without
a project spec. A job ID is reported as soon as created so usage can be backfilled.

The skill's central-runner instructions require inbox checks at round boundaries,
bounded work/checkpoints, task-local question blocking, existing review/grade/
accept/close discipline and no source checkout merge/push. Clear in-scope work
does not ask for approval at each step. A round must not wait forever for input.

## Launch and adapter contracts

Proposed public commands (retain `--project` and `--json`):

```text
acc launch --repo REPO --request-file REQUEST --mode approved-spec --spec-dir SPEC --approved
acc launch --repo REPO --request-file REQUEST --mode direct-spec --spec-dir SPEC
acc launch --repo REPO --request-file REQUEST --mode raw-request --authorize-raw
           [--cli claude|codex] [--base REV] [--restricted|--unrestricted] [--backend default|orca]
           [--install-worktree-acc] [--central-dir DIR]
acc central [--once] [--plain] [--poll SECONDS] [--web] [--central-dir DIR]
acc message ID --text TEXT [--reply-to QUESTION_ID]
acc attach ID
acc pause ID
acc resume ID
acc stop ID
acc recover ID
```

Validate mode/authorization/digest, probe executable, resolve base, create branch
`central/<uuid>` and worktree `<repo-name>.crew/central-<uuid>`, register and start
one supervisor. Setup failure marks failed and keeps the worktree for inspection.
HEAD must exist; do not copy source dirty changes. Validate request/spec through
existing sensitive-path/secret checks. Copy only selected validated external spec
files into the worktree, never arbitrary neighbors. Reject collisions.

Compare installed worktree ACC with the selected release. On absence/mismatch,
offer install/update interactively; non-interactive launch requires explicit
`--install-worktree-acc`. Declining exits before model launch. Run only the
selected released installer, only in that worktree, never in the source checkout
and never against global preferences.

Adapter API: `probe() -> dict`, `headless(record, prompt) -> list[str]`,
`interactive(record) -> list[str]`, `parse_events(stdout, stderr) -> dict`.
Normalize `session_id`, `text`, `tokens_in`, `tokens_out`, `tokens_total`,
`estimated`, `model`, `quota_family`, `error`. Reject a session ID different from
the requested resume. Preserve bounded raw logs for diagnosing format drift.

Claude first round: `claude -p --output-format stream-json --verbose`, prompt via
stdin; subsequent rounds add `--resume SESSION`, interactive uses
`claude --resume SESSION`. Unrestricted adds `--dangerously-skip-permissions`.
Codex first round: `codex exec --json -`; subsequent:
`codex exec resume --json SESSION -`; interactive: `codex resume SESSION`.
Unrestricted adds `--dangerously-bypass-approvals-and-sandbox`. Always set cwd
to the worktree. Local CLI help was checked during planning; implementations
must probe installed capabilities and test supported event fixtures, not claim
all CLI versions work. Restricted headless mode must use supported no-prompt/
deny behavior or return a capability error, never hang waiting for approval.

Use argv, `shell=False`, controlled cwd/env and tested Windows executable/shim
resolution; never interpolate request text into a shell. Hidden Windows
supervisors use stdlib subprocess flags. Interactive entry inherits the current
terminal; no pty is needed.

## Supervision, ownership and recovery

States: `created -> starting -> running -> idle -> running`, plus `waiting`,
`paused`, `quota`, `attached`, `failed`, `stopped`, `completed`. Failed/uncertain
state needs explicit resume/recover. Pause and attach cooperatively wait for the
current bounded round to exit; show pausing/attaching intent. Timeout returns
busy instead of racing. View exit never stops supervisors. Start another model
round only for progress, new messages or an eligible task; unchanged-state loops
pause with diagnostics, idle polling makes no model calls.

A detached Python supervisor owns its CLI child, persists events and heartbeats
every second; lease stale time is ten seconds. Background and interactive entry
share an atomic per-session owner lease with random nonce and round/attach ID.
Attach requests yield, confirms child exit, acquires lease, resumes interactively
and releases in `finally`. If the attaching client dies, explicit recovery must
reconcile ownership. Durable control files route pause/stop to the live owner.
Only that owner kills its own tracked child tree; persisted orphan PIDs are
diagnostics, never kill authority. Reuse existing process-tree timeout patterns
without changing executor semantics. Recovery does not silently rerun a round.

The supervisor module owns its isolated-Python bootstrap and `supervisor_main`
entry before public CLI wiring, so its task can prove actual fake-CLI subprocess
startup without depending on the later central command parser.

Cap logs at 10 MiB per stream and retain 20 rounds of log payloads; mark truncation
and drain/discard excess to prevent pipe deadlock. Preserve all round metadata,
messages, usage and job history. Never clean worktrees automatically.

## Usage, quota and snapshot contracts

`central_usage.py` records coordinator usage under the active job lock, with
stable round event IDs. Reconcile those IDs after crashes so backfill cannot
duplicate usage. Until a job is known, keep usage in round metadata, then backfill
once; never count both pending and backfilled copies. Accept central-session usage
only when reported and attributed once; an unreported remainder stays unknown.
Missing CLI usage can use character/4 estimates clearly marked as estimated.

`central_metrics.py` sums raw counts, not rounded percentages. Preserve existing
`economy.job_economy` rules: accepted-free pieces, all tier tokens including
reviews/retries, attempts excluding quota/brief_refused, all grade entries.
Deduplicate jobs by canonical worktree + job ID and shared imported histories by
stable attempt/usage IDs, not job ID alone across unrelated repositories. Ratios
use summed numerators/denominators; attempts/piece counts attempted pieces; score
uses grade entries. Wall duration is earliest start to latest end; execution time
sums attempts. Active jobs show current elapsed time. Unknown stays unknown,
estimates use `~` and incomplete coverage is explicit. No claimed cash savings
from subscription tokens or invented direct-work baseline.

`central_quota.py` records family/cooldown with UTC times and maximum expiry.
Existing pick/dispatch consult it only under `CREW_CHIEF_CENTRAL_ID`, so ordinary
v0.3.4 jobs do not depend on central. Record executor and coordinator quota events;
hold affected families only, never loosen tier/budget gates.

`central_snapshot.py` returns `{schema_version:1, generated_at, coordinators,
questions, totals, diagnostics}` from registry + live jobs. Read-only with bounded
per-row failure handling: last complete in-memory row is stale, first-read failure
is unavailable. Terminal, JSON and web consume the same object; no view writes
derived data back to disk.

Each coordinator row includes `id`, `repo_label`, `cli`, `backend`, `state`,
`worktree_available`, `stale`, `accepted`, `total`, `active_pieces`, `jobs`,
`metrics`, `models`, `scoreboard`, `last_error`. Active pieces expose ref/title,
work_type, executor, status and elapsed_s. Jobs expose id/title/state/start/end;
models expose role/tier, tokens_in/out/total and estimated/coverage; metrics expose
the raw numerator/denominator counts alongside formatted ratios. Missing numeric
values are JSON null. No consumer calculates success from a missing value.

## Terminal and browser views

`central_terminal.py` owns pure string rendering; `central_keys.py` isolates
input and restoration. Windows uses `msvcrt`, POSIX `select`/`termios`/`tty`, no
curses. Handle resize/narrow output; redirected/plain mode prints one finite
snapshot. Default refresh is one second. Arrows select, Enter attaches, m sends
or answers, p toggles pause, d shows details, q exits the view. Esc dismisses
panel details; while the CLI owns stdin its own exit command returns to the
panel. Document this practical interpretation of the draft's Esc behavior.

v0.4.1 uses `ThreadingHTTPServer`, `127.0.0.1`, ephemeral default port and packaged
HTML/CSS/JS (no CDN). `GET /api/snapshot` returns the common snapshot. Bounded JSON
POST message/answer/pause/resume/attach routes accept IDs, never arbitrary paths
or commands. Per-server unpredictable CSRF token, strict Origin/Host checks, no
CORS and asset allowlist. Use DOM textContent, not innerHTML, for untrusted text.
An attach request queues an intent for an active terminal view to take through
the same handoff; otherwise show `acc attach ID`. No browser shell is embedded.
Cards, token chart, history and scoreboard use synthetic public fixture data.

## Optional Orca backend

v0.4.1 `central_backend.py` defines probe/create/start/send/attach/state contracts.
Default backend delegates accepted core behavior. `central_orca.py` probes the
installed executable's help and machine-readable capabilities; do not invent its
API. Normalize results, use argv, preserve IDs and fail explicitly on unsupported
versions without fallback after partial creation. Backend choice is immutable
per launch. Fake Orca tests prove contract behavior; a real interactive smoke
check is optional owner execution after automated tests.

## Command installation

`command_install.py` creates a wrapper for an explicit released bundle/Python
interpreter with `-I`: Windows `acc.cmd`, POSIX executable `acc`. Explicit
`install.py --command [--bin-dir DIR]` is command-only; positional project
installation retains its behavior, combining both requires explicit options.
Document user bin defaults without changing PATH/admin settings. Refuse unrelated
wrappers unless replacing an ACC-owned wrapper explicitly. Tests use temporary
directories only. Do not install a skill into this development project.

## Decisions to confirm

All eleven defaults below come from input section 10, including the shipped SDD
decision. This planning commit does not silently approve them.

1. **Model-free monitor**: `acc central` is Python in a second terminal; the AI conversation refines/launches and reads registry data when needed. Replies can come from m, the page or the AI session.
2. **Headless resumable rounds**: reuse one Claude/Codex session; entry resumes it interactively in its worktree, with logs/messages fallback. This replaces a custom Windows pty.
3. **Live worktree records and shared quotas**: jobs stay in each worktree's `docs/crew-chief/`; multiple worktrees per repository are supported; registry aggregates observed cooldowns.
4. **Proportional SDD**: trivial needs no spec, small needs a job, project needs a spec folder; mandatory proofs apply to imported hands-on tasks. Already shipped; preserve it.
5. **Configurable anti-goal limits**: token share, attempts and elapsed time thresholds; count reported central-session usage without inventing direct-work cost.
6. **Global command and explicit worktree install**: new installer option for `acc`; offer install/update on mismatch only in the coordinator worktree, never silently or in the source checkout.
7. **Default coordinator matches calling CLI**: project/launch overrides; clear error if no supported default can be resolved.
8. **Registry outside repositories**: `~/.config/agent-crew-chief/central/`; project-local job history.
9. **File messages**: portable inbox/outbox is the default transport.
10. **Command name `acc`**: retain the current Python script entry too.
11. **Split SDD/central delivery when large**: SDD already shipped in v0.3.x; apply the intent as v0.4.0 core and v0.4.1 web/optional Orca, subject to approval.

Additional defaults proposed here: JSON schema v1, UUIDs, 64 KiB bodies,
900-second rounds, one-second polling, ten-second stale lease, bounded logs and
manual owner gate before web/Orca. Unrestricted permissions and Claude/Codex as
first adapters are owner decisions from input section 9, not new proposals.

## Risks

| Risk | Mitigation and residual limit |
| --- | --- |
| CLI resume/event drift | Capability probes, fake adapter fixtures, explicit unsupported state |
| Windows shims and interactive return | OS-specific stdlib input/wrapper proofs, inherited terminal; CLI exit returns to panel |
| Two session owners or orphan processes | Nonce leases, cooperative handoff, contention/recovery tests; uncertain PID never signalled |
| Model ignores question/round protocol | Validate structured result, stop failed rounds, timeouts; instructions are not a sandbox |
| File crash/concurrency/BOM/CRLF | Atomic writes, bounded locks, replay IDs and preservation tests |
| Duplicate/partial accounting | Usage IDs, pending/backfill reconciliation, count-based ratios and coverage labels |
| Unrestricted permissions escape cwd | Explicit restrictive option and existing after-the-fact checks; no confinement claim |
| Local web actions abused | Loopback, Origin/Host/CSRF validation, allowlists, bounded payloads and escaped text |
| Orca version differences | Probe actual capabilities; contract tests; ship after core |
| Scope delays useful delivery | Core release gate at phase 5; separate authorization for phases 6–8; no SDD rewrite |

## Approval handoff

Exact files, dependency edges, interfaces and proofs live in `crew.toml`.
`tasks.md` uses spec-kit task/phase grammar; `[P]` requires disjoint files within
its phase and never bypasses prerequisites. Release-wide suites run at release
gates, not as early proofs. Import/status/brief rendering validate planning; no
unimplemented feature proof is claimed to pass.

Phase 1 commits only docs/preferences/spec/ACC records. No dispatch, accept,
close, install, merge, push or release command. After owner approval, render
piece 1 of the recorded job with the separate released ACC and explicit project,
then pick/dispatch/review/grade/accept in order. Stop again after phase 5.

## Coordinator continuity after quota or stall (US7, owner-approved 2026-10-21)

Phase 3 adds validated preferences (T041), detection/policy integration (T042)
and same-job handoff (T043), after the existing lifecycle foundation. Phase 4
adds display/actions (T044) after the snapshot/terminal owners. No web/Orca work.
Use `stall_minutes = 40` (finite positive) and
`on_coordinator_quota = "ask"` by default; allow `wait` or `handoff:claude` /
`handoff:codex` with normal launch/project/global precedence, read-only settings.
An explicitly configured target counts as owner choice of that CLI; installation
alone never establishes permission to use a costlier CLI.

Track output activity and durable job progress separately using injected clocks.
During a child round, observe activity through the round runner; stall requires
BOTH inactivity windows to expire, even when the normal finite round timeout is
longer. Normal timeout remains distinct. Persist a continuity object with
reason, stopped_at, quota_reset_at (nullable), policy, pending question/action and
handoff history in the strict registry schema with backwards-compatible defaults.
Never infer quota from an arbitrary generic error. Use adapter-normalized quota
and recognized output/exit evidence; parse only explicit supported reset data.
Wait until a known reset without model calls and resume the exact old session;
unknown reset and stalled wait remain held until explicit evidence/action. Ask
uses an idempotent durable question. Stop uses existing ownership-safe controls.

Handoff first cooperatively ends the current round and acquires exclusive owner
lease; never use a persisted PID as kill authority. Validate supported installed
target and explicit owner choice before starting it. Keep central ID, worktree,
authorization, active job and every accepted piece; clear only the old CLI session
for a new target session. Preserve source session in the audit. Build the bounded
brief from actual ACC spec status plus persisted pending pieces/open attempts,
rulings and inbox/outbox. Never consume prior chat/session memory. Reject corrupt,
oversized, traversal or mismatched-job evidence visibly rather than fabricate a
brief. Stable handoff IDs and prepared/committed journal states reconcile a crash
between ruling and registry updates without duplicate ruling or target launch;
uncertain launches require explicit recovery. Every attempt and outcome remains
visible in registry audit and same-job rulings. Existing executor spending gates
remain unchanged. Tests invoke fake CLIs only.

T044 reads continuity metadata through the shared snapshot, prioritizes alerts
above ordinary questions/rows, shows UTC stopped/reset or unknown, and routes
wait/handoff/stop through the phase-3 public action boundary. This phase-4 UI work
is specified now but MUST NOT be implemented during the phase-3-only run.
