# Tasks: Terminais visíveis para toda a crew (ACC 0.12.0)

## T01 — Regra de terminais no SKILL.md e pit-wall.md [P]
- **Kind:** docs
- **Risk:** normal
- **Check:** SKILL.md passo 3 e seção "Without Orca" descrevem os dois modos (worker/terminal Orca, processo em segundo plano); pit-wall.md "Starting and watching drivers" tem os mesmos dois modos com comandos reais; templates/race-engineer.md aponta para eles.
- **Depends on:** —

## T02 — Nome padrão e helper de título [P]
- **Kind:** code
- **Risk:** low
- **Check:** função `terminal_name(slug, lane, job, role, attempt)` em `skill/crew-chief/scripts/crew_chief/terminals.py` com testes unitários; formato `acc-<slug>-<lane>-<job>-<role>[-<n>]`.
- **Depends on:** —

## T03 — Docs do usuário (README, tutorial) [P]
- **Kind:** docs
- **Risk:** low
- **Check:** README.md e README.pt-BR.md documentam os dois modos e as worktrees filhas; tutorial.md e tutorial.pt-BR.md mostram `orca terminal list` e logs.
- **Depends on:** T01

## T04 — Versão 0.12.0 e CHANGELOG [P]
- **Kind:** code
- **Risk:** low
- **Check:** `__version__ = "0.12.0"` em `__init__.py`; CHANGELOG.md tem entrada 0.12.0 com as mudanças desta versão.
- **Depends on:** T01, T02, T03

## T05 — Prova no hello-crew (modo Orca)
- **Kind:** tests
- **Risk:** normal
- **Check:** rodada pequena no hello-crew gera worktrees e terminais; `orca terminal list --json` lista títulos padrão; cleanup remove worktrees e fecha terminais; PR mostra o JSON.
- **Depends on:** T01, T02, T04

## T06 — Revisão de docs vs código vs skill
- **Kind:** review
- **Risk:** normal
- **Check:** comparação linha a linha entre SKILL.md, pit-wall.md, race-engineer.md, READMEs, tutorial e o código; lacuna = falha.
- **Depends on:** T05
