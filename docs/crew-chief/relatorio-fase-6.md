# Fase 6: Web view

Data: 2026-10-22. Job: `20261021-0722-central`.
Branch: `feat/v0.4.1-web`, a partir da release `v0.4.0` (`2108dbb`).
Orquestração: ACC **v0.4.0 lançado**, sem setup ou edição das preferências globais.

## Resultado

**T031–T035 aceitas; nenhuma tarefa pulada.** As fases 7 e 8 não foram iniciadas.

O registro pendente do job foi commitado primeiro. T030 foi regularizada pela
API de registros do ACC lançado, com `attempt: null`, origem `release` e sem
adicionar tentativa. A ruling 100 registra exatamente:

> T030 shipped in v0.4.0 at 2108dbb; accepted from the release, no new attempt

A caixa de T030 foi marcada e commitada antes de iniciar T031. As tarefas já
aceitas das fases anteriores foram preservadas.

| Tarefa | Entrega | Prova repetida na branch após aceite |
| --- | --- | --- |
| T031 | HTTP em loopback, assets permitidos e snapshot público | 29 passed, 1 skipped |
| T032 | Cards, gráficos, histórico, perguntas e scoreboard | 13 passed |
| T033 | Mensagens, respostas, pausa, retomada e intenção de attach autorizadas | 38 passed |
| T034 | `acc central --web [--port PORT]`, ponte e encerramento | 19 passed |
| T035 | Integração HTTP com registros reais temporários e paridade com terminal | 10 passed |

O servidor usa somente `127.0.0.1`, porta efêmera por padrão e biblioteca padrão.
Não há recursos externos no HTML/CSS/JS. Host, Origin, token, campos e UUIDs são
validados; JSON e filas são limitados. Retentativas de ações não duplicam efeitos.
Attach somente entra pela ponte do terminal; sem ponte, retorna `acc attach ID`.
Textos entram no DOM com `textContent`; caminhos locais e credenciais reconhecidas
são filtrados antes de servir o snapshot. Campos com credenciais são mascarados
inteiros para impedir vazamento de fragmentos.

As provas e revisões encontraram defeitos que foram reproduzidos antes da correção:
variantes de campos de credenciais, valores desconhecidos apresentados como zero,
duplicação de gráficos, validação e deduplicação de ações, fila de attach fictícia,
abertura do navegador antes do atendimento HTTP e attach com entrada redirecionada.
T035 também encontrou divergência no relógio: o transporte agora passa UTC em ISO
para a API compartilhada. O módulo do núcleo não foi reimplementado.

As correções pontuais posteriores ao aceite mantiveram os aceites e o trabalho
existente; tiveram novas provas e revisão no SHA correspondente. Não houve
exclusão de arquivos existentes nem remoção de testes preexistentes.

## Execução, revisões e custo

Cada tarefa teve **duas tentativas gratuitas falhas antes da execução própria**,
conforme a exceção autorizada pelo dono. Trabalho parcial útil foi aproveitado.
As decisões, correções, notas e falhas permanecem no [registro do job](jobs/20261021-0722-central.md).

Foram **10 dispatches de execução**: 4 com quota e 6 com timeout. Modelos usados:

- `opencode:opencode/deepseek-v4.1-flash-free`
- `opencode:opencode-go/minimax-m3.1-flash-free`
- `opencode:opencode-go/beam-free`
- `opencode:opencode/glm-5.3-flash-free`
- `opencode:opencode/mimo-v2.6-flash-free`
- `opencode:opencode/nemotron-3-ultra-free`
- `opencode:opencode/nemotron-3.5-super-vl-free`
- `opencode:opencode/flint-40b-free`

Foram **11 dispatches de revisão gratuita**, 10 concluídos e 1 com timeout:
Flint, Sonnet 5.5 low, Opus 5.5 low, Gemini 4 Argon low e Gemini 3.8 Flash low.
Os quatro últimos foram usados via Antigravity, somente com `--review`.
As revisões confirmaram os SHAs e foram de famílias diferentes das autoras.
Os aceites finais não deixaram achados Critical/Important pendentes.

Todos os **21 dispatches** desta fase foram de tier `free`. Não foram despachados
executores included, cheap ou paid. Há nove registros manuais de conclusão ou
correção pelo coordenador; seu custo, duração e tokens não medidos permanecem
desconhecidos, sem atribuição falsa de trabalho gratuito.

Saída de `CC economy` para o **job inteiro**, incluindo as fases anteriores:

```text
free work: 68% of pieces, 100% of recorded tokens · coordinator: not recorded · 3.0 attempts/piece · mean score 61
alert: 3.0 attempts per piece (limit 2.5)
alert: job took 1588 min (limit 180)
```

Esses alertas são do histórico do job; não representam somente a duração desta
fase. O percentual de tokens cobre os registros disponíveis, não o custo total
da coordenação. Não se estima economia monetária.

## Verificação final

Comando no checkout da branch: `python -m pytest -q`.

**1775 passed, 7 skipped, 1 warning in 537.81s (0:08:57)**, exit code 0,
na primeira execução completa, sem repetição. Ambiente: Windows, Python 3.14.7.

O aviso vem do teste de supervisor que grava metadados sintéticos de rodada sem
identidade. Esse teste e o módulo de supervisor não foram alterados nesta fase.
A prova de T031 inclui um skip por permissão de symlink no Windows.

As provas Node de DOM e do cliente foram executadas. Não havia navegador
disponível na sessão nem Playwright instalado; não se afirma smoke visual real.
Não foi executada CI em outros sistemas operacionais nesta rodada.

Para smoke visual com dados sintéticos, na raiz do checkout:

```text
python tests/test_central_web_assets.py --smoke
```

Abra a URL efêmera impressa. Confira cards, perguntas, histórico, gráficos,
scoreboard, texto HTML literal, teclado e largura estreita. Na aba de rede,
confirme recursos somente do loopback. Termine com Ctrl-C. O fixture usa diretório
temporário e snapshot sintético, sem lançar coordenadores ou modelos; ele é visual,
enquanto os efeitos das ações são verificados pelos testes HTTP isolados.

Após a suíte, a inspeção de processos identificou **zero processos próprios
restantes** de executores, revisores, pytest ou supervisores dos fixtures desta
execução. Os testes verificam encerramento de threads e sockets em `finally`.
Os commits novos usam a autoria solicitada e não possuem trailers de coautoria.

## Continuidade

Perguntas para o dono: **nenhuma**. `CC spec status` mostra fase 6 **5/5** e
**T036 pronta**. A fase 7 pode começar em uma próxima rodada; não foi iniciada.
O job permanece aberto para T036–T040. Versão, CHANGELOG e gate de release ficam
para a fase 8. Não houve push, tag, publicação ou merge em main.
