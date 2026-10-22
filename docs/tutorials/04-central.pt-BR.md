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

## 9. Inspecione no navegador (v0.4.1)

O navegador mostra os mesmos registros do terminal: cartões, perguntas,
histórico de jobs, gráficos de tokens por modelo, placar, totais e
diagnósticos.

```bash
acc central --web
acc central --web --port 8080
```

A página é só de loopback, em `http://127.0.0.1:<porta>/` (padrão: uma
porta efêmera), e é servida a partir dos assets estáticos empacotados
`skill/crew-chief/web/index.html`, `skill/crew-chief/web/central.css` e
`skill/crew-chief/web/central.js`. Não há CDN nem script ou folha de estilo
externos; texto não confiável é exibido só via `textContent`, nunca como
markup.

As ações locais (mensagem, resposta a uma pergunta, pausar, retomar, parar,
entrar pelo terminal) são autorizadas por servidor: a página primeiro busca
um token CSRF e depois o envia, junto com um id de nova tentativa, em todo
POST; o servidor exige Origin/Host estritos e não tem CORS. Os botões ficam
desativados até o token chegar. A barra de ações age sobre a coordenadora
que você selecionou; se ela sumir, a página pede que você escolha de novo.
Cada pergunta tem o seu próprio botão Reply, e Stop pede confirmação dizendo
qual coordenadora vai parar.

A entrada pelo terminal é controlada: Attach enfileira um pedido para uma
tela de terminal ativa, que faz a mesma passagem de controle do `Enter`. A
página mostra o pedido na fila (ele pode esperar enquanto outra sessão está
aberta), depois aberto enquanto você trabalha no terminal, depois encerrado
com o código de saída da sessão, ou por que falhou. Sem ponte de terminal
disponível, ou depois de uma falha, a página mostra uma instrução para
copiar, com as aspas do seu shell (PowerShell no Windows). Quando você abriu
a visão com `--central-dir`, a instrução leva a mesma pasta como caminho
absoluto: dentro da sua home ela aparece como `~/...`; fora dela aparece
inteira, porque sem ela o comando abriria outro registro, e a página só é
servida para o seu próprio navegador no loopback:

```bash
acc attach COORDINATOR_ID
```

Ela não afirma que abriu a sessão; rode esse comando no seu próprio
terminal.

As contagens de tokens avisam "includes estimates" ou "partial" quando parte
do uso foi estimada ou não foi informada. Se a página perde a conexão, ela
mantém os últimos dados, marcados como desatualizados, e tenta de novo.

Smoke reproduzível com navegador falso (só fixtures sintéticas, sem
coordenadora real, sem registro real):

```bash
python tests/test_central_web_assets.py --smoke
```

1. Ele imprime uma URL de loopback com porta efêmera e serve só snapshots
   sintéticos.
2. Abra `http://127.0.0.1:<porta>/` e confirme que cartões, perguntas,
   histórico, gráficos, placar e totais aparecem a partir de `/api/snapshot`.
3. Confirme que o texto de demonstração com cara de HTML aparece literal,
   nunca como markup.
4. Confirme que nenhum script ou folha de estilo externo é carregado; depois
   pare o servidor e confirme que o socket de escuta foi liberado.

## 10. Backend Orca opcional (v0.4.1)

O modo padrão nunca precisa do Orca. Passe `--backend orca` só quando quiser
que o Orca crie a worktree:

```bash
acc launch --repo . --request-file request.md --mode raw-request --authorize-raw --backend orca
```

O Orca faz uma coisa só: cria a worktree da coordenadora. Todo o resto
(supervisor, rodadas, mensagens, controles, attach e recuperação) é o mesmo
núcleo do modo padrão.

O `probe` roda só no lançamento: `acc launch --backend orca` verifica o
executável `orca` no `PATH` (`--version`, `--help` e as capacidades legíveis
por máquina de `agent-context --json`) antes de criar qualquer coisa, e de
novo logo antes de pedir a worktree ao Orca. Executável ausente, versão sem
suporte ou capacidade faltando fazem o lançamento falhar de forma explícita,
sem cair para o backend padrão e sem trocar de backend no meio do lançamento.

O backend fica guardado com a coordenadora e não muda. `acc message`,
`acc attach`, `acc pause`, `acc resume`, `acc stop` e `acc recover`, e as
mesmas ações no `acc central` e no navegador, seguem esse nome guardado e
nunca chamam o Orca. Se o Orca for removido ou atualizado depois, você ainda
consegue mandar mensagem, entrar, pausar, parar e recuperar uma coordenadora
Orca:

```bash
acc stop COORDINATOR_ID
```

Uma coordenadora cujo backend guardado não é `default` nem `orca` é recusada
com um erro `unknown backend`; ela nunca é tratada como outro backend.

## 11. Limites da v0.4.1

- Só loopback (`127.0.0.1`), porta efêmera por padrão; sem servidor remoto
  nem acesso compartilhado entre usuários, e nunca há um shell embutido no
  navegador.
- Quem integra é você: revise o branch da coordenadora e faça o merge como
  na seção 8. O ACC nunca faz merge nem push e nunca remove sozinho a
  worktree de uma coordenadora.
