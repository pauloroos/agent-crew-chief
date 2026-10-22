# T038 Connect explicit backend choice to launches and controls in tests/test_central_backend_cli.py, skill/crew-chief/scripts/crew_chief/central_launch.py, skill/crew-chief/scripts/crew_chief/central_cli.py, skill/crew-chief/scripts/crew_chief/central_controls.py; depends on T036, T037
Feature: 001-central · Phase: Phase 7: Optional Orca backend (v0.4.1)

## Acceptance criteria
- FR-017: In v0.4.1 provide explicitly optional Orca backend with the same launch/message/state/attach contract; default mode never needs Orca.
- FR-018: Preserve v0.3.4 SDD/job/executor behavior; coordinators use existing pick/brief/dispatch/review/grade/accept/close and leave work in their own branch for owner integration.
- SC-010: v0.4.1 Orca fake-executable tests cover supported/missing/incompatible cases, session/message identity and Orca-independent default behavior.
- US6/AC2: **Given** no Orca executable, **When** I use default launch/monitoring, **Then** all v0.4.0 features work without it.
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
- consumes: T036 default_backend; T037 orca_backend; T011 prepare_launch; T019 handlers and T016 controls.
- produces: Launch --backend selects immutable backend; public message/attach/control handlers route the registered backend while default behavior stays unchanged.
- verification: Public fake-CLI flow compares default/Orca contracts, rejects incompatible explicit selection, prevents backend switching mid-launch, routes messages/attach to correct backend and preserves existing JSON/errors.
- design: Read the relevant contract only in specs/001-central/plan.md: Optional Orca backend

## Files you may change
- tests/test_central_backend_cli.py
- skill/crew-chief/scripts/crew_chief/central_launch.py
- skill/crew-chief/scripts/crew_chief/central_cli.py
- skill/crew-chief/scripts/crew_chief/central_controls.py
- skill/crew-chief/scripts/crew_chief/central_backend.py
- tests/conftest.py

## Proof
Command: `python -m pytest tests/test_central_backend_cli.py -q`
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
