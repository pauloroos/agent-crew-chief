# Plan: Terminais visíveis para toda a crew (ACC 0.12.0)

## Lanes

### Lane 1: `code` — helper de nome + modo tmux + versão
- **T02** — `terminals.py` com `terminal_name(slug, lane, role, attempt)` e testes.
- **T03** — snippet/comandos tmux no pit-wall.md; teste valida strings montadas.
- **T05** — bump para 0.12.0 em `__init__.py` e entrada no CHANGELOG.md.
- **Risco:** normal (muda código e docs internos).
- **Check:** testes passam; versão bate; CHANGELOG tem entrada 0.12.0.

### Lane 2: `docs` — SKILL, pit-wall, race-engineer, READMEs, tutorial
- **T01** — SKILL.md passo 3 e "Without Orca" com os três modos; pit-wall.md
  "Starting and watching drivers" idem; race-engineer.md aponta para eles.
- **T04** — README.md/pt-BR seção "Watching the crew"; tutorial.md/pt-BR com
  `orca terminal list`, `tmux ls`/`attach` e logs.
- **Risco:** normal (docs são o contrato do usuário).
- **Check:** cada arquivo cita os três modos e o nome padrão; traduções batem.

### Lane 3: `proof` — prova no hello-crew + revisão final
- **T06** — rodada pequena no hello-crew em modo Orca; `orca terminal list
  --json` mostra títulos padrão; cleanup remove worktrees e fecha terminais.
- **T07** — revisão linha a linha entre docs, skill e código; lacuna = falha.
- **Risco:** normal (prova gera recursos; cleanup é obrigatório).
- **Check:** JSON no PR; lista de pendências ("validar com tmux real").

## Ordem
- Lanes 1 e 2 rodam em paralelo (não compartilham arquivos).
- Lane 3 depende de 1 e 2 (precisa do código e das docs prontas).

## Modelos (ritmo race)
- `ACC pick --kind code` para T02/T03/T05.
- `ACC pick --kind docs` para T01/T04.
- `ACC pick --kind tests` para T06.
- `ACC pick --kind review` para T07.
- Todos com `--max-cost included`; engenheiro da lane como worker Orca
  (`opencode-go/deepseek-v4.1-flash`), conforme pedido.

## Prova
- Cria duas lanes pequenas no hello-crew (um job de docs cada) só para gerar
  terminais visíveis.
- `orca terminal list --json` captura os títulos.
- `orca terminal close` e `orca worktree rm` limpam ao final.
- O container não tem tmux: teste de string valida o comando montado; o PR
  lista "validar com tmux real" como pendência.