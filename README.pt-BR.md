# agent-crew-chief

**[Read in English](README.md)**

Transforme qualquer CLI de programação com IA em um chefe de equipe: ele divide
uma tarefa não trivial em partes, despacha cada parte para os outros CLIs que
você tem instalados (os gratuitos primeiro), manda o resultado ser revisado por
um modelo diferente, dá uma nota e mantém um placar dentro do projeto.

## O que é

`agent-crew-chief` é uma skill que você instala em um projeto. O CLI em que você
já trabalha (Claude Code, Codex, OpenCode, Cursor…) vira o **coordenador**: ele
planeja o trabalho, escreve os briefs, despacha para os outros CLIs executores
da sua máquina, confere o resultado e reporta para você.

Os executores rodam em worktrees git descartáveis, então nada entra na sua
branch até o coordenador aceitar. Tudo o que o coordenador aprende é escrito em
`docs/crew-chief/` dentro do projeto e commitado junto com a entrega.

## Por quê

- **Gratuito primeiro.** O `pick` ordena os executores pelo histórico deles
  naquele tipo de trabalho, mas os gratuitos vêm primeiro. Você escala para um
  modelo pago só quando os gratuitos não entregaram, seguindo `strong_order`.
- **Revisão de verdade.** Toda mudança é revisada por uma **família de modelo
  diferente** antes de chegar em você. O coordenador continua julgando: a revisão
  é evidência, não veredito.
- **Um placar.** Cada parte recebe uma nota a partir das correções que precisou,
  dos itens do pedido feitos / faltando / extras e de penalidades nomeadas. A nota
  decai com o tempo, então um modelo que foi bom mês passado não fica com passe
  livre para sempre.
- **Nenhum modelo é descartado de vez.** Um executor sem histórico ou abaixo da
  mediana ainda ganha uma segunda chance em trabalho de risco baixo
  (`explore_rate`), de propósito.

## Como funciona

O ciclo de um job:

1. `job new` — o pedido do usuário é registrado literalmente como um job.
2. `piece add` — o pedido é dividido em partes, cada uma com um tipo de trabalho
   e um nível de risco.
3. `pick` — um executor é escolhido para a parte: gratuito primeiro, melhor nota
   com decaimento primeiro, um executor por tentativa.
4. `dispatch` — o executor roda o brief em uma worktree nova com timeout rígido;
   a tentativa é registrada qualquer que seja o status (`ok`, `failed`,
   `timeout`, `quota`, `outside_write`, `brief_refused`).
5. `dispatch --review` — uma família de modelo diferente revisa a worktree.
6. `correction` + `grade` — as correções são registradas, a parte recebe a nota e
   o placar é reescrito.
7. `accept` / `discard` + `close` — o trabalho entra ou é descartado, e o
   coordenador imprime a mensagem de commit e o comentário da issue.

Não há servidor nem daemon: o coordenador comanda o CLI pelo próprio shell, e o
projeto guarda todo o histórico.

## Instalação

```bash
git clone --branch v0.1.0 https://github.com/pauloroos/agent-crew-chief
python agent-crew-chief/install.py /caminho/do/projeto
```

Sem git? Baixe o `agent-crew-chief-v0.1.0.zip` da
[última release](https://github.com/pauloroos/agent-crew-chief/releases/latest),
descompacte e rode `python install.py /caminho/do/projeto` de dentro da pasta.
Fixe uma tag (`--branch vX.Y.Z`) para nunca instalar código no meio de uma mudança;
o `main` pode estar à frente da última release. Veja o [CHANGELOG.md](CHANGELOG.md).

O `install.py` copia a skill para `.claude/skills/crew-chief/` e
`.agents/skills/crew-chief/`, insere um trecho curto entre
`<!-- BEGIN:agent-crew-chief -->` e `<!-- END:agent-crew-chief -->` no `AGENTS.md`
e no `CLAUDE.md` (criados se faltarem), no `GEMINI.md` (só se existir) e em
`.cursor/rules/agent-crew-chief.mdc` (só se `.cursor/` existir, com
`alwaysApply: true`). Também cria `docs/crew-chief/preferences.toml` a partir do
exemplo, além de `docs/crew-chief/jobs/.gitkeep`. Ele imprime cada alteração.

Um `CLAUDE.md` que só contém `@AGENTS.md` é deixado como está. Um arquivo cujos
marcadores você removeu à mão continua assim; use `--force-snippet` para
recolocar o trecho:

```bash
python install.py /caminho/do/projeto --force-snippet
```

Rode o CLI instalado:

```bash
python -I .agents/skills/crew-chief/scripts/crew-chief.py discover --project /caminho/do/projeto
```

Use `.claude/skills/crew-chief/scripts/crew-chief.py` quando for essa a cópia que
o seu coordenador lê. Todo subcomando aceita `--project <caminho>` e `--json`.

## Atualização

Baixe a tag nova e rode o instalador de novo:

```bash
cd agent-crew-chief && git fetch --tags && git checkout vX.Y.Z
python install.py /caminho/do/projeto
```

Os arquivos da skill e o trecho são atualizados. Seu `preferences.toml`, seus
`jobs/` e seu `scoreboard.md` nunca são tocados.

## Preferências

As preferências ficam em `docs/crew-chief/preferences.toml` (criado a partir de
`skill/crew-chief/templates/preferences.example.toml`; uma cópia completa com
comentários vem junto com a skill). Um arquivo global em
`~/.config/agent-crew-chief/preferences.toml` também é sempre lido — defina
`AGENT_CREW_CHIEF_GLOBAL_PREFS` para apontar para outro lugar, e um arquivo
ausente não é problema. Chaves escalares são sobrescritas uma a uma (o projeto
vence), e as tabelas `[[tools]]` / `[[models]]` são mescladas por `name` /
`match` com o projeto vencendo. Um modelo que não casa com nenhuma regra é
`paid`, e uma regra com `cost = "forbidden"` vence em qualquer nível: um
`forbidden` global bate um `free` do projeto.

Os ids de executor têm o formato `<tool>:<model>`, por exemplo
`opencode:opencode-go/longcat-2.5-preview-free`. Os vocabulários fixos são os
tipos de trabalho `code, tests, refactor, design, docs, research, diagnosis,
data, review`, o risco `low, medium, high` e o custo `free, paid, forbidden`.

Exemplo:

```toml
# Ferramentas tentadas na escalada paga, da melhor para a pior.
strong_order = ["my-claude-proxy", "codex", "claude"]

# Chance (0 a 1) de trabalho de risco baixo ganhar uma segunda chance.
explore_rate = 0.1

# Quando true, um modelo com `trains = true` conta como `forbidden`.
forbid_training = false

# Padrões fnmatch de caminhos que um brief nunca pode citar.
sensitive_paths = []

# none, github, plane, linear ou other.
issue_tracker = "none"

# Limite rígido de uma tentativa de dispatch, em segundos.
dispatch_timeout_s = 900

# Id de executor usado por padrão nas revisões; vazio significa "escolha um".
review_default = ""

[[models]]
match = "opencode:opencode/*-free"
cost = "free"

[[models]]
match = "opencode:opencode-go/*-free"
cost = "free"

[[models]]
match = "antigravity:*"
cost = "free"

[[models]]
match = "*-preview-free"
cost = "free"
trains = true

[[models]]
match = "*contributor*"
cost = "free"
trains = true
```

Todas as chaves:

| Chave | Significado |
| --- | --- |
| `strong_order` | Ferramentas tentadas quando o trabalho escala para um modelo pago, da melhor para a pior. Uma ferramenta não listada vai para o fim. |
| `explore_rate` | Chance (de 0 a 1) de uma parte de risco baixo ir para um executor sem histórico ou abaixo da mediana em vez do melhor colocado. Padrão `0.1`. |
| `forbid_training` | Quando true, um modelo cuja regra define `trains = true` é tratado como `forbidden` e nunca é despachado. |
| `sensitive_paths` | Padrões fnmatch de caminhos que um brief nunca pode citar, por exemplo `[".env", "secrets/*"]`. Um brief que cita um deles é recusado como `brief_refused`. |
| `issue_tracker` | `none`, `github`, `plane`, `linear` ou `other`; usado pelo `close`. |
| `dispatch_timeout_s` | Limite rígido de uma tentativa, em segundos. Ao estourar, a árvore de processos é morta e a tentativa é registrada como `timeout`. Padrão `900`. |
| `review_default` | Id de executor usado nas revisões quando o coordenador não tem ideia melhor, por exemplo `codex:gpt-5-codex`. Vazio significa "escolha um você mesmo". |

As regras `[[models]]` classificam um id de executor com fnmatch; a primeira
regra que casa vence:

| Chave | Significado |
| --- | --- |
| `match` | Glob testado contra `<tool>:<model>`. |
| `cost` | `free`, `paid` ou `forbidden`. |
| `trains` | Se o provedor pode treinar com o que você envia. Com `forbid_training = true`, esse modelo vira `forbidden`. |

`[[tools]]` descreve um CLI executor (as ferramentas embutidas `opencode`,
`antigravity`, `codex`, `claude` e `cursor` sempre existem; uma entrada com o
mesmo `name` substitui a embutida):

| Chave | Significado |
| --- | --- |
| `name` | Nome único; o prefixo do id de executor. |
| `kind` | `claude-code`, `opencode`, `codex`, `antigravity` ou `generic`. |
| `bin` | Executável a rodar, procurado no `PATH`. |
| `models` | Lista fixa de ids de modelo que a ferramenta aceita. Vazio significa "pergunte à ferramenta" com `<bin> models`. Os kinds `claude-code` e `codex` sempre usam essa lista fixa. |
| `effort` | Se a ferramenta aceita uma flag de esforço/raciocínio. Padrão true para `codex` e `antigravity`, false nos demais. |
| `env` | Nomes de variáveis de ambiente necessárias. Um nome ausente do ambiente é lido de `HKCU\Environment` no Windows. |
| `cost_default` | Custo dado a um modelo que nenhuma regra `[[models]]` casou. |
| `run_cmd` | Modelo de argv para `kind = "generic"`; os marcadores `{worktree}`, `{model}`, `{prompt}`, `{brief_file}` e `{output_file}` são preenchidos. |
| `list_cmd` | argv que imprime um id de modelo por linha, para `kind = "generic"`. |

## Adicionando um CLI próprio

Um CLI parecido com o Claude Code atrás de uma chave de API de terceiros:

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

O `bin` precisa estar no `PATH`; `env` nomeia as variáveis que a ferramenta
precisa (os valores nunca são impressos nem guardados). Adicione regras
`[[models]]` se o proxy tiver modelos gratuitos. Qualquer outro CLI pode ser
comandado com `run_cmd`:

```toml
[[tools]]
name = "my-generic-cli"
kind = "generic"
bin = "my-generic-cli"
run_cmd = ["my-generic-cli", "run", "--model", "{model}", "{prompt}"]
list_cmd = ["my-generic-cli", "models"]
```

## O que é commitado

Tudo em `docs/crew-chief/`:

- `preferences.toml` — as preferências resolvidas do projeto;
- `jobs/<job_id>.json` e `jobs/<job_id>.md` — o registro de cada job, legível por
  máquina e por gente, com tentativas, correções, escaladas e notas;
- `scoreboard.md` — o ranking com decaimento por tipo de trabalho.

Eles pertencem ao projeto, então um colega (ou a próxima sessão) herda o
histórico. Commitar é o ponto: o placar só serve se sobreviver.

## Matemática do placar

Cada parte avaliada vira um número — o mesmo que `grade` grava no job e que o
`scoreboard.md` usa para ranquear:

```
score = 100 * done / (done + missing)
        - 15 * corrections
        - 5 * extra
        - penalties
score = clamp(round(score), 0, 100)
```

As penalidades são 40 para `destructive`, 60 para `outside_write`, 40 para
`false_done` e 30 para `model_mismatch`. Quando `done + missing == 0`, o termo
de aderência vale 100.

O histórico é ponderado pela idade com meia-vida de 30 dias: uma nota de 30
dias atrás vale metade da de hoje, uma de 60 dias atrás vale um quarto. A nota
mostrada por executor e tipo de trabalho é a média ponderada das notas dele.

A escolha é free primeiro: a melhor nota com decaimento ganha, empates vão para
a menor duração média e, só em trabalho de risco baixo, há 1 chance em 10
(`explore_rate`, padrão `0.1`) de um executor sem nota ou abaixo da mediana
ganhar uma segunda chance. Nenhum modelo é descartado para sempre.

## Coordenadores suportados

Testados: **Claude Code**, **Codex** e **OpenCode**. Esperados funcionar, ainda
não testados de ponta a ponta: **Cursor** e **Antigravity**. O instalador escreve
o trecho no `AGENTS.md`, no `CLAUDE.md`, no `GEMINI.md` e na regra do Cursor, para
que qualquer agente que leia um deles o encontre.

## Limitações

- **Não há sandbox de verdade.** Os executores rodam com as próprias permissões.
  Arquivos escritos fora da worktree são detectados comparando snapshots antes e
  depois da execução, e essa tentativa é rejeitada como `outside_write` — depois
  do fato, não antes.
- **Cotas são texto.** Uma falha de cota ou rate limit é detectada pelo texto de
  erro do próprio executor (`429`, `quota`, `rate limit`, `usage limit`, `too
  many requests`), então uma mensagem fora do padrão é registrada como `failed`.
- **Um timeout mata a árvore de processos** (`taskkill /T` no Windows, o grupo de
  processos nos demais) e deixa a worktree para inspeção; ele não desfaz efeitos
  colaterais que o executor já tenha causado fora da worktree.
- O placar vale o que vale a avaliação: uma parte sem nota não ensina nada ao
  coordenador.

## Licença

MIT. Veja [LICENSE](LICENSE).
