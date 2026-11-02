# Tutorial: sua primeira execução do ACC

[English](tutorial.md) · **Português**

Este passo a passo roda o ACC v0.12.0 de ponta a ponta no `examples/hello-crew`,
um projeto Python minúsculo que vem com o ACC. Ele tem dois módulos
independentes, cada um com o seu teste:

```text
src/hello_crew/greet.py   greet("crew") -> "Hello, crew!"      tests/test_greet.py
src/hello_crew/cli.py     main(["crew"]) imprime a saudação     tests/test_cli.py
```

Você vai colar um pedido fixo que pede spec e se divide em duas trilhas
paralelas, uma por módulo, e depois conferir o resultado com comandos cuja
saída você já sabe de antemão. Um segundo pedido, pequeno, mostra o caminho
sem spec.

## O que você precisa

- Python 3.11+ com `pytest` (`python -m pip install pytest`) e git 2.28+.
- Um CLI coordenador: Claude Code, Codex ou OpenCode, com login feito.
- Pelo menos um CLI de worker (`claude`, `codex` ou `opencode`), com login
  feito. Pode ser o mesmo do coordenador.
- Opcional: o [Orca](https://github.com/stablyai/orca) rodando, para o modo Orca.
- Opcional: o GitHub CLI `gh`, já logado, para ver um pull request de verdade.

Os comandos são os mesmos no bash e no PowerShell, a não ser quando aparecem
dois blocos.

## 1. Crie um projeto de rascunho

Copie o exemplo para **fora** do checkout do ACC. Dentro dele, as trilhas e as
branches `acc/*` cairiam no próprio repositório do ACC.

```bash
git clone --branch v0.12.0 https://github.com/pauloroos/agent-crew-chief ~/acc
mkdir -p ~/scratch && cp -R ~/acc/examples/hello-crew ~/scratch/hello-crew
cd ~/scratch/hello-crew
git init -b main
git add . && git commit -m "Start hello-crew"
python -m pytest -q
```

```powershell
git clone --branch v0.12.0 https://github.com/pauloroos/agent-crew-chief $HOME\acc
New-Item -ItemType Directory -Force $HOME\scratch | Out-Null
Copy-Item -Recurse $HOME\acc\examples\hello-crew $HOME\scratch\hello-crew
Set-Location $HOME\scratch\hello-crew
git init -b main
git add .; git commit -m "Start hello-crew"
python -m pytest -q
```

Esperado: `3 passed`.

## 2. Instale o ACC com o kit

Instale e faça commit dos arquivos instalados, para todo worktree de trilha
ter as skills também.

```bash
python ~/acc/install.py .
git add . && git commit -m "Install ACC 0.12.0"
alias ACC='python -I .agents/skills/crew-chief/scripts/crew-chief.py'
ACC score --kind code
```

```powershell
python $HOME\acc\install.py .
git add .; git commit -m "Install ACC 0.12.0"
function ACC { python -I .agents/skills/crew-chief/scripts/crew-chief.py @args }
ACC score --kind code
```

O instalador imprime uma linha por cópia (`copied skill to .claude/skills/crew-chief`,
...) e o trecho que pôs no `AGENTS.md` e no `CLAUDE.md`. O `ACC score` imprime
`No graded jobs yet.` numa máquina que nunca rodou o ACC, ou o seu placar atual.

**Opcional, para ver um pull request de verdade:** dê ao projeto um
repositório de rascunho no GitHub agora, antes da execução (usa o login do
`gh` que você já tem).

```bash
gh repo create hello-crew-scratch --private --source . --push
```

Sem remoto, a execução para na branch local com o merge feito. Os dois jeitos
servem.

## 3. Abra o chefe de equipe

O **chefe de equipe** é o seu CLI coordenador. Ele escreve a spec, planeja as
trilhas, escolhe os modelos, faz a parada de aprovação, despacha um
**engenheiro de corrida** por trilha (um worker do Orca no modo Orca, ou uma
sessão CLI em segundo plano caso contrário), confere os relatórios deles, dá
nota, faz o merge e entrega. Engenheiros rodam **pilotos** (workers) e
**fiscais**; o chefe de equipe nunca roda pilotos ele mesmo, exceto num pedido
pequeno sem spec.

- **Modo Orca:** adicione `~/scratch/hello-crew` ao Orca e abra um terminal do
  projeto no Orca. Ali, `orca status --json` deve mostrar o Orca rodando.
- **Modo simples:** qualquer terminal (ou o VS Code), em `~/scratch/hello-crew`.
  Se o Orca estiver rodando e você quiser o modo simples mesmo assim, ponha
  "Sem Orca." no pedido.

Abra o seu CLI ali: `claude`, `codex` ou `opencode`.

## 4. Cole o pedido

Cole exatamente isto:

```text
ACC, adiciona duas funcionalidades ao hello-crew.

1. greet: greet(name, lang="en") ganha a opção lang. lang="en" continua
   "Hello, <name>!"; lang="pt" devolve "Olá, <name>!". Mude só
   src/hello_crew/greet.py e tests/test_greet.py.
2. cli: uma flag --shout imprime a saudação em maiúsculas, então
   main(["--shout", "crew"]) imprime "HELLO, CREW!". Sem a flag a saída
   continua igual. Mude só src/hello_crew/cli.py e tests/test_cli.py.

As duas mudanças são independentes: cada uma mexe só no seu módulo e no seu
arquivo de teste, então rode como duas trilhas paralelas, uma por módulo. Cada
trilha adiciona testes para a sua mudança, e python -m pytest tem que passar.
Se o repositório não tiver remoto, pare na branch local com o merge feito.
```

Você pode adicionar um **ritmo** a qualquer pedido: acrescente `ritmo eco` (só
modelos grátis), `ritmo race` (padrão) ou `ritmo push` para definir o teto de
custo e o escopo da vistoria.

## 5. A entrevista, a spec e a única parada

O pedido deixa dúvidas reais em aberto, então o chefe de equipe pergunta antes,
numa mensagem só. **Exemplo** (a redação no seu vai mudar):

```text
Antes de escrever a spec, duas perguntas:
1. greet(name, lang="xx") com um idioma desconhecido: levanta ValueError ou cai para o inglês?
2. O --shout vale dos dois lados do nome ("--shout crew" e "crew --shout")?
```

Responda, por exemplo: `1. Levanta ValueError. 2. Dos dois lados.` Ou responda
`decide por mim`: aí o chefe de equipe escreve as suposições dele no `spec.md`.

Depois ele escreve a spec numa branch nova `acc/<slug>` e faz commit dela.
**Exemplo** de `specs/001-greet-lang-and-shout/tasks.md`:

```markdown
# Tasks: greet lang e cli --shout

## Phase 3: User Story 1 (P1)
- [ ] T001 [P] [US1] Adicionar lang ("en", "pt"; desconhecido levanta ValueError) ao greet() em src/hello_crew/greet.py, testes em tests/test_greet.py
- [ ] T002 [P] [US2] Adicionar --shout (saída em maiúsculas) ao main() em src/hello_crew/cli.py, testes em tests/test_cli.py
```

O `spec.md` fica ao lado (o que o usuário quer, histórias, requisitos,
"pronto quando"). O `plan.md` costuma não aparecer aqui: o desenho é óbvio.

Aí vem a **única parada** da execução. Cada job agora mostra o seu **risco** e
o **ritmo** junto com as trilhas e os modelos. **Exemplo:**

```text
Spec: specs/001-greet-lang-and-shout/ (commit em acc/greet-lang-and-shout)
Ritmo: race
Trilhas, em paralelo:
  greet  T001  code  normal  opencode:<modelo>  free      verificação: pytest tests/test_greet.py
  cli    T002  code  normal  codex:<modelo>     included  verificação: pytest tests/test_cli.py
Aprova, e eu rodo as duas trilhas até o fim.
```

Responda `aprovado`. Daqui em diante o chefe de equipe não para até o relatório.

## 6. Acompanhe as trilhas

O chefe de equipe despacha um engenheiro de corrida por trilha. Cada engenheiro
inicia pilotos no worktree da trilha e coordena Stint / Relay / Pneus novos /
Vistoria naquela trilha. Todo agente roda num terminal visível com o nome
padrão `acc-<slug>-<trilha>-<papel>` (ou `-<n>` nas repetições).

**Modo Orca:** cada agente tem uma aba do Orca na worktree da trilha. Liste:

```text
orca terminal list --json
```

Os relatórios das trilhas também chegam no chat conforme os engenheiros terminam.

**Modo segundo plano** (sem Orca): worktrees e logs ficam ao lado do projeto,
nunca dentro. Os nomes das trilhas são escolha do chefe de equipe; aqui são
`greet` e `cli`.

```bash
ls ../hello-crew-acc-logs
tail -n 20 ../hello-crew-acc-logs/*.log
git -C ../hello-crew-acc-greet log --oneline -3
git -C ../hello-crew-acc-cli status --porcelain
```

```powershell
Get-ChildItem ..\hello-crew-acc-logs
Get-Content ..\hello-crew-acc-logs\*.log -Tail 20
git -C ..\hello-crew-acc-greet log --oneline -3
git -C ..\hello-crew-acc-cli status --porcelain
```

O `ls` mostra, por agente: o brief ou spec do job (`<nome>.md`), `.log`,
`.log.err` e `.pid`, onde `<nome>` é o nome padrão
`acc-<slug>-<trilha>-<job>-<papel>[-<n>]`. Para jobs de risco alto, e para todo
job exceto baixo risco no ritmo `push`, você também verá prompts e achados dos
fiscais. O `claude -p` só escreve o log no fim, então log vazio é normal
enquanto ele trabalha; commits novos e arquivos alterados é que mostram o
progresso em todos os modos.

## 7. Revisão e notas

Quando um piloto termina, o engenheiro de corrida lê os commits dele, roda ele
mesmo a verificação e registra a nota. Você vai ver linhas assim:

```text
recorded opencode:<modelo> code pass 88
```

O chefe de equipe então lê o relatório de cada trilha, roda ele mesmo as
verificações da trilha, lê os achados dos fiscais dos jobs de risco alto e dá
nota ao engenheiro como `coordinate`:

```text
recorded opencode:<modelo> coordinate pass 90
```

Veja o placar e a próxima escolha:

```text
ACC score --kind code
ACC score --kind coordinate
ACC pick --kind code
```

Depois da primeira execução cada modelo tem `n` 1, então o `pick` acrescenta
`- not proven yet, try another too`: um modelo precisa de score 70 ou mais em
2 jobs para ficar "provado". Quando o `~/.crew-chief/models.toml` lista
modelos ainda não usados, o `score` acrescenta uma linha recolhida:

```text
not tried yet (N): opencode:provider/model-a, codex:model-b, ...
```

Opcional: declare os modelos que você tem em `~/.crew-chief/models.toml` (veja
`examples/models.toml`) e rode `ACC models`. Daí em diante o `pick` escolhe só
entre eles.

## 8. Entrega

O chefe de equipe faz o merge das duas trilhas em `acc/<slug>` com `--no-ff`,
roda `python -m pytest`, remove os worktrees e as branches das trilhas, e então:

- **com o remoto de rascunho:** envia `acc/<slug>` e abre um pull request.
  `gh pr view --web` abre o PR. O merge ou o fechamento é seu.
- **sem remoto:** para na branch local `acc/<slug>` com o merge feito.

O relatório final lista cada job com risco, ritmo, modelo, tentativas, notas
dos fiscais, resultado, notas do piloto e do engenheiro, e qualquer pendência.

## 9. Funcionou?

Ponha o slug com o nome da branch do relatório (aqui, o do exemplo):

```bash
SLUG=greet-lang-and-shout
```

```powershell
$SLUG = "greet-lang-and-shout"
```

| Comando | Saída esperada |
| --- | --- |
| `git log --oneline --first-parent "main..acc/$SLUG"` | 3 linhas: duas `Merge branch 'acc/<slug>-…'` e, embaixo, o commit da spec. Sem `--first-parent` aparecem também os commits dos workers. |
| `git ls-tree -r --name-only "acc/$SLUG" specs` | `specs/001-<slug>/spec.md` e `specs/001-<slug>/tasks.md` |
| `git diff --stat main "acc/$SLUG"` | os dois arquivos da spec, `greet.py`, `cli.py`, `test_greet.py`, `test_cli.py`; mais nada |
| `git branch --list "acc/*"` | uma linha: `acc/<slug>` (com `*` se for a branch atual); nenhuma branch de trilha, a não ser uma cuja pasta o relatório diz estar presa por outro programa |
| `git worktree list` | uma linha: a pasta do projeto; uma pasta de trilha presa aparece como `prunable` até você removê-la (veja abaixo) |
| `ACC score --kind code` | os modelos usados, `last` = data de hoje (UTC), `n` com um a mais por job |
| `git ls-remote origin "refs/heads/acc/*"` | uma linha terminando em `refs/heads/acc/<slug>`; nenhuma branch de trilha (workers nunca fazem push). Pule se não tiver remoto. |
| `python -m pytest -q` (em `acc/<slug>`) | `N passed`, N ≥ 5, nenhuma falha |
| `ls .claude/skills` | `crew-chief`, `ponytail`, `ponytail-review`, `systematic-debugging`, `test-driven-development`, `verification-before-completion` |
| `ls .claude/skills/*/.installed-by-agent-crew-chief` | 5 caminhos, um por pasta do kit (a `crew-chief` é a skill do próprio ACC e não tem) |

No PowerShell, os dois últimos ficam:

```powershell
(Get-ChildItem .claude\skills).Name
(Get-ChildItem .claude\skills\*\.installed-by-agent-crew-chief -Force).Directory.Name
```

No Windows, uma pasta "aberta em outro programa" pode travar a limpeza da trilha
(um processo que sobrou do worker, muitas vezes um servidor MCP, segura a
pasta): o chefe de equipe deixa a pasta e avisa. Quando esse processo terminar
(ou depois de reiniciar), apague essa pasta e rode `git worktree prune` e
`git branch -D acc/<slug>-<lane>`.

## 10. Um pedido pequeno, sem spec

Volte para a `main` e abra uma sessão nova do chefe de equipe:

```text
git switch main
```

Cole:

```text
ACC, faz o greet() tirar os espaços em volta do nome, para greet("  crew ")
devolver "Hello, crew!". Mude só src/hello_crew/greet.py e tests/test_greet.py.
Se o repositório não tiver remoto, pare na branch local com o merge feito.
```

Um job, uma área de arquivos, nenhuma dúvida: sem entrevista, sem arquivos de
spec e sem parada para aprovar. O chefe de equipe conta o plano em poucas
linhas e pode ele mesmo tocar a engenharia da sua única trilha. Ponha em
`SLUG2` o nome da branch nova, e então:

| Comando | Saída esperada |
| --- | --- |
| `git log --oneline --first-parent "main..acc/$SLUG2"` | 1 linha: o merge da trilha; nenhum commit de spec |
| `git diff --stat main "acc/$SLUG2"` | só `greet.py` e `test_greet.py`; nada em `specs/` |
| `ACC score --kind code` | o modelo usado com `n` um a mais e `last` de hoje |

Para forçar esse caminho num pedido maior, diga "sem spec" (ou "run directly").

## 11. Um job de risco: fiscais e pit stop

Jobs de risco alto ganham revisão independente antes de serem aceitos. Este
exemplo usa nomes genéricos de modelos; os seus vão ser diferentes.

Pedido:

```text
ACC, faz o greet() rejeitar nomes ruins: vazios, com mais de 64 caracteres ou
com caracteres de controle; risk high.
```

O que acontece:

1. O engenheiro de corrida roda um piloto. Passa: 14 testes, nota 90.
2. Dois fiscais de outras famílias de modelos revisam o diff em modo somente
   leitura. Ambos escrevem achados no formato definido pelo
   `templates/scrutineer.md`:

   ```text
   severity | file:line | failing scenario
   ```

   Severidade é `serious`, `medium` ou `minor`. A última linha de cada arquivo
   é `VERDICT: pass` ou `VERDICT: fail`. Ambos escrevem `VERDICT: pass`.
3. O chefe de equipe faz sua própria passada no caminho de risco e encontra uma
   brecha que os fiscais deixaram passar: `greet("\u202eabc")` (um override bidi
   Unicode que pode falsificar a saída do terminal) é aceito.
4. O achado confirmado volta para o **mesmo modelo do piloto** numa sessão nova
   — um **pit stop**. Ele corrige em cima do próprio trabalho (sem reset) e
   adiciona 13 casos de teste novos rejeitando caracteres de formatação Unicode.
   Novo resultado: 27 testes, nota 92.
5. Os dois fiscais perdem pontos pela falha (`ACC record --kind review`).

Arquivos deixados em `../hello-crew-acc-logs/` para este job (modo segundo plano):

```text
<nome>.md                            # spec do job
<nome>.log                           # stdout do piloto
<nome>.log.err                       # stderr do piloto
<nome>.pid                           # PID do piloto
<nome>-scrutineer-1-prompt.md
<nome>-scrutineer-1.md
<nome>-scrutineer-2-prompt.md
<nome>-scrutineer-2.md
```

onde `<nome>` é o nome padrão `acc-<slug>-<trilha>-<job>-<papel>[-<n>]`.

Duas regras relacionadas do método Pit Wall:

- **Pneus novos**: antes de cada nova tentativa de um job, o worktree da trilha
  é resetado para o commit que tinha antes do job começar, para o próximo piloto
  nunca herdar edições ou arquivos não rastreados de uma tentativa que falhou.
- **Limite de pit stops**: um job tem no máximo 2 pit stops; cada um recebe sua
  própria nota com "(pit stop <n>)" no título do job. Depois do limite o job
  passa ou vai para Relay.
- **Relay**: quando um stint falha, a tentativa 2 usa outro modelo com pneus
  novos; a tentativa 3 sobe um degrau de custo, ainda com pneus novos; depois de
  três tentativas falhadas o engenheiro conserta o job ele mesmo ou para a trilha.

## 12. Duas máquinas (opcional)

Compartilhe o ledger entre máquinas por um repositório git privado. Em cada
máquina, uma vez só:

```text
ACC sync --init git@github.com:<voce>/crew-chief-ledger.git
```

Depois, no início de um pedido e depois da última nota:

```text
ACC sync
```

Para fundir um arquivo de ledger copiado de outra máquina à mão:

```text
ACC merge <arquivo>
```

Só o ledger é compartilhado; specs, branches e worktrees ficam locais.

## Limpeza

Apague `~/scratch/hello-crew` e, se você criou, o repositório
`hello-crew-scratch` no GitHub. As notas ficam em `~/.crew-chief/ledger.jsonl`;
é para isso que elas servem.
