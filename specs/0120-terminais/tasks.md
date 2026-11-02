# Tasks: Terminais visíveis para toda a crew (ACC 0.12.0)

## T01 — Regra de terminais no SKILL.md e pit-wall.md [P]
- **Kind:** docs
- **Risk:** normal
- **Check:** SKILL.md passo 3 e seção "Without Orca" descrevem os três modos (Orca worker/terminal, tmux, bg); pit-wall.md "Starting and watching drivers" tem os mesmos três modos com comandos reais; templates/race-engineer.md aponta para eles.
- **Depends on:** —

## T02 — Nome padrão e helper de título [P]
- **Kind:** code
- **Risk:** low
- **Check:** função `terminal_name(slug, lane, role, attempt)` em `scripts/crew_chief/terminals.py` com testes unitários; formato `acc-<slug>-<lane>-<role>[-<n>]`.
- **Depends on:** —

## T03 — Modo tmux no pit-wall.md e scripts [P]
- **Kind:** code
- **Risk:** normal
- **Check:** pit-wall.md descreve sessão tmux nomeada + pipe-pane + kill-session; script ou snippet monta os comandos; teste valida strings montadas (sem tmux real).
- **Depends on:** T02

## T04 — Docs do usuário (README, tutorial) [P]
- **Kind:** docs
- **Risk:** low
- **Check:** README.md e README.pt-BR.md têm seção "Watching the crew"; tutorial.md e tutorial.pt-BR.md mostram `orca terminal list`, `tmux ls`/`attach` e logs.
- **Depends on:** T01

## T05 — Versão 0.12.0 e CHANGELOG [P]
- **Kind:** code
- **Risk:** low
- **Check:** `__version__ = "0.12.0"` em `__init__.py`; CHANGELOG.md tem entrada 0.12.0 com as mudanças desta versão.
- **Depends on:** T01, T02, T03, T04

## T06 — Prova no hello-crew (modo Orca)
- **Kind:** tests
- **Risk:** normal
- **Check:** rodada pequena no hello-crew gera worktrees e terminais; `orca terminal list --json` lista títulos padrão; cleanup remove worktrees e fecha terminais; PR mostra o JSON e lista "validar com tmux real".
- **Depends on:** T01, T02, T03, T05

## T07 — Revisão de docs vs código vs skill
- **Kind:** review
- **Risk:** normal
- **Check:** comparação linha a linha entre SKILL.md, pit-wall.md, race-engineer.md, READMEs, tutorial e o código; lacuna = falha.
- **Depends on:** T06