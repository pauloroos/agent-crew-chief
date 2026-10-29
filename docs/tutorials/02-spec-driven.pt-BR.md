# Trabalho guiado por especificação

Este tutorial constrói uma funcionalidade pequena a partir de uma pasta no formato spec-kit. Você vai adicionar dois helpers puros de texto e testes focados. Ele pressupõe que você leu [Seu primeiro job em 10 minutos](01-first-job.pt-BR.md), então aponta para aquele tutorial para copiar o exemplo, instalar o ACC, configurar o arquivo isolado de preferências e descobrir os executores.

Todo trabalho de execução neste tutorial usa modelos OpenCode grátis. Não envie trabalho para executor incluído, barato ou pago.

> **Como ler as saídas deste tutorial.** As saídas de comando citadas aqui vêm de uma execução anterior do laboratório deste tutorial, feita com a prova antiga do passo 3. Os passos atualizados — as provas por tarefa, o primeiro dispatch, a revisão, a nota e a linha de economia — aparecem como saída esperada e ilustrativa: nenhuma execução do tutorial atualizado foi registrada ainda, e cada id de job, branch, SHA, duração, contagem de tokens e nota muda de máquina para máquina e de execução para execução. Os blocos marcados como *históricos* são a saída autêntica daquela execução anterior; os blocos marcados como *ilustrativos* são o que os passos atualizados devem imprimir.

## 1. Prepare o laboratório

Siga os passos 1–4 do [primeiro tutorial](01-first-job.pt-BR.md), mas use uma cópia separada, como `~/projects/hello-crew-spec`. Antes de cada comando do ACC, exporte o caminho das preferências do laboratório:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
```

Esse caminho aponta para o arquivo de preferências criado dentro do projeto copiado, então este tutorial nunca toca nas suas configurações normais do ACC.

No Windows PowerShell, atribua o mesmo caminho antes de cada chamada do ACC. O PowerShell não tem a string `$CC` para expandir, então chame o script direto:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew-spec/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
```

O mesmo mapeamento vale antes de cada chamada do ACC deste tutorial; um caminho diferente é ignorado.

O comando de configuração mostra um resumo de degraus como este do laboratório:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

Trate essas contagens como ilustrativas: elas mudam com o tempo e de uma máquina para outra, conforme os CLIs que você tem instalados. O que importa é que apareça pelo menos um executor OpenCode grátis. Se não houver executor OpenCode grátis, pare e configure um. Não use um fallback pago neste tutorial.

## 2. Crie a pasta da especificação

No projeto Git copiado, crie a pasta numerada da especificação:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
proof = '''python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import normalize; assert normalize('hello crew') == 'hello crew'; assert normalize('  hello   crew  ') == 'hello crew'; assert normalize('') == ''; assert normalize('   ') == ''; print('passed')"'''
expect = "passed"
depends = []
interfaces = { normalize = "def normalize(text) -> str" }

[tasks.T002]
type = "code"
risk = "low"
accept = ["FR-002", "US1/AC2"]
files = ["src/hello_crew/text_tools.py"]
proof = '''python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import word_count; assert word_count('hello crew') == 2; assert word_count('  hello   crew  ') == 2; assert word_count('') == 0; assert word_count('   ') == 0; print('passed')"'''
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

Cada prova verifica a própria tarefa. T001 e T002 leem o módulo em `src` e verificam o comportamento do helper que lhes pertence, então nenhuma das duas precisa de um arquivo de outra tarefa. T003 é a tarefa de verificação: roda pytest sobre o módulo pronto, o que é legítimo porque suas dependências já foram aceitas até lá. Mantenha `expect = "passed"` nas três entradas.

Uma tarefa que se prova não pode depender de um arquivo fora da própria lista `files`. Se uma prova só passa depois que outra tarefa escreveu um arquivo, essa prova pertence à tarefa errada: corrija a prova, não o escopo.

Mantenha o texto da tarefa que `spec import` escreve em cada entrada do sidecar quando você usar uma versão mais nova ou mais antiga do ACC. Esse texto permite que o ACC detecte peças desatualizadas depois.

## 4. Crie e importe o job

Escreva o pedido e depois crie o job. A primeira linha é trabalho normal do shell; a segunda é um comando do ACC:

```bash
printf '%s\n' 'Implement the text-tools spec in specs/001-text-tools. For T001, add only src/hello_crew/text_tools.py with normalize(text): use split-and-join whitespace normalization. Run the proof from the generated brief.' > .crew-chief-request.md

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC job new --project . --title "Text tools" --request-file .crew-chief-request.md
```

O laboratório mostrou este id de job:

```text
20261020-1452-text-tools
```

O seu id será diferente. Use-o abaixo como `JOB`.

Importe a especificação:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC spec import --project . --job JOB --spec-dir specs/001-text-tools
```

O laboratório mostrou:

```text
piece 1: T001 (code, low)
piece 2: T002 (code, low)
piece 3: T003 (tests, low)
parallel conflict: T001/T002 share src/hello_crew/text_tools.py
```

O conflito é um aviso, não um motivo para parar. Não envie essas duas peças ao mesmo tempo. Um job criado por `job new` + `spec import` registra a fase de especificação, então o `dispatch` de peças de implementação é recusado até que as tarefas da spec sejam aprovadas por uma revisão de um modelo distinto do coordenador (uma peça docs/spec-review ainda é permitida).

Confira o que está pronto:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --out .crew-chief-brief.md
```

Você deve ver `wrote .crew-chief-brief.md`. Com o sidecar do passo 3, o brief traz os critérios de aceite, as restrições, a interface, o arquivo permitido e esta prova:

```text
## Proof
Command: `python -c "import sys; sys.path.insert(0, 'src'); from hello_crew.text_tools import normalize; assert normalize('hello crew') == 'hello crew'; assert normalize('  hello   crew  ') == 'hello crew'; assert normalize('') == ''; assert normalize('   ') == ''; print('passed')"`
Expect: passed
```

A prova lê o módulo em `src` e verifica o helper no lugar. T001 nunca precisa do arquivo de testes que T003 escreve, então a tarefa fica dentro dos próprios arquivos permitidos.

Escolha um executor OpenCode grátis:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

A execução anterior do laboratório escolheu:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use o id exato do OpenCode grátis mostrado pela sua execução. Envie-o com o brief gerado:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Um dispatch bem-sucedido mostra este formato (ilustrativo — seu id de job, branch, duração e contagem de tokens são diferentes):

```text
status: ok
branch: crew/<job-id>-1-1
worktree: ../hello-crew-spec.crew/<job-id>-1-1
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 129.0s
steps: 21
tokens: 942551 (in 932015 / out 10536)
proof: passed
```

Não espere linha `scope:`: o executor alterou apenas `src/hello_crew/text_tools.py`, o arquivo que T001 permite. Nada precisou ser adicionado para a prova passar, então a peça não tem extra para contar.

Leia a tentativa registrada antes de aceitá-la. Não aceite `proof_failed`; ela conta como tentativa que falhou. Leia, corrija a causa, escolha outro modelo OpenCode grátis e envie de novo. Troque `opencode:opencode-go/beam-free` no comando abaixo pelo id que acabou de falhar:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude opencode:opencode-go/beam-free

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Nunca adicione um arquivo fora da lista `files` da tarefa só para fazer a prova passar. Uma prova que precisa de um arquivo de outra tarefa é uma prova quebrada: corrija a prova, não o escopo.

### Histórico: a execução anterior sob a prova antiga

Uma execução anterior deste tutorial deu a T001 e T002 uma prova pytest que precisava do arquivo de testes que T003 escreve. Em um projeto novo essa prova não passaria sem tocar em um arquivo de fora da tarefa, então a primeira tentativa falhou:

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

Aquela execução escolheu outro modelo OpenCode grátis e enviou de novo; a nova tentativa usou `opencode:opencode-go/minimax-m3.1-flash-free` e mostrou:

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

Ali a linha `scope:` importa: a prova passou, mas o executor também havia adicionado o arquivo de testes, que está fora da lista `files` de T001, então o revisor marcou como extra e aquela execução deu nota 90 em T001. Fique com a lição, não com os números: leia a tentativa registrada antes de tentar de novo e nunca adicione um arquivo fora do escopo só para satisfazer uma prova. As provas por tarefa do passo 3 eliminam essa armadilha — T001 prova `normalize` e T002 prova `word_count` a partir de `src`, e só T003, cujas dependências já foram aceitas, roda pytest.

## 6. Faça a revisão com um brief gerado

Escolha um revisor OpenCode grátis de outra família de modelo subjacente e depois gere o brief de revisão. Se o `pick` propuser qualquer outra ferramenta, ou um modelo incluído, barato ou pago, exclua o id exato dele com `--exclude` e escolha de novo. Aplique essa regra tanto às revisões quanto à implementação: não envie Antigravity, Codex, Claude, executor incluído, barato ou pago neste tutorial.

Exclua o modelo que escreveu o código, nas duas rotas. Trate `opencode:opencode/MODEL` e `opencode:opencode-go/MODEL` como o mesmo modelo subjacente para uma revisão independente: quando a tarefa foi implementada por um deles, exclua os dois. O ACC pode tratar os dois ids como famílias de cota diferentes, então a exclusão precisa citar os dois, mesmo porque o modelo por trás deles é o mesmo. Troque `MODEL` no comando abaixo pelo modelo que a sua tentativa de implementação realmente usou — o id que o seu `pick` mostrou — e faça isso em todas as tarefas que você revisar. Se mais de um executor produziu uma tentativa bem-sucedida naquela peça (uma repetição depois de uma falha, por exemplo), exclua os aliases de todos eles.

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude opencode:opencode/MODEL opencode:opencode-go/MODEL

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC brief render --project . --job JOB --piece 1 --review --out .crew-chief-review.md
```

O `--exclude` aceita uma lista de ids, então junte todos os ids que quer deixar de fora na mesma flag e rode o `pick` de novo. Um revisor proposto por outra ferramenta é excluído sem dispatch; nada dele é executado.

O brief de revisão gerado traz os critérios, os arquivos permitidos e os dois veredictos. Acrescente as regras que o produto não pode deduzir, para o revisor declarar o HEAD que conferiu e não esconder um arquivo de que não precisava:

```bash
cat >> .crew-chief-review.md <<'EOF'

Start your final answer with `HEAD confirmed: <actual short SHA>`. Report every changed file outside the allowed-files list as extra, even if that file is needed by the proof command. Do not infer that a passing proof permits additional files.

Global edge case from the spec: Empty or whitespace-only text should normalize to an empty string and count as zero words.
EOF
```

No PowerShell, o mesmo acréscimo:

```powershell
@'

Start your final answer with `HEAD confirmed: <actual short SHA>`. Report every changed file outside the allowed-files list as extra, even if that file is needed by the proof command. Do not infer that a passing proof permits additional files.

Global edge case from the spec: Empty or whitespace-only text should normalize to an empty string and count as zero words.
'@ | Add-Content .crew-chief-review.md
```

Rode o acréscimo depois de cada `brief render`: renderizar o brief de novo sobrescreve o arquivo.

Envie a revisão contra a worktree da tentativa de implementação bem-sucedida:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

Na execução anterior do laboratório a revisão veio do `opencode:opencode/beam-free` grátis, uma família de modelo diferente da do executor que escreveu o código naquela execução. Use o id exato do OpenCode grátis que o seu `pick` mostrar. Se um revisor OpenCode grátis retornar `status: quota`, leia a tentativa registrada, exclua aquele id e escolha outro revisor OpenCode grátis; nunca volte para outra ferramenta.

Uma revisão bem-sucedida responde `status: ok` e estes veredictos (ilustrativo — o SHA e os achados dependem da worktree):

```text
HEAD confirmed: <short SHA of the reviewed worktree>

## Spec verdict
- FR-001: done
- US1/AC1: done

`extra`: none

## Quality verdict
No Critical or Important findings.
```

Use a `worktree` da tentativa de implementação bem-sucedida, não um caminho novo. Se o revisor disser que o `HEAD` está errado ou omitir a confirmação de HEAD exigida, descarte essa revisão e envie de novo para a worktree correta.

## 7. Aceite as tarefas e rode as provas

Dê a nota e aceite T001 depois de ler a revisão. Quando a revisão não relata nenhum arquivo fora da lista permitida, não há extra para contar:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 2 --missing 0 --extra 0

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

Espere esta saída (ilustrativo — a nota segue a sua revisão; sem extras ela é 100):

```text
score: 100
adherence 2/2 → 100; 0 corrections; 0 extra; penalties none → 100
tasks.md has other changes; marked T001 but did not commit
accepted crew/<job-id>-1-1
```

Conte um extra só quando a revisão realmente relatar um. Cada extra custa 5 pontos, e um arquivo fora da lista `files` da tarefa é um extra mesmo quando a prova precisava dele. A execução anterior do laboratório, cujo executor adicionou o arquivo de testes, deu nota 90 em T001.

O comando `accept` muda T001 de `[ ]` para `[X]`. Como os arquivos da especificação já tinham outras mudanças, ele não criou um commit separado só para marcar a tarefa.

Repita o mesmo ciclo de render, pick, dispatch, grade e accept para T002. Na execução anterior do laboratório, o executor OpenCode grátis foi `opencode:opencode/glm-5.3-flash-free`; a prova passou e a nota foi 100. Depois envie T003, que agora está desbloqueada. Aquela execução usou `opencode:opencode/deepseek-v4.1-flash-free`; a prova passou e a nota foi 100.

Confira a especificação completa:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
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
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC ruling add --project . --job JOB --text "Keep text helpers pure and stdlib-only — predictable whitespace behavior is enough for this small feature — adding validation now would expand the API."

export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC decision add --project . --job JOB --from-ruling 1 --context "The feature needs two small helpers with no runtime dependency." --alternatives "Add a third-party text library; validate every input type." --consequences "Callers get a small predictable API; non-string validation remains outside this feature."
```

O laboratório mostrou `ruling 1 recorded` e criou:

```text
docs/decisions/0001-keep-text-helpers-pure-and-stdlib-only.md
```

Feche a funcionalidade:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-spec/global-prefs.toml
$CC close --project . --job JOB --type feat
```

O comando mostra `changelog: CHANGELOG.md (Added)`, uma mensagem de commit de funcionalidade, as notas de cada tarefa/executor e esta linha de economia (ilustrativo — seus números dependem da sua execução):

```text
free work: 100% of pieces, 100% of recorded tokens · coordinator: not recorded · 1.0 attempts/piece · mean score 100
```

Leia a linha antes de comemorar:

- `attempts/piece` conta apenas tentativas de execução. Tentativas que não fizeram trabalho — `quota` e `brief_refused` — ficam fora da contagem, e os dispatches de revisão também não são contados. Quando cada uma das três peças precisou de uma tentativa de execução, o número é 1.0.
- Reporte os dispatches totais separadamente quando descrever novas tentativas. Para as três peças acima isso daria seis dispatches: três execuções e três revisões.
- coordinator: not recorded significa que não há uso do coordenador registrado. A porcentagem grátis cobre apenas os tokens registrados; o custo real da coordenação permanece desconhecido até você registrá-lo com usage add. Uma entrada explícita de zero tokens é diferente de nenhuma entrada: ela mostra coordinator: 0% quando há tokens de execução.
- `mean score 100` é a média das três notas do passo 7 quando as três forem 100. Não diz nada sobre o custo da execução.

O `close` adiciona o job ao `CHANGELOG.md` e mostra a mensagem de commit. Faça o commit do trabalho aceito, do ADR e da entrada no changelog conforme o fluxo do seu projeto.

## Se você editar tasks.md depois

Uma peça guarda o texto da tarefa que existia quando foi importada. Se você gerar ou editar `tasks.md`, a peça fica desatualizada quando esse texto muda. Rode `spec import` novamente depois de corrigir a tarefa e o sidecar. Até lá, `brief render` e `dispatch` recusam a peça desatualizada. Se o spec-kit renumerar um id, confira também o texto `task` no sidecar; o ACC avisa quando uma entrada como T012 foi escrita para outra tarefa. Não tente contornar isso com `--ignore-deps`: essa opção é para verificações de dependência, não para texto de tarefa desatualizado.

Se você adicionar uma tarefa, inclua a tabela dela no sidecar e uma prova antes do dispatch. Se mudar os arquivos permitidos, as referências de aceite ou a prova, gere um brief novo e leia a linha `scope:` antes de aceitar.

## O que aconteceu na execução anterior do laboratório

- Arquivos escritos: `spec.md`, `plan.md`, `tasks.md`, `crew.toml`, as saídas das três tarefas, um ADR e `CHANGELOG.md` no laboratório.
- Comandos executados: `spec new`, `setup`, `discover`, `job new`, `spec import`, `spec status`, `brief render`, `pick`, `dispatch`, `grade`, `accept`, `ruling add`, `decision add` e `close`.
- Notas do produto: uma prova por tarefa confere o próprio arquivo da tarefa, então nenhum arquivo fora do escopo é preciso para fazê-la passar; `scope:` lista os arquivos que estão fora da lista permitida da tarefa; `accept` avisa e não cria seu próprio commit quando `tasks.md` já tem outras mudanças; uma resposta de cota é registrada como tentativa que falhou e exige outro revisor grátis.
