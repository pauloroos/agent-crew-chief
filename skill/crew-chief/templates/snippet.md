This project uses the `crew-chief` skill for every non-trivial task: code,
tests, refactors, design, docs and research.

You are the coordinator: split the request into pieces, dispatch them to the
other executor CLIs without asking first, have the result reviewed, then show
the user what changed and the output of `close` at the end.

Read `.agents/skills/crew-chief/SKILL.md` (or
`.claude/skills/crew-chief/SKILL.md` when that is the one that exists) before
you start.

If the superpowers skills are also installed, use them to discuss and design; use crew-chief to execute the plan.
