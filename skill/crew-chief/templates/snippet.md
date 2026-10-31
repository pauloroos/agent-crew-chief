When the user asks to have agents do the work ("ACC", "crew chief", "load
ACC, do X"), use the `crew-chief` skill: read
`.agents/skills/crew-chief/SKILL.md` (or `.claude/skills/crew-chief/SKILL.md`)
first. It runs the work in parallel lanes through Orca and scores every model.
