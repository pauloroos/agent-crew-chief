# 04-central · Coordenadoras em worktrees próprias

Este tutorial instala o comando `acc`, lança uma coordenadora numa worktree
própria, acompanha pela tela central, conversa com ela, pausa e para, recupera
depois de uma falha e integra o branch por sua conta. Ele parte dos tutoriais
01–03 e usa o projeto pequeno
[`examples/hello-crew/`](../../examples/hello-crew/).

Tudo funciona no Windows, macOS e Linux só com Python 3.11+, git e uma CLI
coordenadora suportada (`claude` ou `codex`). Não precisa de Orca, tmux, pty,
curses nem ConPTY: a tela e o `attach` usam o terminal que você já tem.

## 1. Instale o comando `acc`

Clone a release uma vez e instale só o atalho de usuário:

```bash
git clone --branch v0.4.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
python -I ~/tools/agent-crew-chief/install.py --command
```

O instalador grava `acc` (ou `acc.cmd` no Windows) na sua pasta de binários
de usuário (`~/.local/bin`, ou `%LOCALAPPDATA%\agent-crew-chief\bin` no
Windows; `--bin-dir PASTA` escolhe outra) e mostra onde gravou. Ele nunca
mexe no `PATH` nem nas suas preferências: ponha essa pasta no `PATH` você
mesmo se ela ainda não estiver lá. A entrada antiga `python -I .../crew-chief.py`
continua funcionando.

Confira:

```bash
acc --version
acc --help
```

## 2. Prepare o projeto

A coordenadora roda numa worktree nova criada a partir do `HEAD` do projeto,
então o projeto precisa ter o ACC instalado **e commitado** na mesma versão do
comando `acc`:

```bash
python -I ~/tools/agent-crew-chief/install.py .
git add -A
git commit -m "chore: install agent-crew-chief"
```

Se pular isso, o `launch` para com "no worktree ACC is installed"; passe
`--install-worktree-acc` para ele instalar o ACC só dentro da worktree nova.

## 3. Lance uma coordenadora

Escreva o pedido num arquivo e lance. Todo lançamento diz quanto está
autorizado:

```bash
acc launch --repo . --request-file request.md --mode raw-request --authorize-raw
```

- `raw-request` + `--authorize-raw`: a coordenadora pode planejar a partir do
  pedido cru.
- `direct-spec` + `--spec-dir specs/001-feature`: trabalha a partir de uma
  spec existente.
- `approved-spec` + `--spec-dir specs/001-feature --approved`: uma spec que
  você já aprovou.

```bash
acc launch --repo . --request-file request.md --mode approved-spec --spec-dir specs/001-feature --approved
```

Opções úteis: `--cli claude|codex` escolhe a CLI coordenadora (padrão: sua
preferência `coordinator_default`, senão a CLI em que você está),
`--restricted` impede a coordenadora de pular os pedidos de permissão,
`--base REV` começa o branch em outro ponto que não o `HEAD`.

O `launch` imprime `launched coordinator <ID>`. A coordenadora ganha branch e
worktree próprios; o seu checkout nunca é tocado, mesclado nem enviado. Um
supervisor em segundo plano roda ela em rodadas limitadas.

O registro central fica em `~/.config/agent-crew-chief/central/` e todo
comando `acc` encontra ele de qualquer pasta. `--central-dir PASTA` (ou a
variável `AGENT_CREW_CHIEF_CENTRAL_DIR`) aponta para outro.

## 4. Acompanhe (monitor)

Abra a tela central num segundo terminal:

```bash
acc central
```

Cada linha mostra repositório, CLI, estado, peças aceitas/total e nota, com
as peças ativas embaixo e as perguntas pendentes em destaque. Teclas: setas
selecionam, `Enter` entra (attach), `m` responde, `p` pausa/retoma, `d`
mostra detalhes, `Esc` fecha os detalhes, `q` sai (sair nunca para uma
coordenadora).

Para um script ou um terminal redirecionado, imprima uma foto só:

```bash
acc central --once
```

## 5. Mande mensagens e responda perguntas

Mensagens são arquivos duráveis: esperam na caixa de entrada da coordenadora
e são lidas entre rodadas, com IDs e confirmações para nada se perder nem ser
lido duas vezes.

```bash
acc message COORDINATOR_ID --text "Mantenha a API pública igual."
acc message COORDINATOR_ID --text "Use SQLite." --reply-to QUESTION_ID
```

`--reply-to` responde a uma pergunta da coordenadora; só o trabalho que
depende dessa pergunta fica esperando.

## 6. Entre (attach), pause e retome

Assuma a sessão da coordenadora no seu terminal:

```bash
acc attach COORDINATOR_ID
```

O supervisor cede depois da rodada atual. Se a CLI não consegue retomar
sessão ou o seu terminal está redirecionado, o `attach` avisa e mantém a
sessão em segundo plano; ele nunca abre uma sessão substituta.

```bash
acc pause COORDINATOR_ID
acc resume COORDINATOR_ID
```

`pause` deixa a rodada atual terminar e então segura; `resume` libera a
coordenadora de novo.

## 7. Pare e recupere

```bash
acc stop COORDINATOR_ID
```

A parada é cooperativa: o supervisor para depois da rodada atual. Nenhum
processo é morto por um PID guardado.

Uma coordenadora que falhou (ou que você parou) fica segura até você decidir:

```bash
acc recover COORDINATOR_ID
```

`recover` devolve uma coordenadora `failed` ou `stopped` para
`created`; nada é repetido calado. As mensagens e a worktree ficam.

## 8. Integre o trabalho você mesmo

A coordenadora usa o mesmo ciclo de job de antes (pick, brief, dispatch,
review, grade, accept, close) dentro da worktree dela e deixa o resultado no
branch dela. Revise esse branch como qualquer outro e faça o merge quando
estiver satisfeito:

```bash
git log --oneline main..BRANCH
git merge BRANCH
```

O ACC nunca faz merge nem push por você.
