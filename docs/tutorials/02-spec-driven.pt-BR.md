# Trabalho guiado por especificação

Este tutorial constrói uma funcionalidade pequena a partir de uma pasta no formato spec-kit. Você vai adicionar dois helpers puros de texto e testes focados. Ele pressupõe que você leu [Seu primeiro job em 10 minutos](01-first-job.pt-BR.md), então aponta para aquele tutorial para copiar o exemplo, instalar o ACC, configurar o arquivo isolado de preferências e descobrir os executores.

Todo trabalho de execução neste tutorial usa modelos OpenCode grátis. Não envie trabalho para executor incluído, barato ou pago.

## 1. Prepare o laboratório

Siga os passos 1–4 do [primeiro tutorial](01-first-job.pt-BR.md), mas use uma cópia separada, como `~/projects/hello-crew-spec`. Antes de cada comando do ACC, exporte o caminho das preferências do laboratório:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
```

O comando de configuração produziu este resumo real no laboratório:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

As contagens exatas dependem da sua máquina. Se não houver executor OpenCode grátis, pare e configure um. Não use um fallback pago neste tutorial.

## 2. Crie a pasta da especificação

No projeto Git copiado, crie a pasta numerada da especificação:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec new --project . --title "Text tools"
```

Você deve ver:

```text
specs/001-text-tools
```

Se aparecer outro número, use a pasta mostrada pelo seu comando. Substitua o conteúdo dos templates gerados por este `spec.md` mínimo:

```markdown
# Text tools

**Created**: 2026-10-20
**Status**: Draft

## What the user wants

A small text-tools module should make common text cleanup predictable. It will provide a normalize helper and a word-count helper with focused tests.

## Stories

### User Story 1 - Clean and count text (Priority: P1)

As a Python caller, I want to normalize text and count its words so that small text-processing tasks use one consistent module.

**Acceptance Scenarios**:

1. **Given** text with extra whitespace, **When** the caller normalizes it, **Then** leading and trailing whitespace is removed and internal runs become one space.
2. **Given** normalized or ordinary text, **When** the caller counts words, **Then** the result is the number of non-empty whitespace-separated words.

### Open questions and edge cases

- Empty or whitespace-only text should normalize to an empty string and count as zero words.

## Requirements

- **FR-001**: Provide `normalize(text)` with the whitespace behavior in the acceptance scenarios.
- **FR-002**: Provide `word_count(text)` that returns the number of non-empty whitespace-separated words.

## Done when

- **SC-001**: Focused tests cover ordinary, repeated-whitespace, and empty input for both helpers.
```

Use este `plan.md`:

```markdown
# Implementation Plan: Text tools

**Spec**: spec.md

## Summary

Add a small stdlib-only module with two pure functions. Keep the public API simple and verify behavior with focused pytest tests.

## Technical Context

- **Language/Version**: Python 3.11+
- **Dependencies**: none at runtime
- **Storage**: n/a
- **Testing**: pytest

## Project Structure

```text
src/hello_crew/text_tools.py
 tests/test_text_tools.py
```

## Decisions

- Use `str.split()` for whitespace handling because it drops empty runs without extra parsing.
```

Use estas linhas de tarefas em `tasks.md`:

```markdown
# Tasks: Text tools

## Phase 1: Foundational

- [ ] T001 [P] [US1] Add `normalize(text)` in src/hello_crew/text_tools.py
- [ ] T002 [P] [US1] Add `word_count(text)` in src/hello_crew/text_tools.py

## Phase 2: Verification

- [ ] T003 [US1] Add focused tests in tests/test_text_tools.py; depends on T001, T002
```

`[P]` diz que uma tarefa pode rodar em paralelo. Aqui T001 e T002 usam o mesmo arquivo, então o ACC avisa sobre esse conflito. Rode-as uma por vez.

## 3. Adicione o sidecar crew.toml

A especificação descreve a funcionalidade, mas o executor também precisa dos arquivos permitidos, do comando de prova e das dependências. Crie `specs/001-text-tools/crew.toml`:

```toml
global_constraints = ["Python 3.11+, stdlib only at runtime", "Keep public helpers pure and small"]

[tasks.T001]
type = "code"
risk = "low"
accept = ["FR-001", "US1/AC1"]
files = ["src/hello_crew/text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = []
interfaces = { normalize = "def normalize(text) -> str" }

[tasks.T002]
type = "code"
risk = "low"
accept = ["FR-002", "US1/AC2"]
files = ["src/hello_crew/text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = []
interfaces = { word_count = "def word_count(text) -> int" }

[tasks.T003]
type = "tests"
risk = "low"
accept = ["SC-001"]
files = ["tests/test_text_tools.py"]
proof = "python -m pytest tests/test_text_tools.py -q"
expect = "passed"
depends = ["T001", "T002"]
interfaces = {}
```

Mantenha o texto da tarefa que `spec import` escreve em cada entrada do sidecar quando você usar uma versão mais nova ou mais antiga do ACC. Esse texto permite que o ACC detecte peças desatualizadas depois.

## 4. Crie e importe o job

Escreva o pedido e depois crie o job. A primeira linha é trabalho normal do shell; a segunda é um comando do ACC:

```bash
printf '%s\n' 'Implement the text-tools spec in specs/001-text-tools. For T001, add only src/hello_crew/text_tools.py with normalize(text): use split-and-join whitespace normalization. Run the proof from the generated brief.' > .crew-chief-request.md

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC job new --project . --title "Text tools" --request-file .crew-chief-request.md
```

O laboratório mostrou este id de job:

```text
20261020-1452-text-tools
```

O seu id será diferente. Use-o abaixo como `JOB`.

Importe a especificação:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec import --project . --job JOB --spec-dir specs/001-text-tools
```

O laboratório mostrou:

```text
piece 1: T001 (code, low)
piece 2: T002 (code, low)
piece 3: T003 (tests, low)
parallel conflict: T001/T002 share src/hello_crew/text_tools.py
```

O conflito é um aviso, não um motivo para parar. Não envie essas duas peças ao mesmo tempo.

Confira o que está pronto:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec status --project . --job JOB
```

Você deve ver:

```text
Phase 1: Foundational: 0/2 accepted
Phase 2: Verification: 0/1 accepted
ready: T001, T002
blocked:
  T003: depends on T001
```

## 5. Gere o brief de uma tarefa e envie

Gere T001 em vez de escrever um brief grande à mão:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --out .crew-chief-brief.md
```

O laboratório mostrou `wrote .crew-chief-brief.md`. O brief continha os critérios de aceite, as restrições, a interface, o arquivo permitido e esta prova:

```text
## Proof
Command: `python -m pytest tests/test_text_tools.py -q`
Expect: passed
```

Escolha um executor OpenCode grátis:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

O laboratório escolheu:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use o id exato do OpenCode grátis mostrado pela sua execução.

### Uma falha de prova real

O primeiro dispatch do laboratório retornou `proof_failed`. O executor alterou um arquivo fora da lista permitida de T001, então os testes gerados não estavam onde a prova esperava:

```text
status: proof_failed
branch: crew/20261020-1452-text-tools-1-1
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 27.5s
steps: 10
tokens: 233672 (in 230748 / out 2924)
proof: failed (exit 4)
scope: src/hello_crew/__init__.py
```

Leia a tentativa registrada antes de tentar de novo. Não aceite `proof_failed`; ela conta como tentativa que falhou.

Escolha outro modelo OpenCode grátis e envie novamente:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude opencode:opencode-go/beam-free

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

A tentativa bem-sucedida do laboratório usou `opencode:opencode-go/minimax-m3.1-flash-free` e mostrou:

```text
status: ok
branch: crew/20261020-1452-text-tools-1-2
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 129.0s
steps: 21
tokens: 942551 (in 932015 / out 10536)
proof: passed
scope: tests/test_text_tools.py
```

A linha `scope:` importa. A prova passou, mas o executor também adicionou o arquivo de testes. Isso está fora de `files` de T001, então o revisor marcou como extra e a nota foi 90. Decida se esse extra pertence a uma tarefa posterior antes de aceitar trabalho parecido.

## 6. Faça a revisão com um brief gerado

Escolha um revisor de outra família e depois gere o brief de revisão:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude EXECUTOR

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --review --out .crew-chief-review.md
```

O laboratório primeiro escolheu um revisor Claude do Antigravity, mas o dispatch retornou esta resposta honesta de cota:

```text
status: quota
branch: -
worktree: -
base: -
destructive_suspect: -
outside_writes: -
duration: 9.5s
steps: 1
tokens: 0 (in 0 / out 0)
answer:
Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 140h1m53s.
```

Escolha de novo, excluindo esse revisor. O laboratório então usou `opencode:opencode/beam-free`, grátis, e revisou a worktree bem-sucedida:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

A revisão retornou `status: ok`, `HEAD confirmed` e estes veredictos:

```text
## Spec verdict
- FR-001: done
- US1/AC1: done

`extra`: Added `tests/test_text_tools.py` (not in the allowed-files list, though required by the proof command); added `__all__` and a detailed docstring beyond the requested helper.

## Quality verdict
No Critical or Important findings.
```

Use a `worktree` da tentativa de implementação bem-sucedida, não um caminho novo. Se o revisor disser que o `HEAD` está errado, descarte essa revisão e envie de novo para a worktree correta.

## 7. Aceite as tarefas e rode as provas

Dê a nota e aceite T001 depois de ler a revisão:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 2 --missing 0 --extra 2

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

O laboratório mostrou:

```text
score: 90
adherence 2/2 → 100; 0 corrections; 2 extra −10; penalties none → 90
tasks.md has other changes; marked T001 but did not commit
accepted crew/20261020-1452-text-tools-1-2
```

O comando `accept` muda T001 de `[ ]` para `[X]`. Como os arquivos da especificação já tinham outras mudanças, ele não criou um commit separado só para marcar a tarefa.

Repita o mesmo ciclo de render, pick, dispatch, grade e accept para T002. No laboratório, o executor OpenCode grátis foi `opencode:opencode/glm-5.3-flash-free`; a prova passou e a nota foi 100. Depois envie T003, que agora está desbloqueada. O laboratório usou `opencode:opencode/deepseek-v4.1-flash-free`; a prova passou e a nota foi 100.

Confira a especificação completa:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC spec status --project . --job JOB
```

O laboratório mostrou:

```text
Phase 1: Foundational: 2/2 accepted
Phase 2: Verification: 1/1 accepted
ready: -
blocked: -
```

## 8. Registre uma decisão e feche

Para uma escolha de design que deve durar, registre uma decisão de uma linha e promova-a a ADR:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC ruling add --project . --job JOB --text "Keep text helpers pure and stdlib-only — predictable whitespace behavior is enough for this small feature — adding validation now would expand the API."

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC decision add --project . --job JOB --from-ruling 1 --context "The feature needs two small helpers with no runtime dependency." --alternatives "Add a third-party text library; validate every input type." --consequences "Callers get a small predictable API; non-string validation remains outside this feature."
```

O laboratório mostrou `ruling 1 recorded` e criou:

```text
docs/decisions/0001-keep-text-helpers-pure-and-stdlib-only.md
```

Feche a funcionalidade:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec-lab/global-prefs.toml
$CC close --project . --job JOB --type feat
```

O laboratório mostrou `changelog: CHANGELOG.md (Added)`, uma mensagem de commit de funcionalidade, as notas de cada tarefa/executor e esta linha de economia:

```text
free work: 100% of pieces, 100% of tokens · coordinator: 0% · 1.7 attempts/piece · mean score 97
```

O `close` adiciona o job ao `CHANGELOG.md` e mostra a mensagem de commit. Faça o commit do trabalho aceito, do ADR e da entrada no changelog conforme o fluxo do seu projeto.

## Se você editar tasks.md depois

Uma peça guarda o texto da tarefa que existia quando foi importada. Se você gerar ou editar `tasks.md`, a peça fica desatualizada quando esse texto muda. Rode `spec import` novamente depois de corrigir a tarefa e o sidecar. Até lá, `brief render` e `dispatch` recusam a peça desatualizada. Se o spec-kit renumerar um id, confira também o texto `task` no sidecar; o ACC avisa quando uma entrada como T012 foi escrita para outra tarefa. Não tente contornar isso com `--ignore-deps`: essa opção é para verificações de dependência, não para texto de tarefa desatualizado.

Se você adicionar uma tarefa, inclua a tabela dela no sidecar e uma prova antes do dispatch. Se mudar os arquivos permitidos, as referências de aceite ou a prova, gere um brief novo e leia a linha `scope:` antes de aceitar.

## O que aconteceu nesta execução

- Arquivos escritos: `spec.md`, `plan.md`, `tasks.md`, `crew.toml`, as saídas das três tarefas, um ADR e `CHANGELOG.md` no laboratório.
- Comandos executados: `spec new`, `setup`, `discover`, `job new`, `spec import`, `spec status`, `brief render`, `pick`, `dispatch`, `grade`, `accept`, `ruling add`, `decision add` e `close`.
- Notas do produto: uma prova pode passar enquanto `scope:` mostra um arquivo extra; `accept` avisa e não cria seu próprio commit quando `tasks.md` já tem outras mudanças; uma resposta de cota é registrada como tentativa que falhou e exige outro revisor grátis.
