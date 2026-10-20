# Custo e segurança sem surpresas

Este tutorial continua o [01-first-job.md](01-first-job.md). Ele usa o mesmo projeto pequeno, mas foca em escolher modelos, limitar gastos e proteger arquivos.

Nos exemplos, `CC` é o CLI instalado:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

Use um arquivo global de preferências só para o laboratório. Antes de **cada** comando do ACC, exporte esse arquivo. Não aponte para suas preferências normais.

## 1. Comece com padrões seguros

Se ainda não criou o projeto de prática, siga o [01-first-job.md](01-first-job.md) até o primeiro commit e a instalação. Depois rode o setup com `--yes`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC setup --yes --project .
```

No laboratório, apareceu este resumo:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 65
```

As quantidades exatas mudam conforme os CLIs e modelos instalados. `--yes` escolhe não assumir assinatura paga, orçamento `0` para modelos baratos e não permite modelos grátis que treinam com seu tráfego.

Sem `--yes`, o `setup` faz uma pergunta por vez: se você paga uma assinatura Claude, quanto por dia permite para modelos baratos e se aceita modelos grátis que treinam. Rode esse formato num terminal de verdade. Sem terminal, o setup termina sem escrever; use `--yes`.

## 2. Veja o custo de cada executor

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC discover --project .
```

O laboratório mostrou entradas neste formato:

```text
opencode:opencode-go/beam-free  cost=free  trains=no
opencode:opencode-go/minimax-m3.1-flash-free  cost=free  trains=no
claude:sonnet  cost=included  trains=no
skipped: codex: no models listed
skipped: cursor: no models listed

models: baseline: 91 executors recorded

work types: opencode: all types, first on execution work
```

A saída completa também listou modelos pagos do OpenCode e modelos grátis do Antigravity. Não copie contagens ou listas de modelos para um documento permanente: os provedores mudam isso.

`discover` é sua consulta de preço. `free` não custa dinheiro nem exige assinatura. `included` é coberto por uma assinatura que você já paga. `cheap` é pago por uso dentro de um orçamento. `paid` é o fallback pago forte. `forbidden` fica fora da escada.

## 3. Deixe o `pick` subir somente quando você permitir

Crie um job e uma peça como no primeiro tutorial. No laboratório, o pick real foi:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Para trabalho braçal (`code`, `tests`, `refactor`, `docs` e `data`), o `pick` fica nos executores grátis. Ele não sobe só porque um executor grátis falhou. Quando não sobra executor grátis, manda dividir a peça ou fazer o trabalho você mesmo.

Para trabalho de pensamento (`review`, `design`, `diagnosis` e `research`), o `pick` pode subir a escada. O degrau cheap só é usado enquanto os dois orçamentos permitem. Orçamento diário `0` mantém modelos baratos fechados. Quando não há setup ou orçamento cheap utilizável, a mensagem do produto é `run setup to allow cheap models`.

Para permitir de propósito que trabalho de execução chegue ao degrau pago, use `--strong`. Ele tenta `included` e `cheap` antes de `paid`. É uma exceção, não um botão de repetir:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --strong
```

Use `--coordinator <tool>` para informar qual CLI coordena o job. O próprio CLI do coordenador, ou sua família de assinatura, fica por último entre os `included`. `--allow-own-cli` remove essa preferência.

### Excluindo executores grátis

`pick` aceita vários `--exclude`. Copie todos os ids grátis mostrados pelo seu `discover` quando quiser ver o próximo degrau. Por exemplo:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

Se aparecer um executor `included` ou `cheap`, trate isso apenas como demonstração. **Não faça dispatch dele neste tutorial.** Dispatches reais aqui usam somente `opencode/*-free` ou `opencode-go/*-free`.

## 4. Entenda os controles de gasto

Defina estas chaves no arquivo global quando quiser permitir modelos cheap:

```toml
cheap_budget_usd_day = 2
cheap_budget_usd_job = 1
```

`cheap_budget_usd_day` é compartilhado por todos os projetos. `cheap_budget_usd_job` limita um job. Uma regra de modelo pode acrescentar seu próprio limite:

```toml
[[models]]
match = "opencode:openrouter/*"
cost = "cheap"
cap_usd_day = 1
```

Cada tentativa `cheap` ou `paid` acrescenta uma linha JSON a `spend.jsonl`, ao lado do arquivo global de preferências. Com o caminho do laboratório, isso seria `~/projects/hello-crew-lab/spend.jsonl` apenas como exemplo neutro de localização; seu caminho real é a pasta que contém o arquivo global. Uma linha tem o formato JSONL registrado pelo ACC, incluindo executor, projeto, job, degrau de custo e valor. Não copie uma linha de uma execução paga: nenhum dispatch pago foi feito aqui.

A verificação acontece antes da tentativa. Uma tentativa pode passar do teto diário no máximo pelo próprio custo, e tentativas simultâneas podem passar individualmente antes de o gasto combinado ser conhecido. Um modelo cheap de custo desconhecido ganha uma sondagem por executor por dia, a menos que você defina `cap_usd_day`.

Os alertas de economia são diferentes dos orçamentos de modelos. `close` e `economy` podem alertar quando o coordenador usou mais de 50% dos tokens do job, quando a execução passa de 2,5 tentativas por peça ou quando o job dura mais de 180 minutos. Ajuste `alert_coordinator_share`, `alert_attempts_per_piece` ou `alert_job_minutes`; `0` desliga um alerta. Eles também alertam quando o gasto do dia com modelos baratos chega a 80% do `cheap_budget_usd_day` (só quando esse teto é maior que 0). Leia toda linha `alert:` antes de relatar o job.

## 5. Proíba modelos e treinamento

Uma regra `[[models]]` pode proibir um modelo. `forbid_training = true` também transforma em `forbidden` qualquer modelo cuja regra tenha `trains = true`:

```toml
forbid_training = true

[[models]]
match = "opencode:opencode-go/beam-free"
cost = "forbidden"
trains = false

[[models]]
match = "*-preview-free"
cost = "free"
trains = true
```

A primeira regra que combina vence. Uma regra `forbidden` vence mesmo quando outro arquivo de preferências chama o modelo de grátis. Depois de mudar as preferências do projeto, rode `discover` de novo:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew-lab/global-prefs.toml
$CC discover --project .
```

Depois escolha a peça. No laboratório, após proibir `opencode:opencode-go/beam-free`, o `pick` não o escolheu; escolheu outro OpenCode grátis:

```text
opencode:opencode-go/longcat-2.5-preview-free
unrated, no history (opencode:opencode-go/longcat-2.5-preview-free); tier free
```

Essa é a propriedade útil: o modelo proibido é removido dos candidatos, não apenas colocado no fim da lista.

## 6. Esconda caminhos sensíveis

As preferências do projeto ficam em `docs/crew-chief/preferences.toml`. Adicione caminhos que um executor não deve ver:

```toml
sensitive_paths = ["secret.txt", ".env", "secrets/*"]
```

Um prefixo também cobre nomes relacionados, como `.env.example`. Correspondências rastreadas ficam escondidas da worktree de execução. O ACC verifica novamente depois da execução.

Crie um arquivo rastreado sem conteúdo sensível para este teste e mencione-o no brief. O dispatch real do laboratório produziu:

```text
status: brief_refused
branch: -
worktree: -
base: HEAD
destructive_suspect: -
outside_writes: -
duration: 0.0s
steps: -
tokens: -
```

O status é o ponto importante. O ACC recusou o brief antes de iniciar o executor porque ele mencionava um caminho sensível. Nunca coloque o conteúdo de um segredo no brief.

Um arquivo sensível também deve estar ausente da worktree do executor. Se uma execução o criar ou restaurar, o guard pós-execução registra isso na tentativa. Confira `outside_writes`, `destructive_suspect` e a tentativa registrada antes de aceitar qualquer resultado.

## 7. Ignore entradas inofensivas do home

Alguns apps escrevem configuração ou cache no seu home enquanto rodam. O ACC já ignora pastas conhecidas dos CLIs de agentes. Adicione outra entrada quando necessário:

```toml
ignore_home_entries = [".someapp"]
```

Isso afeta somente a checagem de escrita externa. Não cria um sandbox. O laboratório também mostrou por que você deve inspecionar o resultado: um dispatch grátis real terminou como `outside_write`, com `outside_writes: home: Recent`. Sair sem erro do executor não basta para aceitar a tentativa.

## 8. Adicione seu próprio CLI

Para um CLI parecido com Claude Code, adicione uma entrada de ferramenta e mantenha o segredo numa variável de ambiente:

```toml
[[tools]]
name = "my-claude-proxy"
kind = "claude-code"
bin = "my-claude-proxy"
models = ["<model-id>"]
effort = false
env = ["MY_PROXY_API_KEY"]
cost_default = "paid"
```

`bin` precisa estar no `PATH`. O ACC registra o nome da variável, nunca o valor. Acrescente uma regra `[[models]]` se o CLI tiver um modelo grátis. Um CLI genérico pode usar `run_cmd` e `list_cmd`; veja a seção **Adding a custom CLI** do README.

## O que lembrar

- Rode `setup --yes` primeiro, usando sempre o mesmo caminho global do laboratório.
- Leia `discover` antes de escolher. Ele mostra custo e flag de treinamento de cada executor.
- Use executores OpenCode grátis nos dispatches reais deste tutorial.
- Use `--strong` somente quando quiser permitir escalada paga de propósito.
- Mantenha os orçamentos cheap explícitos e confira `spend.jsonl` ao lado do arquivo global.
- Trate `brief_refused`, `outside_write` e `destructive_suspect` como motivos para parar e inspecionar.

## Relatório do tutorial

- **Arquivos escritos:** este tutorial e `03-cost-and-safety.pt-BR.md`.
- **Comandos executados:** cópia e inicialização do exemplo, primeiro commit, instalação do ACC, `setup --yes`, `discover`, criação de job e peça, `pick`, dispatches OpenCode grátis e testes de modelo proibido e brief com caminho sensível.
- **Comportamentos a notar:** o primeiro dispatch OpenCode grátis reportou uma escrita inesperada no home (`home: Recent`) e foi rejeitado como `outside_write`; um dispatch grátis posterior expirou; um brief que citava caminho sensível foi corretamente rejeitado como `brief_refused`. O setup também exige terminal no modo interativo, então use `--yes` em automação sem terminal.
