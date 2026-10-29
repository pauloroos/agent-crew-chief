# 04 · O maestro: uma coordenadora por frente

Aqui o agente com quem você conversa vira o **maestro**: divide um objetivo em
frentes, lança uma coordenadora por frente, mantém todas andando e te entrega
um PR por frente. Cada coordenadora roda o ciclo completo do job (executores
grátis primeiro, revisão por outra família de modelos, notas, placar) na sua
própria worktree, e cada rodada dela é cronometrada e contada.

```
você ─▶ maestro (a sessão do seu agente)
          ├─▶ coordenadora A  (worktree + branch próprias) ─▶ executores / revisores ─▶ PR
          ├─▶ coordenadora B  (worktree + branch próprias) ─▶ executores / revisores ─▶ PR
          └─▶ coordenadora C  ...
```

Parte dos tutoriais 01–03 e usa o projeto
[`examples/hello-crew/`](../../examples/hello-crew/).

## 1. Instale o comando `acc`

```bash
git clone --branch v0.5.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py --command
acc --version
```

O instalador grava o `acc` (`acc.cmd` no Windows) na sua pasta de binários de
usuário e mostra onde; ponha essa pasta no `PATH` se ainda não estiver.

## 2. Escolha as coordenadoras

Liste-as em `docs/crew-chief/preferences.toml`, na ordem em que são tentadas, e
faça commit do arquivo (cada coordenadora trabalha numa worktree nova do `HEAD`):

```toml
[team]
coordinators = ["opencode:omni/chefe", "claude:opus", "codex:gpt-6.1-sol"]
```

Qualquer ferramenta configurada serve (`claude`, `codex`, `opencode`,
`antigravity`, uma ferramenta `generic`). Quando a primeira bate cota ou falha,
a próxima assume a mesma worktree e o mesmo job sozinha.

## 3. Dê um objetivo ao maestro

Abra o seu agente no projeto e peça o maestro com um objetivo que tenha partes
independentes:

```text
Use o maestro do crew-chief. Objetivo: adicionar uma função `farewell(name)`
com testes e uma opção `--shout` no CLI que deixa a saudação em maiúsculas.
```

Ele responde com as frentes antes de lançar qualquer coisa: um resultado
independente por PR, nunca duas frentes nos mesmos arquivos. Aprove uma vez.

## 4. Lance e acompanhe

Para cada frente o maestro roda:

```bash
acc launch --request-file fronts/01-farewell.md
acc launch --request-file fronts/02-shout.md --coordinator codex:gpt-6.1-sol
```

e depois, a cada poucos minutos:

```bash
acc status
```

```text
3f9a1c2e  01-farewell  running  coordinator opencode:omni/chefe  rounds 1 (switches 0)  12m40s  coordinator tokens 48211
    job 20261029-farewell: free work: 100% of pieces, 81% of tokens · coordinator: 19% · 1.5 attempts/piece · mean score 88
```

| Estado | O que o maestro faz |
|---|---|
| `question` | responde com `acc resume ID --answer "..."` quando o objetivo já decide; senão pergunta para você e repassa a resposta |
| `quota` | todas as coordenadoras da lista bateram limite: `acc resume ID` quando uma voltar |
| `failed` | lê o `last_error` e os logs em `<projeto>.crew/coordinators/ID/`; só retoma depois de corrigir a causa |
| `completed` | anota o PR |

## 5. Merge

O maestro reporta cada frente com o PR, rodadas, tempo, tokens, pedaços
aceitos e nota. Revise e faça o merge de cada PR você mesmo; o ACC nunca faz
merge, push na branch base nem tag.

## Limites

- As frentes precisam ser independentes; arquivos em comum dão PRs em conflito.
- Uma coordenadora tem seis rodadas por execução; `acc resume` dá mais seis.
- `acc stop ID` para uma coordenadora; a worktree e a branch ficam para você.
