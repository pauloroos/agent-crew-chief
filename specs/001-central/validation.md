# Phase-1 validation and handoff

**Date**: 2026-10-21

**Coordinator tool**: separate released ACC v0.3.4, not development scripts

**Job**: `20261021-0722-central`

**Spec**: `specs/001-central`

**Implementation authorization**: pending owner approval

## Recorded ACC commands

`CC` below means `python -I` followed by the separately installed release's
`skill/crew-chief/scripts/crew-chief.py`. Commands are shown without local
machine paths; the actual run always supplied this development project explicitly.

```text
CC spec new --project . --title central
CC job new --project . --title central --request-file specs/001-central/request.md
CC spec import --project . --job 20261021-0722-central --spec-dir specs/001-central --json
CC spec status --project . --job 20261021-0722-central --json
CC brief render --project . --job 20261021-0722-central --piece N --out <temporary-brief>
```

The final import reported no new pieces (idempotent re-import), with all warning
arrays empty: `missing_proof`, `orphan_sidecar`, `orphan_pieces`, `stale`,
`sidecar_mismatch`, `parallel_conflicts`. Initial import created 40 pieces.
The model check recorded a baseline of 73 available executors; it dispatched none.

## Checks

- Exactly 40 unchecked tasks match 40 sidecar entries and 40 imported job pieces.
- All 26 FR and 11 SC references are covered; every cited US/AC exists.
- All dependency IDs exist and point backwards; no dependency cycles.
- Every task supplies type, risk, acceptance refs, files, proof, expected output, dependencies and interfaces.
- Every focused proof's test file is owned by that task. Release-wide proofs occur only at T030/T040 after their prerequisites; no proof needs a later task's deliverable.
- `[P]` tasks in each phase have disjoint allowed files; dependency gates still apply.
- All 40 execution briefs render without unresolved criteria, including blocked tasks as read-only previews.
- The owner's global preferences retain the same SHA-256 digest as before planning; no setup or installer ran.
- Public files were checked for local drive paths, home-directory paths, names from the private visual draft and credential-like values.
- Only planning documents and ACC project records are staged; production code, tests, installer, package version and changelog remain unchanged.

`spec status` reports:

| Implementation phase | Accepted / total |
| --- | --- |
| 1: Foundation | 0 / 6 |
| 2: Launch and adapters | 0 / 8 |
| 3: Supervision and lifecycle | 0 / 6 |
| 4: Metrics and terminal | 0 / 6 |
| 5: Core verification and release gate | 0 / 4 |
| 6: Web view | 0 / 5 |
| 7: Optional Orca backend | 0 / 3 |
| 8: Web and Orca release gate | 0 / 2 |

Ready: `T001`, `T005`, `T006`. All others are dependency/phase blocked. These are
implementation phases; the owner's current phase 1 is preparation only.

## Start phase 2 only after approval

From the development worktree, with `ACC_RELEASE_DIR` pointing to the existing
released v0.3.4 bundle, the first PowerShell command is:

```powershell
python -I "$env:ACC_RELEASE_DIR/skill/crew-chief/scripts/crew-chief.py" brief render --project . --job 20261021-0722-central --piece 1 --out .crew-chief-brief.md
```

Then use that same release and explicit project argument for `pick --piece 1`,
dispatch with its selected free executor and rendered brief, review at the exact
successful HEAD with a different underlying model family, grade and accept.
Do not run a development-copy coordinator, install another project skill or edit
global preferences. Render/pick separately for every task. No paid escalation
for hands-on work without the existing explicit authorization rules.

Stop after T030 for core review and separate approval before web/Orca. Do not
push, tag or publish. No feature proof or runtime integration test was run in
phase 1: the implementation does not exist yet. Validation here covers the plan,
ACC import/readiness and rendered contracts, not future feature behavior.
