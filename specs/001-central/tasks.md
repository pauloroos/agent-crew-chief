# Tasks: ACC central

**Status**: Owner-approved implementation; phases 1 and 2 accepted. US7 added per owner instruction on phase-3 start.

The numbered phases below are implementation phases, not authorization to start.
This planning session is phase 1 of the owner workflow and stops before T001.
Use released ACC v0.3.4 for orchestration. Exact acceptance refs/files/proofs/interfaces
are in crew.toml. Each task owns its tests and creates any fake fixture it needs.
For every behavior: demonstrate a focused red test, implement, run proof, obtain
independent-family review at the correct HEAD, grade and accept. Only accept checks tasks.

| Phase | Tasks | Count |
| --- | --- | --- |
| 1: Foundation (v0.4.0) | T001–T006 | 6 |
| 2: Launch and adapters (v0.4.0) | T007–T014 | 8 |
| 3: Supervision and lifecycle (v0.4.0) | T015-T020, T041-T043 | 9 |
| 4: Metrics and terminal (v0.4.0) | T021-T026, T044 | 7 |
| 5: Core verification and release gate (v0.4.0) | T027–T030 | 4 |
| 6: Web view (v0.4.1, separate approval) | T031–T035 | 5 |
| 7: Optional Orca backend (v0.4.1) | T036–T038 | 3 |
| 8: Web and Orca release gate (v0.4.1) | T039–T040 | 2 |

Total: **44 tasks**: **34 core** and **10 follow-up**.
Stop after T030 for owner review. Do not start T031 merely because it becomes ready.

## Phase 1: Foundation (v0.4.0)

- [X] T001 [P] [US1] Resolve central roots and coordinator settings in skill/crew-chief/scripts/crew_chief/central_config.py, tests/test_central_config.py
- [X] T002 [P] [US1] Expose validated central preference keys in tests/test_central_preferences.py, skill/crew-chief/scripts/crew_chief/config.py, skill/crew-chief/templates/preferences.example.toml; depends on T001
- [X] T003 [P] [US1] Persist versioned coordinator registry records in skill/crew-chief/scripts/crew_chief/central_registry.py, tests/test_central_registry.py; depends on T001
- [X] T004 [P] [US2] Deliver durable inbox and outbox envelopes in skill/crew-chief/scripts/crew_chief/central_messages.py, tests/test_central_messages.py; depends on T003
- [X] T005 [P] [US2] Define bounded coordinator round prompts and results in skill/crew-chief/scripts/crew_chief/coordinator_protocol.py, tests/test_coordinator_protocol.py
- [X] T006 [P] [US6] Build portable user command wrappers in skill/crew-chief/scripts/crew_chief/command_install.py, tests/test_command_install.py

### T001: verification boundary

Check explicit/env/home root precedence; launch/project/global/caller precedence; unknown CLI; non-finite/negative timing; no filesystem writes.

Proof: `python -m pytest tests/test_central_config.py -q`.

### T002: verification boundary

Load new scalars with project-over-global precedence; preserve unknown existing settings; parse BOM/CRLF and leave input bytes unchanged.

Proof: `python -m pytest tests/test_central_preferences.py -q`.

### T003: verification boundary

Test concurrent register/update without lost entries; BOM/CRLF; invalid UUID/schema/path; atomic-write interruption; bounded reads and contested lease without unsafe stale-lock takeover.

Proof: `python -m pytest tests/test_central_registry.py -q`.

### T004: verification boundary

Test send/ack/reply IDs, concurrent sends, crash before ack, replay without duplicate replies, question correlation, malformed/oversized bodies and symlink/traversal refusal.

Proof: `python -m pytest tests/test_central_messages.py -q`.

### T005: verification boundary

Test bounded context and each mode; only dependent tasks blocked; correlated answers; invalid/missing result fields rejected; tool-output text cannot impersonate the assistant result.

Proof: `python -m pytest tests/test_coordinator_protocol.py -q`.

### T006: verification boundary

Create Windows acc.cmd and POSIX executable acc in temp bin; test spaces/Unicode/quoting, -I, idempotent update, unrelated-wrapper refusal, unchanged preferences/PATH; run platform-native wrapper with a fake entry point.

Proof: `python -m pytest tests/test_command_install.py -q`.

## Phase 2: Launch and adapters (v0.4.0)

- [X] T007 [P] [US1] Create isolated long-lived coordinator worktrees in skill/crew-chief/scripts/crew_chief/central_worktrees.py, tests/test_central_worktrees.py; depends on T003
- [X] T008 [P] [US1] Adapt Claude Code session launch and resume in skill/crew-chief/scripts/crew_chief/coordinator_claude.py, tests/test_coordinator_claude.py; depends on T001, T005
- [X] T009 [P] [US1] Adapt Codex session launch and resume in skill/crew-chief/scripts/crew_chief/coordinator_codex.py, tests/test_coordinator_codex.py; depends on T001, T005
- [X] T010 [P] [US1] Preflight requests and explicit worktree skill updates in skill/crew-chief/scripts/crew_chief/central_preflight.py, tests/test_central_preflight.py; depends on T005, T007, T008, T009
- [X] T011 [P] [US1] Prepare authorized central launches in skill/crew-chief/scripts/crew_chief/central_launch.py, tests/test_central_launch.py; depends on T002, T003, T007, T008, T009, T010
- [X] T012 [P] [US2] Execute one bounded headless coordinator round in skill/crew-chief/scripts/crew_chief/central_rounds.py, tests/test_central_rounds.py; depends on T003, T004, T005, T008, T009, T011
- [X] T013 [P] [US6] Add explicit command-only installation mode in tests/test_command_install_cli.py, install.py; depends on T006
- [X] T014 [P] [US4] Record idempotent coordinator usage in active jobs in skill/crew-chief/scripts/crew_chief/central_usage.py, tests/test_central_usage.py; depends on T003, T012

### T007: verification boundary

Use temp Git repos; preserve dirty source files; two launches in one repo; explicit base; branch collision, unborn HEAD and spaces; record all git argv and reject any merge/push/automatic removal.

Proof: `python -m pytest tests/test_central_worktrees.py -q`.

### T008: verification boundary

Own fake executable/event fixtures cover first launch, exact --resume identity, session ID capture, stream-json usage, estimates, quota, malformed events, missing flags, restrictive no-prompt capability and shell metacharacters.

Proof: `python -m pytest tests/test_coordinator_claude.py -q`.

### T009: verification boundary

Own fake executable/event fixtures cover exec JSON, exec resume exact session, interactive resume, usage, estimates, quota, session mismatch, nonzero exits and restrictive mode that cannot hang on approval.

Proof: `python -m pytest tests/test_coordinator_codex.py -q`.

### T010: verification boundary

Missing CLI, denied mismatch update and unapproved/raw modes launch no model; matching version passes; explicit update targets only temp worktree; selected spec copy excludes neighbors, private paths, traversal and secrets.

Proof: `python -m pytest tests/test_central_preflight.py -q`.

### T011: verification boundary

Prepare creates unique durable record with authorization digest, worktree/session placeholders and selected adapter; failed preflight preserves diagnostic/worktree and never starts a supervisor; no source mutation.

Proof: `python -m pytest tests/test_central_launch.py -q`.

### T012: verification boundary

Fake child proves cwd/stdin/env, exact resume, ack only consumed IDs, result persistence, timeout/failure/session mismatch and 10-MiB log truncation while draining output; tests need no future supervisor.

Proof: `python -m pytest tests/test_central_rounds.py -q`.

### T013: verification boundary

Temp released bundle and temp bins verify command-only does not create project skills; legacy installation still works; unchanged real-settings sentinel, no PATH writes, bad source/wrapper errors.

Proof: `python -m pytest tests/test_command_install_cli.py -q`.

### T014: verification boundary

Own temp job fixtures cover measured/estimated/missing totals, unknown active job, backfill once, crash between job update and round mark, repeated calls, attributed central role and no double counting.

Proof: `python -m pytest tests/test_central_usage.py -q`.

## Phase 3: Supervision and lifecycle (v0.4.0)

- [X] T015 [P] [US2] Supervise coordinator rounds without idle model calls in skill/crew-chief/scripts/crew_chief/central_supervisor.py, tests/test_central_supervisor.py; depends on T011, T012, T014
- [X] T016 [P] [US2] Implement cooperative pause resume and stop controls in skill/crew-chief/scripts/crew_chief/central_controls.py, tests/test_central_controls.py; depends on T003, T004, T015
- [X] T017 [P] [US2] Hand off exclusive session ownership for interactive attach in skill/crew-chief/scripts/crew_chief/central_attach.py, tests/test_central_attach.py; depends on T008, T009, T015, T016
- [X] T018 [P] [US2] Reconcile stale supervisors and recover explicitly in skill/crew-chief/scripts/crew_chief/central_recovery.py, tests/test_central_recovery.py; depends on T003, T015, T016, T017
- [X] T019 [US1] Wire public launch and lifecycle CLI commands in skill/crew-chief/scripts/crew_chief/central_cli.py, tests/test_central_cli.py, skill/crew-chief/scripts/crew_chief/cli.py, skill/crew-chief/scripts/crew_chief/central_supervisor.py; depends on T011, T012, T014, T015, T016, T017, T018
- [X] T020 [US4] Share observed quota cooldowns across central coordinators in skill/crew-chief/scripts/crew_chief/central_quota.py, tests/test_central_quota.py, skill/crew-chief/scripts/crew_chief/scoring.py, skill/crew-chief/scripts/crew_chief/cli.py, skill/crew-chief/scripts/crew_chief/dispatch.py, skill/crew-chief/scripts/crew_chief/central_supervisor.py; depends on T003, T015, T019

### T015: verification boundary

Own fake CLI and subprocess bootstrap prove hidden Windows start before public CLI integration; fake runner/clock proves state transitions, heartbeat, session reuse, failed hold, no-progress pause, idle zero-call behavior and log retention preserving metadata.

Proof: `python -m pytest tests/test_central_supervisor.py -q`.

### T016: verification boundary

Pause finishes current bounded round; resume restores eligibility; stop routes only to live owner and its child tree; repeated controls idempotent; view exit cannot stop a coordinator; stale PID never signalled.

Proof: `python -m pytest tests/test_central_controls.py -q`.

### T017: verification boundary

Fake terminal proves yield before resume, same worktree/session, no competing writer, busy timeout, exception/finally release, interrupted-client recovery state, unsupported CLI and redirected fallback.

Proof: `python -m pytest tests/test_central_attach.py -q`.

### T018: verification boundary

PID reuse, stale heartbeat, unknown child, held attach lease, crash during persistence and missing worktree yield explicit states; recovery never kills by PID, loses messages or silently retries failed rounds.

Proof: `python -m pytest tests/test_central_recovery.py -q`.

### T019: verification boundary

Subprocess CLI fake tests cover exact help/JSON/exit codes and each launch mode; launch now starts supervisor; controls consumed; no dev-bundle coordinator execution; old commands retain parser behavior.

Proof: `python -m pytest tests/test_central_cli.py -q`.

### T020: verification boundary

Two coordinator fake jobs share same-family expiry, unrelated families proceed, latest expiry wins; supervisor and executor quota both observed; no change to budgets/free-first; absent central context preserves baseline.

Proof: `python -m pytest tests/test_central_quota.py -q`.

- [X] T041 [US7] Add validated coordinator continuity preferences in skill/crew-chief/scripts/crew_chief/central_config.py, skill/crew-chief/scripts/crew_chief/config.py, skill/crew-chief/templates/preferences.example.toml, tests/test_central_continuity_preferences.py; depends on T002
- [X] T042 [US7] Detect and hold quota or stalled coordinators with durable policy in skill/crew-chief/scripts/crew_chief/central_continuity.py, skill/crew-chief/scripts/crew_chief/central_rounds.py, skill/crew-chief/scripts/crew_chief/central_supervisor.py, skill/crew-chief/scripts/crew_chief/central_registry.py, tests/test_central_continuity.py; depends on T020, T041
- [X] T043 [US7] Transfer the same job exclusively with persisted takeover evidence in skill/crew-chief/scripts/crew_chief/central_handoff.py, skill/crew-chief/scripts/crew_chief/central_supervisor.py, skill/crew-chief/scripts/crew_chief/central_cli.py, tests/test_central_handoff.py; depends on T042

### T041: verification boundary

Defaults 40/ask; launch/project/global precedence; finite positive stall timing and strict supported-target policy; BOM/CRLF and unchanged settings sentinels.

Proof: `python -m pytest tests/test_central_continuity_preferences.py -q`.

### T042: verification boundary

Fake running-child output/progress clocks prove both-window stall detection; quota output/exit and known/unknown resets; durable ask, exact-session automatic wait/resume, explicit stop; registry compatibility, no idle calls.

Proof: `python -m pytest tests/test_central_continuity.py -q`.

### T043: verification boundary

Fake installed CLIs and temporary real job records prove same job/worktree, accepted preservation, bounded spec status/pending/open attempt/rulings/messages brief; explicit cost-target consent, lease exclusion and idempotent ruling/registry crash reconciliation; public wait/handoff/stop action boundary.

Proof: `python -m pytest tests/test_central_handoff.py -q`.

## Phase 4: Metrics and terminal (v0.4.0)

- [X] T021 [P] [US4] Aggregate raw economy counts with coverage labels in skill/crew-chief/scripts/crew_chief/central_metrics.py, tests/test_central_metrics.py; depends on T014, T020
- [X] T022 [P] [US3] Read one shared snapshot from live worktree records in skill/crew-chief/scripts/crew_chief/central_snapshot.py, tests/test_central_snapshot.py; depends on T003, T004, T018, T021
- [X] T023 [P] [US3] Read terminal keys and restore OS terminal state in skill/crew-chief/scripts/crew_chief/central_keys.py, tests/test_central_keys.py; depends on T001
- [X] T024 [P] [US3] Render coordinator rows questions and details in skill/crew-chief/scripts/crew_chief/central_terminal.py, tests/test_central_terminal.py; depends on T022
- [x] T025 [US3] Run the refreshing terminal and route its actions in skill/crew-chief/scripts/crew_chief/central_screen.py, tests/test_central_screen.py, skill/crew-chief/scripts/crew_chief/cli.py, skill/crew-chief/scripts/crew_chief/central_cli.py; depends on T017, T019, T022, T023, T024
- [x] T026 [US2] Teach coordinators the bounded central round workflow in tests/test_central_skill.py, skill/crew-chief/SKILL.md, skill/crew-chief/templates/snippet.md; depends on T005, T019, T025

### T021: verification boundary

Own synthetic jobs prove exact tier/model sums including retries/reviews, shared-history dedup, weighted ratios, wall vs execution time, estimates/unknown/coverage and all zero-disabled threshold alerts.

Proof: `python -m pytest tests/test_central_metrics.py -q`.

### T022: verification boundary

Temporary worktree records cover duplicate refs, missing/corrupt/oversized files, unknown schema, last-complete stale row, read-only byte/hash sentinels and parity-ready deterministic payload.

Proof: `python -m pytest tests/test_central_snapshot.py -q`.

### T023: verification boundary

Decode arrows/Enter/Esc/m/p/d/q, timeouts, unsupported/redirected input, resize and restoration after exceptions; importing on Windows never imports unavailable termios.

Proof: `python -m pytest tests/test_central_keys.py -q`.

### T024: verification boundary

Render repository/CLI/state/piece counts/free share/score and active executor/type/time; pending questions first; empty/unknown/stale/narrow/Unicode/ANSI plain behavior without filesystem changes.

Proof: `python -m pytest tests/test_central_terminal.py -q`.

### T025: verification boundary

Fake clock/input prove every key, two-interval update, message/reply, attach return and busy state, details/Esc, pause toggle, finite redirected snapshot, exception cleanup and q preserving supervisor; zero model polling calls.

Proof: `python -m pytest tests/test_central_screen.py -q`.

### T026: verification boundary

Render a synthetic central-runner brief and verify the quoted protocol/constraints survive installation and BOM/CRLF handling; existing job-only path remains valid; no AI trailer instruction.

Proof: `python -m pytest tests/test_central_skill.py -q`.

- [x] T044 [US7] Show coordinator interruption alerts and continuity actions in skill/crew-chief/scripts/crew_chief/central_snapshot.py, skill/crew-chief/scripts/crew_chief/central_terminal.py, skill/crew-chief/scripts/crew_chief/central_screen.py, tests/test_central_continuity_view.py; depends on T025, T043

### T044: verification boundary

Synthetic snapshot and fake actions prove top-priority quota/stall alerts, stopped/reset/unknown, wait/handoff/stop routing, no model polling and read-only viewing.

Proof: `python -m pytest tests/test_central_continuity_view.py -q`.

## Phase 5: Core verification and release gate (v0.4.0)

- [X] T027 [P] [US1] Prove the core multi-worktree coordinator flow in tests/test_central_flow.py; depends on T019, T020, T025, T026
- [X] T028 [P] [US6] Extend isolated core verification to macOS CI in tests/test_central_isolation.py, .github/workflows/ci.yml; depends on T027
- [X] T029 [P] [US6] Document core installation launch and recovery in tests/test_central_docs.py, README.md, docs/tutorials/04-central.md; depends on T025, T026, T027
- [X] T030 [US6] Gate the v0.4.0 core release in tests/test_central_core_gate.py, skill/crew-chief/scripts/crew_chief/__init__.py, CHANGELOG.md; depends on T027, T028, T029

### T027: verification boundary

Launch two repos plus second worktree of one; ask/reply while independent work proceeds; pause/resume/attach/failure/recover; retained close output in coordinator branch, no source merge/push; final cleanup proves no test process survives.

Proof: `python -m pytest tests/test_central_flow.py -q`.

### T028: verification boundary

Test subprocesses inherit only temporary homes/global/central roots, no live CLI lookup; config sentinel unchanged and wrapper executes in native platform. Run full suite in CI across matrix.

Proof: `python -m pytest tests/test_central_isolation.py -q`.

### T029: verification boundary

Own documentation test checks examples against parser with temp requests and fake commands; explain calling CLI override, yolo/restricted, interactive exit vs Esc, estimates, worktree consent, recovery and owner integration.

Proof: `python -m pytest tests/test_central_docs.py -q`.

### T030: verification boundary

Run whole suite and a fake installed-bundle smoke path; update current-release/pin assertions in test_docs while keeping all historical changelog assertions; update README/tutorial release pins in both languages only. Inspect scope/private data/coauthor trailers; require three-OS CI before owner release review. Stop before web/Orca; no push/tag/release authorized.

Proof: `python -m pytest tests/test_central_core_gate.py -q`.

Also run `python -m pytest -q` and require the declared CI matrix before release review.

**OWNER GATE**: Report the v0.4.0 result and wait for separate permission before T031.

## Phase 6: Web view (v0.4.1, separate approval)

- [X] T031 [P] [US5] Serve loopback HTTP snapshots with allowlisted assets in skill/crew-chief/scripts/crew_chief/central_web.py, tests/test_central_web.py; depends on T022, T030
- [X] T032 [P] [US5] Build public dashboard cards charts and history in tests/test_central_web_assets.py, skill/crew-chief/web/index.html, skill/crew-chief/web/central.css, skill/crew-chief/web/central.js; depends on T031
- [X] T033 [US5] Authorize web messages replies and lifecycle actions in tests/test_central_web_actions.py, skill/crew-chief/scripts/crew_chief/central_web.py, skill/crew-chief/web/central.js; depends on T031, T032
- [X] T034 [US5] Wire acc central --web startup and shutdown in tests/test_central_web_cli.py, skill/crew-chief/scripts/crew_chief/cli.py, skill/crew-chief/scripts/crew_chief/central_cli.py, skill/crew-chief/scripts/crew_chief/central_screen.py; depends on T025, T033
- [X] T035 [US5] Prove terminal and web parity through HTTP actions in tests/test_central_web_flow.py; depends on T031, T032, T033, T034

### T031: verification boundary

Bind 127.0.0.1 ephemeral port, JSON parity and read-only requests; reject traversal/oversized paths, disallowed Host, arbitrary files and non-loopback bind; release socket/threads in finally.

Proof: `python -m pytest tests/test_central_web.py -q`.

### T032: verification boundary

Own asset-serving fixture checks packaged resources, safe text insertion, no external scripts, model/tier chart data and question/history/scoreboard states. Record browser smoke steps against fake local snapshots in docs, never a live coordinator.

Proof: `python -m pytest tests/test_central_web_assets.py -q`.

### T033: verification boundary

Reject missing/wrong token, hostile Origin/Host, unknown coordinator, arbitrary argv/path, malformed/oversized JSON; accept one correlated reply once; attach queues intent without browser shell; enable asset controls safely.

Proof: `python -m pytest tests/test_central_web_actions.py -q`.

### T034: verification boundary

Fake browser/subprocess test verifies URL/port, browser-open failure fallback, terminal bridge action handoff, JSON/plain incompatibility errors and cleanup without stopping coordinators.

Proof: `python -m pytest tests/test_central_web_cli.py -q`.

### T035: verification boundary

Create message/question/reply and pause/attach intents through HTTP, compare terminal/web totals and state after refresh, exercise escaping/traversal/CSRF, copyable terminal fallback, repeated actions and full cleanup.

Proof: `python -m pytest tests/test_central_web_flow.py -q`.

## Phase 7: Optional Orca backend (v0.4.1)

- [X] T036 [P] [US6] Define optional backend operations over accepted core in skill/crew-chief/scripts/crew_chief/central_backend.py, tests/test_central_backend.py; depends on T030, T035
- [X] T037 [P] [US6] Adapt probed Orca capabilities to the backend contract in skill/crew-chief/scripts/crew_chief/central_orca.py, tests/test_central_orca.py; depends on T036
- [X] T038 [US6] Connect explicit backend choice to launches and controls in tests/test_central_backend_cli.py, skill/crew-chief/scripts/crew_chief/central_launch.py, skill/crew-chief/scripts/crew_chief/central_cli.py, skill/crew-chief/scripts/crew_chief/central_controls.py; depends on T036, T037

### T036: verification boundary

Run backend contract with core fakes; preserve coordinator/session/message IDs and errors; importing/using default mode never resolves or invokes Orca.

Proof: `python -m pytest tests/test_central_backend.py -q`.

### T037: verification boundary

Probe before use; test create/start/send/attach/state, ID/session normalization, unsupported version/missing binary, spaces/metacharacters, partial-creation diagnostics and no automatic fallback or real Orca calls.

Proof: `python -m pytest tests/test_central_orca.py -q`.

### T038: verification boundary

Public fake-CLI flow compares default/Orca contracts, rejects incompatible explicit selection, prevents backend switching mid-launch, routes messages/attach to correct backend and preserves existing JSON/errors.

Proof: `python -m pytest tests/test_central_backend_cli.py -q`.

## Phase 8: Web and Orca release gate (v0.4.1)

- [ ] T039 [P] [US6] Document browser and optional Orca operation in tests/test_central_web_docs.py, README.md, docs/tutorials/04-central.md; depends on T035, T038
- [ ] T040 [US6] Gate the v0.4.1 web and Orca release in tests/test_central_web_gate.py, skill/crew-chief/scripts/crew_chief/__init__.py, CHANGELOG.md; depends on T035, T038, T039

### T039: verification boundary

Check doc commands against parser with fakes and link/assets existence; provide reproducible fake-browser smoke procedure and user-controlled integration; no personal paths/private examples.

Proof: `python -m pytest tests/test_central_web_docs.py -q`.

### T040: verification boundary

Run full suite and installed-bundle fake flow, inspect public artifacts, require three-OS CI and documented fake browser smoke evidence before release review; no push/tag/release without owner action.

Proof: `python -m pytest tests/test_central_web_gate.py -q`.

Also run `python -m pytest -q` and require the declared CI matrix before release review.
