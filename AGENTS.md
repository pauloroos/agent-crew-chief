# Agents working on this repository

This repository is coordinated by Agent Crew Chief (ACC).

## Git flow rules
- ACC always works on its own feature branch and delivers by pull request.
- Never commit or push to `main`.
- Never use `--force` or rewrite pushed history; never tag.
- Push the feature branch after each accepted item.
- ACC opens the pull request; only the owner merges it.
- If a push or pull request creation fails, the round stops and reports the exact command and error.
- Never create, copy or request new credentials, keys or tokens; use the GitHub access already configured.

## Docs rule
- Every version that changes what ACC does updates its docs in the same pull
  request: `README.md` and `README.pt-BR.md` (same content), `docs/tutorial.md`
  and `docs/tutorial.pt-BR.md` whenever a step, a role or an output changes,
  `skill/crew-chief/SKILL.md` and `pit-wall.md`, and `CHANGELOG.md`.
- New roles, names, commands, files and logs are described for the user, not
  only in the skill.
- Before the pull request, a review compares the docs with the code and the
  skill; a gap is a failed job, not a follow-up.
