# T040 Gate the v0.4.1 web and Orca release in tests/test_central_web_gate.py, skill/crew-chief/scripts/crew_chief/__init__.py, CHANGELOG.md; depends on T035, T038, T039
Feature: 001-central · Phase: Phase 8: Web and Orca release gate (v0.4.1)

## Acceptance criteria
- FR-016: In v0.4.1 provide `acc central --web` with `http.server` and packaged static assets, loopback only, including cards, charts, history, scoreboard, messages, questions and controlled terminal entry.
- FR-017: In v0.4.1 provide explicitly optional Orca backend with the same launch/message/state/attach contract; default mode never needs Orca.
- FR-018: Preserve v0.3.4 SDD/job/executor behavior; coordinators use existing pick/brief/dispatch/review/grade/accept/close and leave work in their own branch for owner integration.
- FR-019: Runtime requires only Python 3.11+ stdlib and Git on Windows/Linux/macOS; optional CLIs are probed; pytest is development-only.
- FR-021: Public artifacts contain no private data, client names, personal paths or credentials; tests use temporary homes/config roots and fake CLIs, never real `~/.config` or subscriptions.
- FR-023: Commits and generated commit instructions omit AI co-author trailers while preserving human authorship and executor accounting in records.
- SC-008: Existing/new suites pass in CI on Windows/Linux/macOS and Python 3.11/3.13 with isolated processes and configuration.
- SC-009: v0.4.1 web tests cover snapshot parity, actions, escaping, Origin/token checks, traversal, loopback binding and cleanup without live coordinators.
- SC-010: v0.4.1 Orca fake-executable tests cover supported/missing/incompatible cases, session/message identity and Orca-independent default behavior.
- SC-011: Release docs give exact install/launch/monitor/message/attach/pause/stop/recovery flow, split, estimate limitations and owner-controlled integration.

## Global constraints
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

## Interfaces
- consumes: All earlier tasks accepted; full existing/new suite; CI matrix accepted at T028.
- produces: Version 0.4.1 and changelog; installed-bundle fake end-to-end gate includes web/default/Orca modes.
- verification: Run full suite and installed-bundle fake flow, inspect public artifacts, require three-OS CI and documented fake browser smoke evidence before release review; no push/tag/release without owner action.
- design: Read the relevant contract only in specs/001-central/plan.md: Delivery split and gates

## Files you may change
- tests/test_central_web_gate.py
- skill/crew-chief/scripts/crew_chief/__init__.py
- CHANGELOG.md
- README.md
- README.pt-BR.md
- docs/tutorials/01-first-job.md
- docs/tutorials/01-first-job.pt-BR.md

## Proof
Command: `python -m pytest -q`
Expect: passed

## Style (ponytail full)
- Make the smallest change that fully solves the task; reuse what the codebase, the standard library or installed dependencies already have.
- No abstraction, wrapper, option, config or "for later" code nobody asked for. Deletion beats addition.
- Never add a dependency for a few lines.
- Be lazy about the solution, never about the change: update every caller, test and fixture your change breaks.
- Never cut validation at trust boundaries, error handling that prevents data loss, or security.
- End your report with one line: what you skipped or did not check.
- Full rules: .agents/skills/ponytail/SKILL.md (if present).

## Report
End with one status line: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT
