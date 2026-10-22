# Review T039 Document browser and optional Orca operation in tests/test_central_web_docs.py, README.md, docs/tutorials/04-central.md; depends on T035, T038

Do not trust the executor's report; read the diff.

## Where to review
Work in E:\DEV\acc-v041.crew\20261021-0722-central-39-1. Confirm `git rev-parse --short HEAD` is e9e29f3. Review `git diff 9a995f9..HEAD`.
If HEAD is not e9e29f3, stop and answer only: WRONG HEAD <what you found>.

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

## Files allowed
- tests/test_central_web_docs.py
- README.md
- docs/tutorials/04-central.md

## Proof
Command: `python -m pytest tests/test_central_web_docs.py -q`
Expect: passed

## Spec verdict
For each acceptance criterion above, answer `done | missing | misunderstood`.
Then list `extra`: anything the diff does that no criterion asked for.

## Quality verdict
List findings as `Critical | Important | Minor`, each with file and line.
Judge the code, not the spec: bugs, edge cases, tests, readability.
Bloat is a finding: an abstraction, option, dependency, wrapper or "for later" code no criterion needs is `Important`.
