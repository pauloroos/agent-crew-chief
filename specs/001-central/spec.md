# ACC central

**Created**: 2026-10-21

**Status**: Draft — owner approval required before implementation

**Baseline**: ACC v0.3.4

**Delivery**: v0.4.0 core; v0.4.1 web and optional Orca adapter

## What the user wants

Run work in several repositories from one conversation. Each launch gets its own
worktree and a strong Claude Code or Codex coordinator. Coordinators plan, delegate
hands-on work through ACC's existing free-first cycle, review, and leave completed
branches for the owner. A Python-only central screen shows progress, questions,
messages, and recorded delegation cost without spending model tokens on polling.

Existing v0.3.x SDD, grading, proofs, rulings, changelog and per-job economy are
dependencies, not features to implement again. Input design section 7 is excluded.

## Stories

### User Story 1 - Launch independent coordinators (Priority: P1)

As an owner, I want Claude Code or Codex in isolated worktrees so that several
projects progress without disturbing my current checkout.

**Acceptance Scenarios**:

1. **Given** two repositories and installed CLIs, **When** I launch work in each, **Then** each has a distinct central ID, branch, worktree and coordinator session; neither source checkout changes.
2. **Given** an explicit launch mode, **When** I choose approved-spec, direct-spec or raw-request, **Then** only that mode's prerequisites apply; approved-spec requires an explicit approval record and raw-request requires explicit authorization.
3. **Given** no CLI override, **When** launch resolves its coordinator, **Then** per-launch, project and calling-CLI choices apply in that order; an unknown calling CLI produces an actionable error.
4. **Given** missing or different-version ACC in the new worktree, **When** preflight runs, **Then** it offers explicit install/update there; declining leaves launch unstarted and never modifies global preferences.
5. **Given** unrestricted permissions were disabled, **When** a coordinator starts, **Then** bypass flags are absent and permission-blocked work is reported without a hidden interactive prompt or automatic escalation.

### User Story 2 - Ask, answer and resume (Priority: P1)

As an owner, I want messages and an interactive conversation with each coordinator
so that a decision can be resolved without stopping unrelated work.

**Acceptance Scenarios**:

1. **Given** a coordinator between rounds, **When** I send a message, **Then** it reads the durable inbox next round, acknowledges the message and exposes its answer without losing it on retry.
2. **Given** one blocked and one independent task, **When** a coordinator asks an architectural, scope or data-loss question, **Then** it appears centrally with task/correlation IDs while independent work remains eligible.
3. **Given** a resumable session, **When** I enter its conversation, **Then** the background round yields exclusive ownership before interactive resume of that exact session in its worktree; returning restores background eligibility.
4. **Given** no resume capability or a redirected terminal, **When** I request entry, **Then** output and messaging remain available with an explicit limitation; no substitute session starts silently.
5. **Given** pause, quota, process failure or central restart, **When** state is reconciled, **Then** work/messages remain intact and only the affected coordinator is held.

### User Story 3 - Follow progress in a terminal (Priority: P1)

As an owner, I want a self-refreshing screen so that I can see all coordinators
and questions without model tokens being spent just to poll their state.

**Acceptance Scenarios**:

1. **Given** live jobs, **When** I open `acc central`, **Then** rows show repository, CLI, state, accepted/total pieces, review score and free share, with active pieces, work type, executor, state and elapsed time underneath.
2. **Given** a focused screen, **When** I use arrows, Enter, Esc, m, p, d or q, **Then** selection, conversation entry/return, message/reply, pause/resume, details and view exit work as documented; q leaves supervisors running.
3. **Given** Windows without pty or redirected output, **When** the screen starts, **Then** stdlib input or a finite plain snapshot works and terminal settings are restored on exit and exceptions.
4. **Given** a missing worktree or partially written job, **When** refresh runs, **Then** that row is unavailable/stale while other rows remain usable; reading never rewrites records.

### User Story 4 - Measure delegation honestly (Priority: P1)

As an owner, I want cost, rework, time and review quality per job, coordinator and
central so that I can see when delegation becomes wasteful.

**Acceptance Scenarios**:

1. **Given** measured, estimated and missing usage, **When** metrics aggregate, **Then** estimates are labelled, missing is unknown and repeated snapshots never count an attempt/usage event twice.
2. **Given** overlapping jobs, **When** totals display, **Then** ratios use summed numerators/denominators, wall time differs from summed execution time and model/tier totals include retries/reviews.
3. **Given** token-share, attempts and time thresholds, **When** exceeded, **Then** actionable alerts appear; zero disables a threshold and no hypothetical direct-work cost is invented.
4. **Given** a quota-family cooldown, **When** another coordinator uses that family, **Then** new rounds are held until expiry without blocking other families or increasing spending privileges.

### User Story 5 - Inspect the same data in a browser (Priority: P2, v0.4.1)

As an owner, I want localhost cards, history and charts so that I can inspect
progress and communicate using the same records as the terminal.

**Acceptance Scenarios**:

1. **Given** `acc central --web`, **When** the browser opens, **Then** cards, pieces, job history, model token charts, scoreboard and questions use the same snapshot API as the terminal.
2. **Given** an authorized local page, **When** I send a message, reply, pause/resume or request terminal entry, **Then** the action occurs once and appears in both views.
3. **Given** hostile origins, HTML-like messages or traversal paths, **When** handled, **Then** unauthorized mutations are refused, text renders safely and no arbitrary files/commands are exposed.
4. **Given** no available terminal bridge, **When** I select open terminal, **Then** the page shows a copyable `acc attach ID` instruction and does not claim it opened.

### User Story 6 - Install and optionally use Orca (Priority: P2)

As an owner, I want an `acc` command and optional Orca backend so that the workflow
works with plain Git/Python or integrates with my existing terminals.

**Acceptance Scenarios**:

1. **Given** an explicit command-install option and user bin directory, **When** I install/update a released tool, **Then** `acc` invokes that release, the old script still works and existing settings remain unchanged.
2. **Given** no Orca executable, **When** I use default launch/monitoring, **Then** all v0.4.0 features work without it.
3. **Given** explicit Orca mode in v0.4.1, **When** its capability probe succeeds, **Then** worktree/terminal/message behavior meets the same backend contract; incompatibility fails without switching backend mid-launch.

## Requirements

- **FR-001**: Add explicit user-scoped `install.py --command` for the `acc` wrapper without requiring project installation; retain the old script and never edit global preferences or PATH silently.
- **FR-002**: Create unique coordinator worktrees/branches from explicit base (default HEAD), support multiple coordinators per repository, preserve dirty source checkouts and never merge, push or delete worktrees automatically.
- **FR-003**: Resolve CLI by launch override, project preference, then calling CLI (`CREW_CHIEF_COORDINATOR`); support Claude Code/Codex with unrestricted default and an explicit restrictive override.
- **FR-004**: Support approved-spec, direct-spec and explicitly authorized raw-request modes; persist mode/authorization evidence without per-step approvals for clear requests.
- **FR-005**: Preflight executables and worktree ACC version; require explicit mismatch install/update consent and install only in the coordinator worktree.
- **FR-006**: Store versioned atomic concurrent-safe registry records under `~/.config/agent-crew-chief/central/`, with injectable root, stable IDs, process/session identity, worktree and job references.
- **FR-007**: Use durable file inboxes/outboxes with message IDs, acknowledgments, timestamps, replies and idempotent redelivery; reject malformed/oversized messages.
- **FR-008**: Surface correlated questions/answers with blocked task IDs; only dependent work waits.
- **FR-009**: Supervise finite headless rounds resuming one persisted session, consuming messages between rounds, bounding logs and retaining failures.
- **FR-010**: Hand off exclusive session ownership to interactive resume in the same worktree and restore background eligibility; otherwise fall back explicitly to logs/messages.
- **FR-011**: Provide pause/resume, explicit stop, restart reconciliation and owner-controlled recovery; never signal a bare persisted PID or silently restart failed/uncertain work.
- **FR-012**: Provide stdlib refreshing terminal with US3 rows, active pieces, highlighted questions and arrows/Enter/Esc/m/p/d/q controls, finite plain snapshots and terminal restoration.
- **FR-013**: Aggregate job/coordinator/central tokens by role/model/tier, free pieces/tokens, attempts per piece, grades and wall/execution durations using v0.3.4 record semantics.
- **FR-014**: Reuse configurable anti-goal thresholds; identify incomplete coverage/estimates and suggest adjustments without claiming monetary savings.
- **FR-015**: Share observed quota-family cooldowns through the registry before new rounds and executor pick/dispatch, preserving free-first and spending gates.
- **FR-016**: In v0.4.1 provide `acc central --web` with `http.server` and packaged static assets, loopback only, including cards, charts, history, scoreboard, messages, questions and controlled terminal entry.
- **FR-017**: In v0.4.1 provide explicitly optional Orca backend with the same launch/message/state/attach contract; default mode never needs Orca.
- **FR-018**: Preserve v0.3.4 SDD/job/executor behavior; coordinators use existing pick/brief/dispatch/review/grade/accept/close and leave work in their own branch for owner integration.
- **FR-019**: Runtime requires only Python 3.11+ stdlib and Git on Windows/Linux/macOS; optional CLIs are probed; pytest is development-only.
- **FR-020**: Windows needs no pty, curses, tmux or ConPTY dependency; interactive entry uses the actual terminal and persisted session.
- **FR-021**: Public artifacts contain no private data, client names, personal paths or credentials; tests use temporary homes/config roots and fake CLIs, never real `~/.config` or subscriptions.
- **FR-022**: Read UTF-8 with optional BOM/CRLF and preserve existing BOM/newline style when editing preferences, instructions and records; replace rewritten records atomically.
- **FR-023**: Commits and generated commit instructions omit AI co-author trailers while preserving human authorship and executor accounting in records.
- **FR-024**: Bound message/event/log reads; isolate corrupt/stale/incompatible/missing records per coordinator, reject traversal and report errors without dropping history.
- **FR-025**: Capture CLI-reported coordinator usage, otherwise mark character estimates; use stable event IDs to record usage exactly once in the active job and accept attributed central-session usage without guessing totals.
- **FR-026**: Use one read-only terminal/web/JSON snapshot contract sourcing live worktree jobs and registry metadata, without a mutable metrics database or viewing side effects.

## Done when

- **SC-001**: Fake-CLI integration covers two repositories and two worktrees of one repository, unique sessions/branches, unchanged source checkouts and no merge/push command.
- **SC-002**: Both adapter suites cover first launch, exact-session headless/interactive resume, permissions, usage, missing capabilities and failures without model calls.
- **SC-003**: Crash/retry message tests preserve IDs, resolve correlated questions once and show independent work continuing while a dependency waits.
- **SC-004**: Lifecycle tests cover pause, quota, restart, stale PID, attach contention/interruption and explicit stop without signalling unrelated processes or concurrent session writers.
- **SC-005**: Terminal tests cover all keys, resize, narrow/redirected output, questions, corrupt rows and cleanup; fake updates appear within two polling intervals (one-second default).
- **SC-006**: Metric fixtures cover retries/reviews, mixed tiers/models, duplicate job refs, overlapping time, estimates, missing usage and each threshold with exact expected totals.
- **SC-007**: Temporary command-install tests cover three OS families, spaces/Unicode, untouched global preferences and the legacy script entry.
- **SC-008**: Existing/new suites pass in CI on Windows/Linux/macOS and Python 3.11/3.13 with isolated processes and configuration.
- **SC-009**: v0.4.1 web tests cover snapshot parity, actions, escaping, Origin/token checks, traversal, loopback binding and cleanup without live coordinators.
- **SC-010**: v0.4.1 Orca fake-executable tests cover supported/missing/incompatible cases, session/message identity and Orca-independent default behavior.
- **SC-011**: Release docs give exact install/launch/monitor/message/attach/pause/stop/recovery flow, split, estimate limitations and owner-controlled integration.

## Boundaries and edge cases

- This phase writes/imports the plan only. Tasks remain unchecked; no dispatch before owner approval.
- No remote server, shared-user access, sandbox, automatic merge/push, new billing scheme, SDD rewrite or Antigravity coordinator adapter.
- Unrestricted permissions are an owner-selected default, not filesystem confinement; cwd/instructions cannot guarantee containment.
- The AI conversation refines/launches; Python screens only read or send explicit actions.
- Unknown usage, corrupt jobs, moved worktrees and unsupported sessions stay visible with diagnostics. Do not reconstruct lost sessions silently or inflate success totals.
- The supplied visual draft was read as layout guidance. Public examples must use fictional repositories and synthetic metrics; do not copy the private original.
