# Lanes visíveis no Orca numa rodada lançada

**Created**: 2026-11-17
**Status**: Draft

## What the user wants

ACC 0.17: numa rodada lançada (`ACC launch --crew`), cada engenheiro de lane
aparece numa aba própria do Orca, com o título padrão
`acc-<slug>-<lane>-engineer`, recebe o brief, roda sozinho até o relatório da
lane, e o crew chief espera por ele e segue o fluxo de sempre (notas, merge,
PR, summary, sync). Sem Orca, continua o background mode.

Hoje (0.16.0) o `CREW_PROMPT` manda a rodada lançada rodar "sem Orca
(background mode)": funciona e entrega PR, mas as lanes somem do Orca — só a
aba do crew chief aparece. No lab, o OpenCode como worker interativo do Orca
ficou parado em "Ask anything" sem receber o brief, e o crew chief encerrou
sem merge nem PR.

## Stories

### User Story 1 - O engenheiro da lane numa aba do Orca (P1)

1. **Given** o Orca de pé e uma rodada lançada, **When** o crew chief inicia o
   engenheiro da lane, **Then** a rodada abre uma aba do Orca com o título
   `acc-<slug>-<lane>-engineer`, o brief vai como prompt (não como worker
   interativo) e o engenheiro roda até o relatório da lane.
2. **Given** uma rodada lançada sem Orca na máquina, **When** o crew chief
   inicia o engenheiro da lane, **Then** o mesmo comando cai no background
   mode (processo com log e PID) e a rodada continua como hoje.
3. O crew chief da rodada espera pelo engenheiro com `ACC runs <id>`
   (estado `finished`/`failed`) em checagens curtas, e não encerra o turno
   antes do PR aberto e do `ACC summary` escrito.

### User Story 2 - O comando que faz isso (P1)

4. **Given** a worktree da lane, **When** o crew chief roda
   `ACC launch --engineer --slug <slug> --lane <lane> --brief <brief>`, **Then**
   o comando monta o título padrão, roda no modo direto (o brief vai como
   está) e decide o lugar: aba do Orca quando o Orca roda aqui, background
   quando não.

### Open questions and edge cases

- Orca no ar mas sem terminal: `ACC launch` já cai no background; a rodada
  não pode quebrar por isso.
- O `--where orca` explícito do pedido vira `--where auto` (padrão): com o
  Orca de pé o resultado é `where=orca`; sem Orca, `--where orca` seria um
  erro. O `auto` dá os dois casos com um comando só.
- Drivers dentro da lane: o engenheiro continua usando o caminho do próprio
  `pit-wall.md` (Orca worker ou background). Não muda nesta versão.

## Requirements

- **FR-001**: `ACC launch --engineer --slug <slug> --lane <lane> --brief <f>`
  inicia o engenheiro da lane: modo direto, título
  `acc-<slug>-<lane>-engineer` (via `crew_chief.terminals.terminal_name`), na
  pasta onde o comando roda (a worktree da lane), `--job-dir`/`--id` da
  rodada, e `--where auto`: aba do Orca quando o Orca roda aqui, background
  quando não.
- **FR-002**: `--engineer` e `--crew` são mutuamente exclusivos; `--engineer`
  exige `--slug` e `--lane`.
- **FR-003**: o `CREW_PROMPT` (rodada `crew`) deixa de mandar "sem Orca
  (background mode)" e manda o caminho novo: iniciar cada engenheiro da lane
  com `ACC launch --engineer`, esperar com `ACC runs <id>` em checagens
  curtas, e não encerrar o turno antes do PR aberto e do `ACC summary`
  escrito.
- **FR-004**: a versão passa a 0.17.0 e o CHANGELOG ganha a seção
  `## [0.17.0]`.
- **FR-005**: `SKILL.md` descreve o caminho na seção do launcher e na seção 3,
  em até 180 linhas.
- **FR-006**: `README.md` e `README.pt-BR.md` contam a mesma história (mesmo
  conteúdo), e os tutoriais (`docs/tutorial.md`, `docs/tutorial.pt-BR.md`)
  fixam a v0.17.0.

## Done when

- **SC-001**: `python -m pytest` passa inteiro (Windows e Linux).
- **SC-002**: um teste novo cobre a rodada lançada com o Orca pronto
  (o engenheiro parte por `launch` com `where=orca` e o título padrão) e sem
  Orca (cai para o background). Nenhum teste toca Orca ou agente de verdade: a
  fixture `no_real_runs` de `tests/conftest.py` continua valendo.
- **SC-003**: PR aberto contra `main`. Sem tag e sem release: o dono testa no
  Orca do servidor antes de publicar.
