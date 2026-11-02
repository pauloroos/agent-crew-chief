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
   em qualquer harness. A worktree já existe, então `--name` não é passado; o
   título padrão é aplicado com `orca terminal rename --terminal <handle>
   --title <nome>` depois do start (quando o Chat UI não expõe handle, pula o
   rename e usa `worker-read`). Drivers e scrutineers também sobem como
   terminais do Orca na worktree da lane: worker supervisionado (`worker-start`)
   quando o Orca conhece o agente; senão `orca terminal create --worktree
   path:<wt> --title <título> --command "<comando do driver do pit-wall.md>"`.
   As flags reais foram conferidas com `orca <cmd> --help` neste container.
2. **Sem Orca** (ex.: Windows sem WSL): segundo plano com log e PID, como hoje.
3. **Nome padrão** em todos os modos (título da aba no Orca = nome do log):
   `acc-<slug>-<lane>-<job>-<role>` para a primeira tentativa e
   `acc-<slug>-<lane>-<job>-<role>-<n>` para a n-ésima, onde `<slug>` vem da
   branch da feature (`acc/<slug>` → `<slug>`) e `<job>` é um slug curto do
   título do job. Fiscais paralelos usam `-1`/`-2` (ou números de tentativa
   maiores) para garantir unicidade. Curto e legível.
   *Emenda 2026-11-02 (ACC 0.12.1):* o race engineer é um por lane e cobre
   vários jobs, então omite `<job>`: `acc-<slug>-<lane>-engineer` e
   `acc-<slug>-<lane>-engineer-<n>`. Drivers e scrutineers mantêm o formato
   acima.
4. **Docs (regra do AGENTS.md):** `README.md` e `README.pt-BR.md`,
   `docs/tutorial.md` e `docs/tutorial.pt-BR.md` (como acompanhar: aba no Orca,
   logs), `skill/crew-chief/SKILL.md`, `skill/crew-chief/pit-wall.md`,
   `skill/crew-chief/templates/race-engineer.md`, `CHANGELOG.md` (0.12.0) e a
   versão no código. Antes do PR, um job de revisão compara docs, skill e
   código; lacuna é falha, não pendência.
5. **Prova:** no `examples/hello-crew`, rodar uma rodada pequena em modo Orca e
   mostrar, com `orca terminal list --json`, os terminais de cada worktree de
   lane com os títulos padrão. Fechar os terminais e remover as worktrees da
   prova ao final.

Fora do escopo: mexer no ledger, no pick, no sync ou nos models.

## Suposições registradas

- O pedido diz "ritmo race" e "aprovação já está dada": este spec e o plano são
  escritos e executados sem parada de aprovação.
- O nome padrão usa o formato `acc-<slug>-<lane>-<job>-<papel>` para a primeira
  tentativa e `acc-<slug>-<lane>-<job>-<papel>-<n>` para a n-ésima (o engineer
  não tem `<job>`, ver a emenda no item 3), onde
  `<slug>` vem do nome da branch da feature (`acc/<slug>` → `<slug>`) e `<job>`
  é um slug curto do título do job. Exemplo:
  `acc-terminais-lane1-greet-driver`, `acc-terminais-lane1-cli-driver-2`.
- No modo Orca, quando o agente é conhecido pelo Orca (`--agent <id>`), usa-se
  `orca orchestration worker-start` sem `--name` (a worktree já existe); o
  título da aba é aplicado com `orca terminal rename` depois. Quando o agente
  não é conhecido, usa-se `orca terminal create --worktree path:<wt> --title
  <nome-padrão> --command "<cmd>"`.
- A versão sobe para 0.12.0 em `skill/crew-chief/scripts/crew_chief/__init__.py`.
- O exemplo `hello-crew` já existe e serve para a prova; a prova cria lanes
  pequenas (um job de docs por lane) só para gerar terminais visíveis.

## Riscos

- **Normal:** mudança no SKILL.md e pit-wall.md afeta todos os futuros runs; a
  revisão de docs é o check.
- **Low:** mudança de versão e changelog.
- **Normal:** prova no hello-crew gera worktrees e terminais; cleanup no final
  evita lixo. Se o cleanup falhar, o PR reporta os recursos restantes.