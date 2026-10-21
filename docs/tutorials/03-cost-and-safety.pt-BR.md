# Custo e segurança sem surpresas

Este tutorial continua o [01-first-job.md](01-first-job.md). Ele usa o mesmo projeto pequeno, mas foca em escolher modelos, limitar gastos e proteger arquivos.

## Antes de começar

Escolha uma raiz de laboratório: um diretório seu que guarde o arquivo global de preferências isolado deste tutorial. Nada aqui toca suas preferências normais do ACC.

Nos exemplos, `CC` é o CLI instalado:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

Antes de **cada** comando do ACC, exporte o caminho das preferências do laboratório. Não aponte para suas preferências normais.

```bash
export LAB_ROOT="$HOME/projects/hello-crew-lab"
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
```

Todos os blocos de comando abaixo repetem esse export, então uma nova janela só precisa de `LAB_ROOT` uma vez.

No Windows, use a seção de PowerShell do tutorial 01 para todos os comandos aqui. Troque `$HOME` pela sua raiz de laboratório, defina `$env:AGENT_CREW_CHIEF_GLOBAL_PREFS` antes de cada comando do ACC e chame o `python -I` direto: o PowerShell não expande uma string `$CC` em comando. A seção [PowerShell no Windows](#powershell-no-windows) no fim deste tutorial repete todos os comandos deste tutorial em PowerShell.

Dois placeholders aparecem abaixo. `JOB` é o id do job de prática que você cria no passo 3, e `DEMO_JOB` é um segundo job, usado só pelas demonstrações da escada do passo 3. Troque os dois pelos ids que o seu `job new` imprimiu.

Todo dispatch real deste tutorial usa um executor OpenCode grátis: um id no formato `opencode:opencode/*-free` ou `opencode:opencode-go/*-free`. Confira o degrau do id exato no `discover` antes de cada dispatch. Um pick que devolve qualquer outra coisa é só uma demonstração — não despache.

## 1. Comece com padrões seguros

Se ainda não criou o projeto de prática, siga o [01-first-job.md](01-first-job.md) até o primeiro commit e a instalação. Depois rode o setup com `--yes`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC setup --yes --project .
```

No laboratório, apareceu este resumo:

```text
your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 66
```

As quantidades exatas mudam conforme os CLIs e modelos instalados. O `--yes` responde não às perguntas de assinatura que ele não consegue deduzir, define um teto `0` para modelos baratos e não permite modelos grátis que treinam com seu tráfego. Credenciais encontradas na máquina ainda contam: um CLI cuja conta já está conectada pode ser classificado como `included` sem nenhuma pergunta. Então o `--yes` não restringe todo candidato a `free` — leia a saída do `discover` do passo 2 antes de escolher.

O setup escreve um bloco gerenciado no arquivo global de preferências, entre `# >>> acc setup >>>` e `# <<< acc setup <<<`. Este é o bloco da rodada de laboratório; o seu lista os CLIs que a sua máquina tem:

```toml
# >>> acc setup >>>
cheap_budget_usd_day = 0
forbid_training = true

[[models]]
match = "codex:*"
cost = "included"

[[models]]
match = "claude:*"
cost = "included"
# <<< acc setup <<<
```

As chaves escalares ficam acima das tabelas `[[models]]` de propósito. Em TOML, uma chave escrita depois de uma tabela pertence a essa tabela, então uma chave de orçamento colada no fim do arquivo seria lida como parte da última regra e ignorada. Mantenha toda chave escalar — as chaves de orçamento, `forbid_training`, `sensitive_paths`, `ignore_home_entries` — acima da primeira tabela `[[models]]` ou `[[tools]]`.

Sem `--yes`, o `setup` faz uma pergunta por vez: se você paga uma assinatura Claude, quanto por dia permite para modelos baratos e se aceita modelos grátis que treinam. Rode esse formato num terminal de verdade. Sem terminal, o setup termina sem escrever; use `--yes`.

## 2. Veja o custo de cada executor

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
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

Antes de cada dispatch real deste tutorial, confira duas coisas nessa saída: o campo `cost=` do id exato e o formato do próprio id. Só `opencode:opencode/*-free` e `opencode:opencode-go/*-free` são usados em dispatches reais aqui.

## 3. Deixe o `pick` subir somente quando você permitir

O `pick` sobe uma escada, do mais barato ao mais caro: `free`, depois `included` (uma assinatura que você já paga), depois `cheap` (pago por uso, só enquanto o orçamento permite) e por último `paid`. Um degrau só é abandonado quando não há ninguém disponível nele — nunca porque um executor grátis apenas falhou.

- Para trabalho braçal (`code`, `tests`, `refactor`, `docs` e `data`), o `pick` tenta primeiro os executores grátis. Quando não sobra executor grátis, ele ainda pode escolher um executor `included`, ou um `cheap` enquanto o orçamento permitir, sem `--strong`. Só o `paid` de trabalho braçal exige `--strong`.
- Para trabalho de pensamento (`review`, `design`, `diagnosis` e `research`), o `pick` continua subindo a mesma escada e pode chegar a `paid` sozinho.

Neste tutorial, trate toda seleção não-grátis como uma demonstração. Não despache.

### Dois jobs: o de verdade e o de demonstração

O `pick` registra toda escolha no job, e um pick com `--strong` registra também a escalada, mesmo que nenhum dispatch venha depois. O `close` pode imprimir essa escalada no comentário da entrega. Para manter limpo o registro de entrega do job de verdade, rode as experiências da escada em um job de demonstração separado.

O job de demonstração precisa do seu próprio arquivo de pedido:

```bash
printf '%s\n' 'Demonstration only: compare the cost tiers of pick. Do not dispatch this piece.' > .crew-chief-demo-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC job new --project . --title "Cost ladder demo" --request-file .crew-chief-demo-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC piece add --project . --job DEMO_JOB --title "Show the cost ladder" --type code --risk low
```

O `job new` imprime o id: use-o como `DEMO_JOB` a partir de agora. O `piece add` imprime `1`, o número da peça do job de demonstração.

### O job normal

O job de prática é criado aqui, com o mesmo pedido do tutorial 01. Se você já o criou lá, reuse o id dele como `JOB` e pule estes dois comandos; caso contrário, rode-os e use o id impresso como `JOB`:

```bash
printf '%s\n' 'Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test.' > .crew-chief-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

O `job new` imprime o id: use-o como `JOB` a partir de agora. O `piece add` imprime `1`, o número da peça usado por todo pick e dispatch abaixo.

### O pick normal

Escolha a peça do job de verdade:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
```

No laboratório, o pick escolheu um executor OpenCode grátis, com o degrau na segunda linha:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

Use o id exato do executor impresso na primeira linha. É esse id que um dispatch precisa usar.

### A demonstração com `--strong`

O `--strong` tira todo executor grátis do conjunto e é a única forma de o trabalho braçal chegar a um executor pago. Ele tenta `included` e `cheap` antes de `paid`. É uma exceção, não um botão de repetir, e pertence ao job de demonstração:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job DEMO_JOB --piece 1 --strong
```

No laboratório, apareceu algo neste formato:

```text
claude:opus
second chance: unrated claude:opus; tier included; climbed: free exhausted
```

`climbed: free exhausted` diz que o pick teve de sair do degrau grátis. Não significa que o degrau grátis era o último disponível: `included` vem depois, depois `cheap` enquanto o orçamento permite e por último `paid`. Nenhum dispatch vem depois deste comando, e a escalada fica registrada no `DEMO_JOB`.

Use `--coordinator <tool>` para informar qual CLI coordena o job. O próprio CLI do coordenador, ou sua família de assinatura, fica por último entre os `included`. `--allow-own-cli` remove essa preferência.

### Excluindo executores

Cada `--exclude` soma ids, então repetir a opção acumula: `--exclude ID_1 --exclude ID_2` deixa de fora os dois. Uma única opção com vários ids também funciona (`--exclude ID_1 ID_2`). Copie todos os ids grátis mostrados pelo `discover` quando quiser ver o próximo degrau:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job DEMO_JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

Se aparecer um executor `included` ou `cheap`, trate isso apenas como demonstração. **Não faça dispatch dele neste tutorial.** Dispatches reais aqui usam somente `opencode/*-free` ou `opencode-go/*-free`.

### Revisores ficam fora da família do autor

O `pick --review` trata a escolha como trabalho de pensamento e deixa de fora a família de cota de todo executor com uma tentativa `ok` na peça. No OpenCode, as duas rotas para o mesmo modelo são uma família só: `opencode:opencode/minimax-m3.1-flash-free` e `opencode:opencode-go/minimax-m3.1-flash-free` nomeiam o mesmo modelo, então o ACC deixa as duas de fora sozinho. Você não precisa excluir na mão o alias do autor para conseguir um revisor de outro modelo.

## 4. Entenda os controles de gasto

Defina estas chaves no arquivo global quando quiser permitir modelos cheap:

```toml
cheap_budget_usd_day = 2
cheap_budget_usd_job = 1
```

Mantenha-as acima da primeira tabela `[[models]]` ou `[[tools]]` desse arquivo, como faz o bloco do setup do passo 1. Uma chave de orçamento colada depois de uma tabela muda de escopo em vez de definir o orçamento.

`cheap_budget_usd_day` é compartilhado por todos os projetos. `cheap_budget_usd_job` limita um job. Uma regra de modelo pode acrescentar seu próprio limite:

```toml
[[models]]
match = "opencode:openrouter/*"
cost = "cheap"
cap_usd_day = 1
```

Um teto diário cheap igual a zero fecha só o degrau cheap. Ele não proíbe executores `included` nem `paid`. Um teto cheap por job igual a zero significa que não há teto adicional do job. Para este tutorial, verifique o degrau grátis no `discover` e o id exato `opencode/*-free` ou `opencode-go/*-free` antes de cada dispatch real.

Cada tentativa `cheap` ou `paid` acrescenta uma linha JSON a `spend.jsonl`, ao lado do arquivo global de preferências. Com o caminho do laboratório, isso seria `$LAB_ROOT/spend.jsonl` apenas como exemplo neutro de localização; seu caminho real é a pasta que contém o arquivo global. Uma linha tem o formato JSONL registrado pelo ACC, incluindo executor, projeto, job, degrau de custo e valor. Não copie uma linha de uma execução paga: nenhum dispatch pago foi feito aqui.

A verificação acontece antes da tentativa. Uma tentativa pode passar do teto diário no máximo pelo próprio custo, e tentativas simultâneas podem passar individualmente antes de o gasto combinado ser conhecido. Um modelo cheap de custo desconhecido ganha uma sondagem por executor por dia, a menos que você defina `cap_usd_day`.

Os alertas de economia são diferentes dos orçamentos de modelos. `close` e `economy` podem alertar quando o coordenador usou mais de 50% dos tokens do job, quando a execução passa de 2,5 tentativas por peça ou quando o job dura mais de 180 minutos. Ajuste `alert_coordinator_share`, `alert_attempts_per_piece` ou `alert_job_minutes`; `0` desliga um alerta. Eles também alertam quando o gasto do dia com modelos baratos chega a 80% do `cheap_budget_usd_day` (só quando esse teto é maior que 0). Leia toda linha `alert:` antes de relatar o job.

## 5. Proíba modelos e treinamento

Uma regra `[[models]]` pode proibir um modelo. `forbid_training = true` também transforma em `forbidden` qualquer modelo cuja regra tenha `trains = true`. Acrescente estas regras nas preferências do projeto, `docs/crew-chief/preferences.toml`, com a chave escalar acima da primeira tabela:

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

Coloque regras exatas de modelo antes das regras mais amplas que também combinem com ele: entre regras comuns de custo, a primeira que combina vence. Uma regra explícita `cost = "forbidden"` sempre vence, mesmo se uma regra ampla grátis aparecer antes ou outro arquivo de preferências chamar o modelo de grátis. A regra `*-preview-free` acima não combina com `beam-free`; ela marca os modelos de prévia correspondentes como modelos que treinam. Depois de mudar as preferências do projeto, rode `discover` de novo:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC discover --project .
```

Depois escolha a peça do job de verdade. O modelo proibido sai dos candidatos, não apenas vai para o fim da lista. Uma saída ilustrativa, com um modelo que é grátis e não treina com seu tráfego:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
```

```text
opencode:opencode-go/minimax-m3.1-flash-free
unrated, no history (opencode:opencode-go/minimax-m3.1-flash-free); tier free
```

Essa é a propriedade útil: o modelo proibido é removido dos candidatos.

Com `forbid_training = true`, qualquer modelo que uma regra combinante marque com `trains = true` também fica proibido, então o `pick` não vai escolhê-lo; a regra `*-preview-free` acima é uma dessas. Essa proteção é por regra e por flag: confira o campo `trains=` no `discover` para ver a flag que o ACC realmente tem para um modelo. Ela não afirma por si só como um provedor trata cada requisição, apenas que o ACC exclui os modelos que marcou como treinando com o seu tráfego.

## 6. Esconda caminhos sensíveis

As preferências do projeto ficam em `docs/crew-chief/preferences.toml`. Adicione caminhos que um executor não deve ver, acima da primeira tabela `[[models]]` ou `[[tools]]` desse arquivo:

```toml
sensitive_paths = ["secret.txt", ".env", "secrets/*"]
```

Um prefixo também cobre nomes relacionados, como `.env.example`. Correspondências rastreadas ficam escondidas da worktree de execução. O ACC verifica novamente depois da execução.

Crie um arquivo rastreado sem conteúdo sensível para este teste e mencione-o no brief. `FREE_EXECUTOR` é o id exato do pick novo feito no fim do passo 5, que já rodou depois da edição de política de modelos; se você mudou alguma regra `[[models]]` desde então, rode aquele pick de novo e use o id novo. Os resultados de `--strong` e `--exclude` do passo 3 pertencem ao `DEMO_JOB` e não são candidatos a dispatch. Confira no `discover` que o id tem `cost=free` e o formato `opencode:opencode/*-free` ou `opencode:opencode-go/*-free`, então copie-o exatamente:

```bash
printf '%s\n' 'Harmless tutorial fixture; no real secret.' > secret.txt
git add secret.txt
git commit -m "Add harmless sensitive-path fixture"
printf '%s\n' 'Read secret.txt and implement the requested shout(text) helper.' > .crew-chief-sensitive.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-sensitive.md --timeout 300
```

O dispatch real do laboratório produziu:

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

Alguns apps escrevem configuração ou cache no home enquanto rodam. O ACC já ignora as pastas conhecidas dos CLIs de agentes e as que o sistema atualiza sozinho. Adicione outra entrada quando necessário, acima da primeira tabela `[[models]]` ou `[[tools]]` das preferências do projeto:

```toml
ignore_home_entries = [".someapp"]
```

Isso afeta somente a checagem de escrita externa. Não cria um sandbox.

Agora o dispatch real. Escreva o brief, escolha um executor OpenCode grátis novo, copie o id exato que ele imprime e despache:

```bash
printf '%s\n' 'Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run python -m pytest -q.' > .crew-chief-brief.md
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Use o id exato do último `pick` e confira no `discover` que ele é um modelo OpenCode grátis no formato `opencode:opencode/*-free` ou `opencode:opencode-go/*-free`. Se o dispatch terminar como `timeout`, leia `docs/crew-chief/jobs/JOB.json` para ver a tentativa, depois escolha de novo com `--exclude` para aquele executor e despache o próximo modelo OpenCode grátis.

Nunca aceite uma tentativa que terminou como `outside_write` ou `destructive_suspect`: inspecione primeiro a worktree que o dispatch deixou. Sair sem erro do executor não basta para aceitar a tentativa.

O histórico do laboratório mostra por que você inspeciona o resultado. Um dispatch grátis mais antigo foi rejeitado como `outside_write`, com `outside_writes: home: Recent`. Essa entrada é ignorada pelo ACC agora, e `.docker` (escrito pelo Docker Desktop por conta própria) também é; as outras escritas ainda são reportadas.

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

Este é só um exemplo de configuração. Se você não tem esse CLI no `PATH`, não instale um proxy nem digite credenciais por causa deste tutorial. A descoberta vai pular o placeholder, com uma linha como `skipped: my-claude-proxy: bin 'my-claude-proxy' not found on PATH`, e isso é esperado.

Depois de adicionar a entrada opcional, confira a descoberta de novo:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS="$LAB_ROOT/global-prefs.toml"
$CC discover --project .
```

## O que lembrar

- Rode `setup --yes` primeiro, usando sempre o mesmo caminho global do laboratório.
- Leia `discover` antes de escolher. Ele mostra custo e flag de treinamento de cada executor.
- A escada é grátis, depois included, depois cheap dentro do orçamento; só o `paid` de trabalho braçal exige `--strong`, enquanto o trabalho de pensar pode chegar a `paid` sozinho.
- Use executores OpenCode grátis nos dispatches reais deste tutorial; um pick não-grátis é demonstração.
- Use `--strong` somente quando quiser permitir escalada paga de propósito.
- Mantenha os orçamentos cheap explícitos e confira `spend.jsonl` ao lado do arquivo global.
- Trate `brief_refused`, `outside_write` e `destructive_suspect` como motivos para parar e inspecionar.

## Relatório do tutorial

- **Arquivos escritos:** este tutorial e `03-cost-and-safety.pt-BR.md`.
- **Comandos executados:** a rodada de laboratório registrada copiou e inicializou o exemplo, fez o primeiro commit, instalou o ACC, rodou `setup --yes`, rodou `discover`, criou um job e uma peça, rodou `pick`, fez dispatches OpenCode grátis e testou um modelo proibido e um brief com caminho sensível. Esta revisão não foi rodada de novo ao vivo. As saídas apresentadas como de laboratório ou registradas vêm daquela rodada; o `pick` pós-política do passo 5 e o formato do `--strong` no passo 3 são exemplos ilustrativos do formato e da linha de degrau, não saída registrada palavra por palavra.
- **Comportamentos a notar:** um brief que cita caminho sensível é corretamente rejeitado como `brief_refused`, antes de qualquer executor começar; um dispatch grátis também pode terminar como `outside_write`, e a tentativa registrada precisa ser lida antes de aceitar o resultado. O setup exige terminal no modo interativo, então use `--yes` em automação sem terminal, e o `--yes` não proíbe executores `included` que as próprias credenciais da máquina comprovam.

## PowerShell no Windows

Todos os blocos de comando acima são Bash. No PowerShell, defina a raiz do laboratório e o caminho das preferências antes de **cada** comando do ACC, e chame o CLI instalado com `python -I` — o PowerShell não expande uma string `$CC` em comando.

```powershell
$LabRoot = Join-Path $HOME 'projects/hello-crew-lab'   # your designated lab root
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
```

Setup e descoberta:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
```

O job normal (escreva primeiro o arquivo de pedido, depois crie o job e a peça). O `job new` imprime o id: use-o como `JOB`; o `piece add` imprime `1`:

```powershell
$Request = "Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-request.md'), "$Request`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

O job de demonstração:

```powershell
$DemoRequest = "Demonstration only: compare the cost tiers of pick. Do not dispatch this piece."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-demo-request.md'), "$DemoRequest`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py job new --project . --title "Cost ladder demo" --request-file .crew-chief-demo-request.md
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py piece add --project . --job DEMO_JOB --title "Show the cost ladder" --type code --risk low
```

`[IO.File]::WriteAllText` com `UTF8Encoding($false)` grava UTF-8 sem byte-order mark. `Set-Content -Encoding UTF8` acrescenta BOM no Windows PowerShell 5.1, e um BOM na primeira linha do brief é ruído que você não precisa.

Os picks:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job DEMO_JOB --piece 1 --strong
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job DEMO_JOB --piece 1 --exclude FREE_ID_1 --exclude FREE_ID_2
```

Depois das edições de política de modelos do passo 5, rode o discover de novo e o pick normal novo. Confira no `discover` que o id exato tem `cost=free` e o formato `opencode:opencode/*-free` ou `opencode:opencode-go/*-free`, então copie-o como `FREE_EXECUTOR`:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
```

A fixture de caminho sensível e o dispatch:

```powershell
$Fixture = "Harmless tutorial fixture; no real secret."
[IO.File]::WriteAllText((Join-Path (Get-Location) 'secret.txt'), "$Fixture`n", (New-Object System.Text.UTF8Encoding($false)))
git add secret.txt
git commit -m "Add harmless sensitive-path fixture"
$Sensitive = "Read secret.txt and implement the requested shout(text) helper."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-sensitive.md'), "$Sensitive`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-sensitive.md --timeout 300
```

O dispatch real do passo 7:

```powershell
$Brief = "Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run python -m pytest -q."
[IO.File]::WriteAllText((Join-Path (Get-Location) '.crew-chief-brief.md'), "$Brief`n", (New-Object System.Text.UTF8Encoding($false)))
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
# Copy the exact free OpenCode executor id printed above as FREE_EXECUTOR.
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py dispatch --project . --job JOB --piece 1 --executor FREE_EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Depois de adicionar a entrada de CLI opcional do passo 8, confira a descoberta de novo:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $LabRoot 'global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project .
```

A variável vive só na sessão atual. Defina `$LabRoot` e o caminho das preferências de novo em cada janela nova, e nunca aponte o caminho das preferências para o seu arquivo normal do ACC.
