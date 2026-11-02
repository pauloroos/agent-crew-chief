# Spec: Terminais visíveis para toda a crew (ACC 0.12.0)

## Problema

No modo Orca, o SKILL.md atual manda iniciar o race engineer "com o seu próprio
mecanismo de subagente" (passo 3). Quando o crew chief é o Claude Code, o
engenheiro vira subagente interno: a worktree aparece no Orca, mas nenhum
terminal. Drivers e scrutineers que o engenheiro dispara rodam como processo em
segundo plano, também sem terminal. O dono quer abrir o Orca (ou qualquer
IDE/CLI) e ver, em cada worktree, os terminais "filhos" da crew, entrar neles e
acompanhar ao vivo, seja qual for o harness. Quando o chefe foi o OpenCode
(rodada do Vard 0.10.0), isso já aconteceu por acaso, via `orca orchestration
worker-start`; a regra precisa garantir isso.

## Escopo

1. **Modo Orca:** o race engineer sobe SEMPRE como worker do Orca na worktree
   da lane (`orca orchestration worker-start`), nunca como subagente do chefe,
   em qualquer harness. Drivers e scrutineers também sobem como terminais do
   Orca na worktree da lane: worker supervisionado (`worker-start` / `orca agent
   launch`) quando o Orca conhece o agente; senão `orca terminal create
   --worktree path:<wt> --title <título> --command "<comando do driver do
   pit-wall.md>"`. As flags reais foram conferidas com `orca <cmd> --help` neste
   container.
2. **Sem Orca, com tmux no PATH:** cada engenheiro, driver e scrutineer roda
   numa sessão tmux nomeada (`acc-<slug>-<lane>-<papel>[-<n>]`), com o log
   gravado no mesmo arquivo de hoje (`tmux pipe-pane`), de modo que `tmux ls`
   lista a crew e `tmux attach -t <nome>` entra no terminal de qualquer IDE,
   CLI ou SSH. Matar a sessão substitui o kill pelo PID.
3. **Sem Orca e sem tmux** (ex.: Windows sem WSL): segundo plano com log e PID,
   como hoje.
4. **Nome padrão** em todos os modos (título da aba no Orca = nome da sessão
   tmux = nome do log): lane, papel (engineer/driver/scrutineer) e número da
   tentativa. Curto e legível.
5. **Docs (regra do AGENTS.md):** `README.md` e `README.pt-BR.md`,
   `docs/tutorial.md` e `docs/tutorial.pt-BR.md` (como acompanhar: aba no Orca,
   `tmux ls`/`attach`, logs), `skill/crew-chief/SKILL.md`,
   `skill/crew-chief/pit-wall.md`, `skill/crew-chief/templates/race-engineer.md`,
   `CHANGELOG.md` (0.12.0) e a versão no código. Antes do PR, um job de revisão
   compara docs, skill e código; lacuna é falha, não pendência.
6. **Prova:** no `examples/hello-crew`, rodar uma rodada pequena em modo Orca e
   mostrar, com `orca terminal list --json`, os terminais de cada worktree de
   lane com os títulos padrão. O container não tem tmux: validar o modo tmux
   com testes dos comandos montados e listar "validar com tmux real" no PR. Não
   instalar nada no sistema. Fechar os terminais e remover as worktrees da prova
   ao final.

Fora do escopo: mexer no ledger, no pick, no sync ou nos models.

## Suposições registradas

- O pedido diz "ritmo race" e "aprovação já está dada": este spec e o plano são
  escritos e executados sem parada de aprovação.
- O nome padrão usa o formato `acc-<slug>-<lane>-<papel>` para a primeira
  tentativa e `acc-<slug>-<lane>-<papel>-<n>` para a n-ésima, onde `<slug>` vem
  do nome da branch da feature (`acc/<slug>` → `<slug>`). Exemplo:
  `acc-terminais-lane1-engineer`, `acc-terminais-lane1-driver-2`.
- No modo Orca, quando o agente é conhecido pelo Orca (`--agent <id>`), usa-se
  `orca orchestration worker-start` com `--name <nome-padrão>`; quando não é,
  usa-se `orca terminal create --worktree path:<wt> --title <nome-padrão>
  --command "<cmd>"`. O título da aba no Orca vem de `--name` (worker) ou
  `--title` (terminal).
- No modo tmux, o log vai para `../<repo>-acc-logs/<nome-padrão>.log`, o mesmo
  caminho de hoje, e a sessão tmux recebe `pipe-pane` para esse arquivo. O PID
  gravado é o da sessão tmux (`tmux display-message -p '#{session_pid}'`), e
  `kill` mata a sessão (`tmux kill-session -t <nome>`).
- A versão sobe para 0.12.0 em `skill/crew-chief/scripts/crew_chief/__init__.py`.
- O exemplo `hello-crew` já existe e serve para a prova; a prova cria lanes
  pequenas (um job de docs por lane) só para gerar terminais visíveis.
- O container não tem tmux instalado; os testes de tmux validam apenas os
  comandos montados (string matching), não a execução real. O PR lista isso
  como pendência de validação manual.

## Riscos

- **Normal:** mudança no SKILL.md e pit-wall.md afeta todos os futuros runs; a
  revisão de docs é o check.
- **Low:** mudança de versão e changelog.
- **Normal:** prova no hello-crew gera worktrees e terminais; cleanup no final
  evita lixo. Se o cleanup falhar, o PR reporta os recursos restantes.