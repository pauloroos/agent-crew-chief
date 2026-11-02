# Tutorial: sua primeira execução do ACC

[English](tutorial.md) · **Português**

Este passo a passo roda o ACC v0.9.0 de ponta a ponta no `examples/hello-crew`,
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
git clone --branch v0.9.0 https://github.com/pauloroos/agent-crew-chief ~/acc
mkdir -p ~/scratch && cp -R ~/acc/examples/hello-crew ~/scratch/hello-crew
cd ~/scratch/hello-crew
git init -b main
git add . && git commit -m "Start hello-crew"
python -m pytest -q
```

```powershell
git clone --branch v0.9.0 https://github.com/pauloroos/agent-crew-chief $HOME\acc
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
git add . && git commit -m "Install ACC 0.9.0"
alias ACC='python -I .agents/skills/crew-chief/scripts/crew-chief.py'
ACC score --kind code
```

```powershell
python $HOME\acc\install.py .
git add .; git commit -m "Install ACC 0.9.0"
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

## 3. Abra o coordenador

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

## 5. A entrevista, a spec e a única parada

O pedido deixa dúvidas reais em aberto, então o coordenador pergunta antes,
numa mensagem só. **Exemplo** (a redação no seu vai mudar):

```text
Antes de escrever a spec, duas perguntas:
1. greet(name, lang="xx") com um idioma desconhecido: levanta ValueError ou cai para o inglês?
2. O --shout vale dos dois lados do nome ("--shout crew" e "crew --shout")?
```

Responda, por exemplo: `1. Levanta ValueError. 2. Dos dois lados.` Ou responda
`decide por mim`: aí o coordenador escreve as suposições dele no `spec.md`.

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

Aí vem a **única parada** da execução. **Exemplo:**

```text
Spec: specs/001-greet-lang-and-shout/ (commit em acc/greet-lang-and-shout)
Trilhas, em paralelo:
  greet  T001  code  opencode:<modelo>  free      verificação: pytest tests/test_greet.py
  cli    T002  code  codex:<modelo>     included  verificação: pytest tests/test_cli.py
Aprova, e eu rodo as duas trilhas até o fim.
```

Responda `aprovado`. Daqui em diante o coordenador não para até o relatório.

## 6. Acompanhe as trilhas

**Modo Orca:** o painel do Orca mostra dois workers, um por worktree de
trilha. De outro terminal:

```text
orca worktree ps
```

**Modo simples:** worktrees e logs ficam ao lado do projeto, nunca dentro.
Os nomes das trilhas são escolha do coordenador; aqui são `greet` e `cli`.

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

O `ls` mostra uma spec de job `.md`, um `.log` e um `.pid` por trilha. O
`claude -p` só escreve o log no fim, então log vazio é normal enquanto ele
trabalha; commits novos e arquivos alterados é que mostram o progresso.

## 7. Revisão e notas

Quando um worker termina, o coordenador lê os commits dele, roda ele mesmo a
verificação e registra a nota. Você vai ver linhas assim:

```text
recorded opencode:<modelo> code pass 88
```

Veja o placar e a próxima escolha:

```text
ACC score --kind code
ACC pick --kind code
```

Depois da primeira execução cada modelo tem `n` 1, então o `pick` acrescenta
`- not proven yet, try another too`: um modelo precisa de score 70 ou mais em
2 jobs para ficar "provado".

Opcional: declare os modelos que você tem em `~/.crew-chief/models.toml` (veja
`examples/models.toml`) e rode `ACC models`. Daí em diante o `pick` escolhe só
entre eles.

## 8. Entrega

O coordenador faz o merge das duas trilhas em `acc/<slug>` com `--no-ff`, roda
`python -m pytest`, remove os worktrees e as branches das trilhas, e então:

- **com o remoto de rascunho:** envia `acc/<slug>` e abre um pull request.
  `gh pr view --web` abre o PR. O merge ou o fechamento é seu.
- **sem remoto:** para na branch local `acc/<slug>` com o merge feito.

O relatório final lista cada job com o modelo, o resultado e a nota.

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
pasta): o coordenador deixa a pasta e avisa. Quando esse processo terminar (ou
depois de reiniciar), apague essa pasta e rode `git worktree prune` e
`git branch -D acc/<slug>-<lane>`.

## 10. Um pedido pequeno, sem spec

Volte para a `main` e abra uma sessão nova do coordenador:

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
spec e sem parada para aprovar. O coordenador conta o plano em poucas linhas e
roda uma trilha. Ponha em `SLUG2` o nome da branch nova, e então:

| Comando | Saída esperada |
| --- | --- |
| `git log --oneline --first-parent "main..acc/$SLUG2"` | 1 linha: o merge da trilha; nenhum commit de spec |
| `git diff --stat main "acc/$SLUG2"` | só `greet.py` e `test_greet.py`; nada em `specs/` |
| `ACC score --kind code` | o modelo usado com `n` um a mais e `last` de hoje |

Para forçar esse caminho num pedido maior, diga "sem spec" (ou "run directly").

## Limpeza

Apague `~/scratch/hello-crew` e, se você criou, o repositório
`hello-crew-scratch` no GitHub. As notas ficam em `~/.crew-chief/ledger.jsonl`;
é para isso que elas servem.
