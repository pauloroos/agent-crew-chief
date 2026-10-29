# 04-central · Coordenadoras em worktrees próprias

Este tutorial instala o comando `acc`, lança uma coordenadora numa worktree
própria, acompanha pela tela central, conversa com ela, pausa e para, recupera
depois de uma falha e integra o branch por sua conta. Ele parte dos tutoriais
01–03 e usa o projeto pequeno
[`examples/hello-crew/`](../../examples/hello-crew/).

Tudo funciona no Windows, macOS e Linux só com Python 3.11+, git e uma CLI
coordenadora suportada (`claude` ou `codex`). Não precisa de tmux, pty,
curses nem ConPTY: a tela e o `attach` usam o terminal que você já tem.

## 1. Instale o comando `acc`

Clone a release uma vez e instale só o atalho de usuário:

```bash
git clone --branch v0.5.0 https://github.com/pauloroos/agent-crew-chief ~/tools/agent-crew-chief
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

## 9. Pausa por quota

Quando a CLI da coordenadora para por quota ou limite de uso (um HTTP 429,
"usage limit", "rate limit"), a coordenadora não é marcada como falha: ela vai
para `quota` e fica parada. Worktree, registros do job e sessão continuam como
estão.

```bash
acc resume COORDINATOR_ID
```

`resume` continua dos mesmos registros quando o limite volta. Para trocar de
CLI, pare a coordenadora e lance o mesmo pedido de novo com outro `--cli`. O
maestro (tutorial 05) faz isso por todas as coordenadoras que roda.

## 10. Limites

- Só terminal: `acc central` é a tela, `acc central --json` imprime um
  snapshot para scripts e para o maestro.
- Quem integra é você: revise a branch da coordenadora e faça o merge como na
  seção 8. O ACC nunca faz merge nem push e nunca remove a worktree de uma
  coordenadora sozinho.

## 11. Checagem de atualização

O ACC pode verificar se há uma release estável mais nova em segundo plano. Fora os
webhooks que você configurar e o `install.py --with-rtk`, esta é a **única
conexão de rede externa do ACC**. A checagem roda no máximo uma vez a cada 24 horas
rolantes (incluindo falhas), nunca bloqueia seu comando e é totalmente silenciosa
quando offline, em timeout ou quando o cache está desatualizado ou inválido.
Nenhuma configuração é necessária.

**Preferência.** `check_updates` é um booleano estrito (padrão `true`; um `false`
estrito desativa). Defina `check_updates = false` no `preferences.toml` do seu
projeto ou global para desativar completamente — nenhuma checagem é agendada,
nenhum aviso em cache é exibido e as preferências nunca são reescritas. A
precedência existente de projeto-sobre-global se aplica.

Quando uma versão estável mais nova é encontrada, a visão central do
terminal exibe exatamente uma linha:

```
ACC v0.4.4 available. Update manually in your ACC source clone: git fetch --tags; git checkout v0.4.4; python -I install.py --command; python -I install.py <project>
```

(v0.4.4 acima é um EXEMPLO HIPOTÉTICO; substitua os dois tokens V pela versão
mostrada no aviso.)

**São quatro comandos manuais separados. Confira se cada um funciona e PARE na falha antes de prosseguir.** `<project>` é um caminho de diretório de projeto fornecido explicitamente pelo usuário. Repita o último comando para outras cópias instaladas, se desejar. O ACC nunca infere caminhos do clone, nunca executa esses comandos automaticamente e nunca baixa, instala ou atualiza skills instaladas automaticamente.

Os quatro comandos separados, conferidos em sequência, parando na falha:

1. `git fetch --tags`
2. `git checkout v0.4.4` (tag de exemplo)
3. `python -I install.py --command`
4. `python -I install.py <project>`

As visões centrais leem apenas o cache local `update-check.json` ao lado das
preferências globais resolvidas, nunca HTTP: um comando de uma única execução
pode retornar antes do worker desanexado terminar, e a próxima atualização ou
invocação mostra o resultado. Padrões: booleano estrito com padrão `true`;
`false` estrito desativa todas as checagens. A limitação é de 24 horas
rolantes incluindo falhas, com silêncio quando offline e sem aviso para versão
já presente.
