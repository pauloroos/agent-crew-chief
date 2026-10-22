# T039 Document browser and optional Orca operation in tests/test_central_web_docs.py, README.md, docs/tutorials/04-central.md; depends on T035, T038
Feature: 001-central · Phase: Phase 8: Web and Orca release gate (v0.4.1)

## Acceptance criteria
- FR-016: In v0.4.1 provide `acc central --web` with `http.server` and packaged static assets, loopback only, including cards, charts, history, scoreboard, messages, questions and controlled terminal entry.
- FR-017: In v0.4.1 provide explicitly optional Orca backend with the same launch/message/state/attach contract; default mode never needs Orca.
- FR-021: Public artifacts contain no private data, client names, personal paths or credentials; tests use temporary homes/config roots and fake CLIs, never real `~/.config` or subscriptions.
- SC-011: Release docs give exact install/launch/monitor/message/attach/pause/stop/recovery flow, split, estimate limitations and owner-controlled integration.
- US5/AC4: **Given** no available terminal bridge, **When** I select open terminal, **Then** the page shows a copyable `acc attach ID` instruction and does not claim it opened.
- US6/AC3: **Given** explicit Orca mode in v0.4.1, **When** its capability probe succeeds, **Then** worktree/terminal/message behavior meets the same backend contract; incompatibility fails without switching backend mid-launch.

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
- consumes: Accepted v0.4.1 parser/contracts; prior synthetic core tutorial; no new runtime behavior.
- produces: README and central tutorial cover --web, local action authorization, terminal fallback, optional Orca probes and v0.4.1 limits.
- verification: Check doc commands against parser with fakes and link/assets existence; provide reproducible fake-browser smoke procedure and user-controlled integration; no personal paths/private examples.
- design: Read the relevant contract only in specs/001-central/plan.md: Optional Orca backend; Terminal and browser views

## Files you may change
- tests/test_central_web_docs.py
- README.md
- docs/tutorials/04-central.md

## Proof
Command: `python -m pytest tests/test_central_web_docs.py -q`
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
