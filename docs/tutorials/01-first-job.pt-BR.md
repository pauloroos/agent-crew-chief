# Seu primeiro job em 10 minutos

Este tutorial usa um projeto Python pequeno e um job real do ACC. Você vai pedir a um executor grátis para adicionar `shout(text)`, fazer outro modelo grátis revisar o resultado e aceitar a entrega.

## Kit base

Este tutorial pressupõe que o kit base está instalado (Ponytail para prompts concisos e skills superpowers como test-driven-development, systematic-debugging e verification-before-completion). O instalador copia esses componentes por padrão; use `--no-ponytail` ou `--no-superpowers` para pular algum deles. Consulte o README para detalhes sobre os padrões e o comportamento de cada componente.

## 1. Copie o projeto de prática

No repositório do ACC, copie o exemplo para uma pasta nova e entre nela:

```bash
cp -R examples/hello-crew ~/projects/hello-crew
cd ~/projects/hello-crew
git init
git add .
git commit -m "Start hello-crew practice project"
```

Você deve ver um repositório Git novo e um primeiro commit. Se o Git pedir seu nome e e-mail, configure-os para este repositório de prática e repita o commit. O ACC precisa de um repositório Git próprio porque usa worktrees isoladas. No Windows, use a seção [PowerShell no Windows](#powershell-no-windows) no lugar dos blocos Bash.

## 2. Instale o ACC

Clone uma versão marcada do ACC em algum lugar fora do projeto de prática. Depois instale o ACC na cópia:

```bash
git clone --branch v0.6.1 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py .
```

Você deve ver a skill copiada para `.claude/skills/crew-chief` e `.agents/skills/crew-chief`, além dos arquivos de projeto criados pelo instalador. Se o clone falhar, baixe o arquivo da release e rode o `install.py` dele.

Nos comandos abaixo, usaremos esta forma curta para o CLI:

```bash
CC="python -I .agents/skills/crew-chief/scripts/crew-chief.py"
```

## 3. Configure os degraus de custo

Escolha um arquivo de preferências dentro do projeto de prática. Assim, o tutorial fica separado das configurações normais do ACC. Antes de **cada** comando do ACC, exporte esta variável:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC setup --yes --project .
```

Você deve ver um resumo como `your tiers: free 23 · included 3 · cheap 0 (budget US$ 0) · paid 66`, de uma máquina de teste. Esses números são um exemplo, não uma promessa: as contagens mudam conforme os provedores adicionam ou removem modelos e são diferentes em cada máquina, porque dependem dos CLIs instalados nela. O que importa é que a contagem de grátis seja maior que zero. `--yes` aceita os padrões seguros: nenhuma assinatura, orçamento zero para modelos baratos e nenhuma permissão de treinamento.

Se não houver executor OpenCode grátis, pare aqui e configure um antes de enviar o job. Não use executor incluído, barato ou pago neste tutorial.

## 4. Descubra os executores

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC discover --project .
```

Você deve ver entradas OpenCode grátis, como `opencode:opencode-go/beam-free`, seguidas das ferramentas ignoradas e dos tipos de trabalho. A lista exata muda com o tempo. Se aparecer `run setup to configure cost tiers`, use o mesmo caminho de preferências no `setup`; um nome diferente é ignorado.

## 5. Crie um job

Escreva o pedido exatamente como você quer que ele seja registrado:

```bash
printf '%s\n' 'Add a function shout(text) to src/hello_crew/__init__.py. Return the upper-case text followed by an exclamation mark, and add a focused test.' > .crew-chief-request.md
```

Registre o pedido:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC job new --project . --title "Add shout helper" --request-file .crew-chief-request.md
```

Você deve ver um id como `20261020-1437-add-shout-helper`. O seu id será diferente. Salve-o como `JOB` no shell ou substitua `JOB` pelo id mostrado nos comandos abaixo.

## 6. Divida o trabalho

Este pedido é pequeno o bastante para uma peça:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC piece add --project . --job JOB --title "Implement shout helper" --type code --risk low
```

Você deve ver `1`. Esse é o número da peça. Se o pedido crescer, adicione uma peça para cada mudança que possa ser revisada separadamente.

## 7. Escolha um executor grátis

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1
```

Esta escolha de execução do OpenCode imprime duas linhas:

```text
opencode:opencode-go/beam-free
unrated, no history (opencode:opencode-go/beam-free); tier free
```

A primeira linha é o executor. A segunda explica a escolha e confirma o degrau. Use o id exato da primeira linha. Uma terceira linha, `effort: low`, é impressa apenas quando a escolha tem um valor de esforço: o ACC imprime a linha de esforço quando o executor escolhido tem um. Por padrão as ferramentas do OpenCode não têm parâmetro de esforço, então uma escolha do OpenCode mostra só as duas linhas acima. Uma ferramenta configurada com parâmetro de esforço, como Codex ou Antigravity, pode mostrar essa terceira linha (o passo 10 mostra uma). Se o `pick` escolher outro modelo grátis que não seja OpenCode para execução, passe `--exclude` para ele e escolha de novo. Nunca envie um executor incluído, barato ou pago neste tutorial.

## 8. Escreva o brief

O brief é o contrato completo do executor:

```bash
printf '%s\n' 'Implement the requested shout(text) helper in src/hello_crew/__init__.py. Add a focused test in tests/test_basic.py. Keep the change small and run the existing test command.' > .crew-chief-brief.md
```

Mencione os arquivos permitidos, o comportamento esperado e o comando de teste. Não coloque segredos nem caminhos pessoais no brief.

## 9. Envie o trabalho

Substitua `EXECUTOR` pelo valor da primeira linha do `pick`:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor EXECUTOR --brief-file .crew-chief-brief.md --timeout 300
```

Você deve ver `status: ok`, uma `branch`, uma `worktree` e linhas de duração e tokens. Se aparecer `status: timeout`, leia a tentativa registrada antes de tentar de novo. Escolha outro modelo OpenCode grátis com `--exclude` e envie novamente.

## 10. Revise com outro modelo grátis

Escolha primeiro o revisor:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review
```

A saída deve mostrar um executor grátis de outra família e explicar `reviewer outside the family of`. Uma escolha de revisão de uma ferramenta com parâmetro de esforço tem três linhas:

```text
antigravity:claude-opus-5-5-low
unrated, no history (antigravity:claude-opus-5-5-low); tier free; reviewer outside the family of: opencode:opencode-go/minimax-m3.1-flash-free
effort: low
```

A terceira linha é o nível de esforço. Ela aparece só para ferramentas que têm parâmetro de esforço, então uma escolha de revisão de um modelo do OpenCode tem apenas as duas primeiras linhas. Use o id exato da primeira linha como `REVIEWER`. Escreva o brief da revisão:

```bash
printf '%s\n' 'Review the last successful attempt for the shout(text) request. Check the diff and tests. Report only concrete missing requirements or defects; do not edit files.' > .crew-chief-review.md
```

Envie a revisão usando a `worktree` mostrada no dispatch bem-sucedido da implementação:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC dispatch --project . --job JOB --piece 1 --executor REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

Você deve ver `status: ok` e uma linha `answer:`. Se a revisão falhar por cota, escolha outro revisor grátis e use `--exclude` para o que falhou.

## 11. Dê a nota, aceite e feche

Se a revisão não encontrou correções, dê a nota assim:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC grade --project . --job JOB --piece 1 --executor EXECUTOR --corrections 0 --done 4 --missing 0 --extra 0
```

Você deve ver `score: 100`. Se o revisor encontrar um defeito real, registre-o com `correction`, envie apenas esse ajuste a partir da última tentativa bem-sucedida, revise de novo e dê nota a cada executor que trabalhou na peça.

Aceite a última tentativa bem-sucedida:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC accept --project . --job JOB --piece 1
```

Você deve ver `accepted` e o nome de uma branch. Feche o job:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC close --project . --job JOB --type feat
```

Você deve ver uma mensagem de commit, o executor e a nota, o caminho do registro e um bloco de economia. A linha de economia se parece com esta:

```text
free work: 100% of pieces, 100% of recorded tokens · coordinator: not recorded · 2.0 attempts/piece · mean score 100
```

Sem entradas de usage, o resumo mostra coordinator: not recorded: isso não é um zero medido. A porcentagem grátis cobre apenas os tokens registrados e não comprova o custo total da sessão. Use usage add para registrar o uso do coordenador; uma entrada explícita de zero tokens mantém coordinator: 0% quando há tokens de execução.

Os números exatos dependem da sua execução. Leia a linha como uma verificação de saúde: quanto trabalho ficou grátis, quanto uso do coordenador foi registrado, quantas tentativas foram necessárias e qual foi a nota média.

> **O que aconteceu?** O ACC registrou o pedido, isolou a implementação em uma worktree Git, escolheu um executor OpenCode grátis, usou um segundo modelo como revisor, registrou a nota, mesclou a branch aceita e mostrou o registro da entrega com os dados de economia. O coordenador ainda decide o que pedir e se a revisão está correta.

## Solução de problemas

### Apareceu `__pycache__` no job

O exemplo inclui um `.gitignore` com `__pycache__/` e `.pytest_cache/`. Se você copiou apenas os arquivos-fonte, copie o exemplo inteiro outra vez, incluindo `.gitignore`, e faça o commit antes de instalar o ACC.

### `run setup to configure cost tiers`

O ACC não encontrou o bloco de configuração gerenciado no arquivo de preferências. Use exatamente o mesmo caminho em todos os comandos:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC setup --yes --project .
```

Um arquivo chamado `preferences.toml` não é o mesmo que o `global-prefs.toml` deste projeto de prática. Não use o arquivo normal da sua pasta pessoal neste tutorial.

### `review requires --target` ou `review requires --brief-file`

Uma revisão precisa da worktree da tentativa bem-sucedida e de um brief de revisão. Rode `pick --review`, salve o caminho `worktree` do dispatch da implementação, crie `.crew-chief-review.md` e passe `--target WORKTREE` e `--brief-file .crew-chief-review.md`.

### `no free executor left`

O trabalho de execução deve continuar com executores grátis. Rode `discover` novamente e use `pick` com `--exclude` para os executores que falharam. Não use `--strong` neste tutorial.

### A revisão devolve `status: quota`

O revisor bateu no limite de uso dele, então a revisão nem chegou a rodar. Leia a tentativa registrada para ver qual revisor falhou e qual worktree da implementação é a boa. O registro do job guarda cada tentativa:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
cat docs/crew-chief/jobs/JOB.json
```

A lista `attempts` da peça 1 tem uma entrada por tentativa. Procure a tentativa `exec` com `"status": "ok"` e copie o valor de `worktree`, e a tentativa `review` com `"status": "quota"` e copie o `executor`. Depois escolha outro revisor grátis, deixando de fora o que falhou, e envie a revisão de novo contra a mesma worktree bem-sucedida:

```bash
export AGENT_CREW_CHIEF_GLOBAL_PREFS=~/projects/hello-crew/global-prefs.toml
$CC pick --project . --job JOB --piece 1 --review --exclude FAILED_REVIEWER
$CC dispatch --project . --job JOB --piece 1 --executor NEW_REVIEWER --brief-file .crew-chief-review.md --review --target WORKTREE --timeout 300
```

Use o id exato mostrado pelo `pick` como `NEW_REVIEWER`, e a worktree da implementação bem-sucedida como `WORKTREE`.

Alguns revisores compartilham a mesma cota: os modelos Antigravity de um fornecedor saem do mesmo pool, e um modelo por assinatura divide a cota com todos os outros modelos daquele CLI. Quando um revisor de um pool falha, outro do mesmo pool pode falhar também. Nesse caso, exclua o pool inteiro e revise com um modelo grátis do OpenCode. O ACC também pausa por um tempo um revisor que devolveu `quota`, então um `pick` novo o mostra como `skipped for quota` e segue sozinho.

### Os testes não rodam

O exemplo não tem dependências de execução, mas o comando de teste precisa do `pytest`. Instale-o no seu ambiente e rode `pytest -q`. Se o shell não encontrar o executável `pytest`, use o ambiente Python onde você o instalou.

## PowerShell no Windows

Todos os blocos de comando acima são Bash. No PowerShell, copie o exemplo com `Copy-Item -Recurse` no lugar de `cp -R`:

```powershell
Copy-Item -Recurse examples/hello-crew (Join-Path $HOME 'projects/hello-crew')
Set-Location (Join-Path $HOME 'projects/hello-crew')
git init
git add .
git commit -m "Start hello-crew practice project"
if ($LASTEXITCODE -ne 0) { throw 'Commit failed; configure repository-local identity and retry before install.py.' }
```

Se o commit falhou, defina a identidade só para este repositório e repita o commit:

```powershell
git config user.name "Your Name"
git config user.email "you@example.com"
git commit -m "Start hello-crew practice project"
```

O PowerShell não expande uma string `$CC` em um comando, então chame o CLI instalado direto com `python -I`. Defina o caminho das preferências antes de **cada** comando do ACC, com o mesmo caminho usado no resto deste tutorial:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py setup --yes --project .
```

Depois disso, todos os outros comandos do ACC seguem o mesmo formato, por exemplo:

```powershell
$env:AGENT_CREW_CHIEF_GLOBAL_PREFS = Join-Path $HOME 'projects/hello-crew/global-prefs.toml'
python -I .agents/skills/crew-chief/scripts/crew-chief.py pick --project . --job JOB --piece 1
```

A variável vale só para a sessão atual. Defina-a de novo em cada janela nova e nunca aponte para o seu arquivo normal de preferências do ACC.
