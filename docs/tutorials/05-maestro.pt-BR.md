# 05 · O maestro: várias coordenadoras ao mesmo tempo

No tutorial 04 você lançou uma coordenadora. Aqui o agente com quem você
conversa faz isso por você: ele vira o **maestro**, divide um objetivo em
frentes, lança uma coordenadora por frente, mantém todas andando e te entrega
um PR por frente. Cada coordenadora continua rodando o ciclo completo do job
(executores grátis primeiro, revisão por outra família de modelos, notas,
placar) na sua própria worktree.

```
você ─▶ maestro (sua sessão do agente)
          ├─▶ coordenadora A  (worktree + branch próprias) ─▶ executores / revisores ─▶ PR
          ├─▶ coordenadora B  (worktree + branch próprias) ─▶ executores / revisores ─▶ PR
          └─▶ coordenadora C  ...
```

Ele parte do tutorial 04: instale o comando `acc` e o ACC no projeto antes, e
faça commit da instalação.

## 1. Dê um objetivo ao maestro

Abra seu agente (Claude Code ou Codex) no projeto e peça o maestro com um
objetivo que tenha partes independentes:

```text
Use o maestro do crew-chief. Objetivo: adicionar uma função `farewell(name)`
com testes, e uma opção `--shout` no CLI que deixa a saudação em maiúsculas.
```

## 2. Aprove as frentes

O maestro responde com um plano antes de lançar qualquer coisa. Uma frente é
um resultado independente que cabe em um PR e mexe em arquivos que nenhuma
outra frente mexe:

```text
Frente 1  farewell(name) + testes       src/hello_crew/farewell.py, tests/   claude
Frente 2  opção --shout + testes        src/hello_crew/cli.py, tests/cli/   codex
```

Trabalho que depende de outra frente espera o PR dela; duas frentes nunca
dividem arquivos. Aprove uma vez e o maestro escreve o pedido de cada frente
num arquivo próprio.

## 3. Lance

Para cada frente o maestro roda um launch a partir da raiz do projeto:

```bash
acc launch --repo . --request-file fronts/01-farewell.md --mode raw-request --authorize-raw --cli claude
acc launch --repo . --request-file fronts/02-shout.md --mode raw-request --authorize-raw --cli codex
```

Cada launch imprime `launched coordinator <ID>`, com worktree e branch
próprias. Seu checkout nunca é tocado.

## 4. Acompanhe

O maestro lê o estado de todas as coordenadoras num único snapshot JSON, a
cada poucos minutos:

```bash
acc central --json
```

Você pode acompanhar as mesmas coordenadoras com `acc central`. O que o
maestro faz em cada estado:

| Estado | O que o maestro faz |
|---|---|
| uma pergunta pendente | responde com `acc message ID --reply-to QUESTION_ID --text "..."` quando o objetivo já decide; senão pergunta a você e repassa a resposta |
| `quota` | a CLI da coordenadora bateu um limite e ela está parada: `acc resume ID` quando o limite voltar, ou `acc stop ID` e relança a frente com outro `--cli` |
| `paused` (sem progresso) | lê a última rodada, manda orientação com `acc message` e depois `acc resume ID` |
| `failed` | lê o `last_error`; `acc recover ID` só depois de corrigir a causa |
| `completed` | anota a branch e o PR |

## 5. Merge

O maestro entrega cada frente com o PR (ou a branch), as peças aceitas, a nota
e o que ficou faltando. Revise e faça o merge de cada PR você mesmo; o ACC
nunca faz merge, nunca dá push na branch base e nunca cria tag.

## Limites

- As frentes precisam ser independentes. Arquivos em comum geram PRs em
  conflito; divida de outro jeito ou rode as frentes uma depois da outra.
- O maestro é a sua sessão do agente: ela precisa ficar aberta para continuar
  respondendo perguntas. As coordenadoras seguem trabalhando sem ela e as
  perguntas esperam no `acc central`.
- Não há rotação automática de coordenadora: uma quota deixa a coordenadora
  parada até ser retomada ou relançada.
