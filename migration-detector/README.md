# Migration Detector

Action para detectar alterações de banco de dados em Pull Requests e classificar o impacto para o time de dados com um classificador determinístico.

## Objetivo

Garantir visibilidade e governança sobre mudanças de schema/migração antes do merge, com classificação automática de risco:

- ⚪ **Sem alteração de banco** (`none`)
- 🟢 **Safe Change** (`safe`)
- 🟡 **Mudança Controlada** (`controlled`)
- 🟠 **Não classificado** (`unknown`) — o classificador não entendeu ao menos uma operação do PR. É um pedido de revisão manual, não uma classificação benigna.
- 🔴 **Breaking Change** (`breaking`)

O que cai em cada uma está em [Severidades](#severidades).

Com isso, o time de dados consegue priorizar review, avaliar impacto em pipelines e reduzir incidentes em produção.

## ⚠️ Release publicada = deploy imediato

`.github/workflows/update-semver-tags-on-release.yml` roda `git tag -f` nas tags
de major e minor a cada release publicada, e os repositórios consumidores fixam
a major. Ou seja: **release publicada = deploy imediato para quem pina aquela
major.**

A subida desta entrega é a **`v5.0.0`**, decidida na
[ADR 0001](../docs/adr/0001-provedor-de-ia-do-migration-detector.md). Não é a
linha `v3.x`: a tag é do repositório inteiro e o repo é público, então uma
`v3.5.0` cortada da main faria a tag `v3` saltar por cima da `v3.4` e da
`v4.0.0` e trocaria o código embaixo de qualquer consumidor que tenha pinado
qualquer uma das outras oito automações naquela major. É major e não minor
porque `highest_severity` ganhou o valor `unknown`, que é valor novo num output
em que consumidor pode ramificar, e `minimum_confidence` ficou inerte.

O custo é um PR de uma linha em cada consumidor, trocando a major fixada por
`@v5`.

> A tag `@v5` só existe depois da Release. Até lá, quem quiser exercitar esta
> versão aponta para a branch.

A janela aberta entre QQ-2159 e QQ-2160 está fechada: o classificador
determinístico de `detect/` está ligado no lugar da chamada de IA, que falhava
em toda migração desde que a GitHub Models API foi desligada em 2026-07-30.

## Como funciona (spec)

1. A action é disparada em eventos de `pull_request`.
2. Um script shell coleta os arquivos alterados no PR e filtra possíveis migrações/DDL.
3. Um script Python lê cada arquivo e o encaminha ao parser do seu stack (SQL, Django, Alembic ou TypeORM).
4. O parser classifica cada operação por severidade, com justificativa, a partir de tabelas — sem rede e sem modelo.
5. A action publica o resultado em um canal do Slack com breve descrição e link do PR.
6. A action pode falhar o job em caso de `Breaking Change`.

## Configuração

### 1. No repositório `iclinic/automations`

A action é consumida como `iclinic/automations/migration-detector@v5` e não requer alteração de código para uso. É necessário apenas garantir que as **variables** abaixo existam no nível da organização, acessíveis aos repositórios que usarão a action.

> `slack_webhook_url` vazia **falha o job em PR que toca migração**: sem canal a action não tem como avisar ninguém, e antes ela saía verde em silêncio. PR que não toca migração passa, com `::warning::` nos checks. Configure antes de adicionar o workflow ao repositório.

Verifique (ou crie) cada variable em `https://github.com/organizations/iclinic/settings/variables/actions` e confira se o repositório consumidor está listado em **Repository access**:

| Variable | Descrição | Obrigatório |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Incoming Webhook do canal de alertas do time de dados. Crie em [api.slack.com/apps](https://api.slack.com/apps) | Sempre |
| `SLACK_CHANNEL_FOR_MIGRATION_ALERTS` | Canal lógico do alerta, ex.: `#data-impact-alerts` | Não |

> **Sem provedor de IA:** a classificação é determinística e roda inteiramente no runner. Não há secret de IA a configurar, nenhuma permissão `models: read` no workflow e nenhuma dependência de Copilot Business/Enterprise. Workflows que ainda declaram `models: read` continuam funcionando — a permissão apenas não é mais usada.

---

### 2. Em cada repositório que usará a action

**2.1** Confirme que os secrets de organização estão acessíveis ao repositório. Caso não estejam, solicite ao time de SRE ou acesse `Settings > Secrets and variables > Actions` no repositório e verifique a herança.

**2.2** Crie o arquivo `.github/workflows/migration-detector.yml` com o seguinte conteúdo:

```yml
name: Migration Detector

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  migration-detector:
    runs-on: ubuntu-latest

    permissions:
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Detect DB migration impact
        id: migration_detector
        uses: iclinic/automations/migration-detector@v5
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          slack_webhook_url: ${{ vars.SLACK_WEBHOOK_URL }}
          slack_channel: ${{ vars.SLACK_CHANNEL_FOR_MIGRATION_ALERTS }}
```

**2.3 (Opcional — recomendado para Breaking Change)** Configure uma **branch protection rule** na branch principal para exigir que o job `migration-detector` passe antes do merge:

1. Acesse `Settings > Branches > Add rule` no repositório.
2. Em **Require status checks to pass before merging**, adicione `migration-detector`.
3. Isso garante que um `🔴 Breaking Change` bloqueie o merge automaticamente.

**2.4 (Opcional)** Se o projeto tiver um padrão diferente de estrutura de migrações, sobrescreva os globs padrão. A conversão de glob para regex só entende `*`, `**` e ponto literal — `?`, `[]` e `{}` fazem o step falhar com erro explícito, em vez de silenciosamente não casar nada:

```yml
          migration_paths: "**/db/migrate/*.rb,**/*.sql,**/migrations/*.py"
          ignore_name_contains: "dump,seed,fixture"
```

---

## Regras de detecção de mudança de banco

A detecção **não** deve se limitar a arquivos `.sql`.

### Inclusões obrigatórias

Os globs padrão de `migration_paths`, um por stack suportado:

- Arquivos SQL (`**/*.sql`)
- Migrações Django (`**/migrations/*.py`)
- Migrações Alembic/FastAPI (`**/alembic/versions/*.py`)
- Migrações TypeORM (`**/migrations/*.ts`, mais `**/*AutoMigrate.ts` e
  `**/*automigrate.ts`, que é como o consumidor TypeORM nomeia as dele)

### Exclusões obrigatórias

- Qualquer arquivo cujo nome contenha `dump` (ex.: `db_dump.sql`, `users_dump_2026.sql`), pois representa uso local.

### Estratégia recomendada

- Filtrar por padrões de inclusão.
- Remover da lista final qualquer item com `dump` no nome do arquivo (case-insensitive).
- Se restar ao menos um arquivo candidato, seguir para a classificação determinística.

## Severidades

Cinco severidades, nesta ordem de gravidade:

```
none < safe < controlled < unknown < breaking
```

A severidade de um arquivo é a pior das operações dele, e a do PR é a pior
entre os arquivos. `unknown` fica acima de `controlled` de propósito: "não
consegui classificar" tem que pesar mais que uma classificação benigna quando
as severidades se agregam. E fica abaixo de `breaking` porque não pode esconder
uma quebra que já foi identificada.

| Severidade | No Slack | O que quer dizer |
|---|---|---|
| `none` | ⚪ Sem alteração de banco | A operação não chega ao banco |
| `safe` | 🟢 Safe Change | Adição que não afeta quem já lê o schema atual |
| `controlled` | 🟡 Mudança Controlada | Muda o schema sem quebrar leitor existente |
| `unknown` | 🟠 Não classificado | O classificador não leu a operação. Pedido de revisão manual |
| `breaking` | 🔴 Breaking Change | Quem lê o schema atual quebra |

### O que cai em cada uma

`none` — a operação existe no arquivo e não emite DDL. `AlterModelOptions` do
Django é metadado; um `AlterField` que só mexe em `choices`, `verbose_name`,
`help_text`, `default`, `blank`, `editable` ou `validators` fica no estado do
Django e não chega ao banco. Em SQL, `BEGIN`, `COMMIT` e `SET`. Um arquivo sem
operação nenhuma — `__init__.py` de pacote `migrations/`, migração de merge,
barrel de TypeScript — também sai `none`, com a razão dizendo que não há o que
reportar.

`safe` — tabela nova (`CreateModel`, `op.create_table`, `CREATE TABLE`), coluna
nova que aceita `NULL` ou traz `DEFAULT` (`AddField`, `op.add_column`,
`ADD COLUMN`), índice não único (`AddIndex`, `op.create_index`,
`CREATE INDEX`), valor novo em enum (`ALTER TYPE ... ADD VALUE`).

`controlled` — coluna nova `NOT NULL` sem `DEFAULT`, que barra a migração numa
tabela que já tem linha; coluna que passou a aceitar `NULL`; coluna que ficou
mais larga; mudança de `DEFAULT`; remoção de índice ou de constraint;
`unique_together` esvaziado; e `RunPython` de dados, cujo corpo o classificador
varre por DDL e não encontra nenhum — quando encontra, quem decide é o DDL.

`unknown` — a seção abaixo.

`breaking` — remoção de coluna ou de tabela (`RemoveField`, `DeleteModel`,
`op.drop_column`, `op.drop_table`, `DROP COLUMN`, `DROP TABLE`); renomeação de
qualquer das duas; mudança de tipo; coluna que passou a `NOT NULL`; coluna que
ficou mais curta; e todo índice ou constraint **único** novo
(`AddConstraint(UniqueConstraint)`, `op.create_unique_constraint`,
`op.create_index(unique=True)`, `CREATE UNIQUE INDEX`, `ADD CONSTRAINT ...
UNIQUE`, `unique_together` que passou a exigir combinação nova). O índice único
não quebra quem lê, mas quebra quem grava: a migração falha se a tabela já tem
duplicata, e o `INSERT` que antes passava passa a estourar. É a mesma severidade
nos quatro parsers, porque a mesma mudança não pode sair com duas cores conforme
o stack.

### `unknown` não é uma classificação benigna

`unknown` quer dizer que o classificador leu o arquivo e **não soube dizer** o
que uma das operações faz. Não é "provavelmente tudo bem", não é o meio-termo
entre `safe` e `controlled`, e não é erro do step — o job continua verde e o
alerta chega ao Slack em laranja, pedindo que alguém olhe.

Sai `unknown`:

- `AlterField` do Django ou `op.alter_column` do Alembic sem estado anterior. A
  operação carrega só a definição nova, e comparar é a única forma de saber o
  que mudou;
- `RunSQL`, `op.execute` ou `queryRunner.query` com SQL montado por f-string ou
  por template com interpolação. O SQL que vai rodar não está escrito no
  arquivo;
- `AlterUniqueTogether` cujo valor não é literal, e ramo de
  `SeparateDatabaseAndState` montado fora da chamada;
- operação de Alembic fora da tabela do parser: `op.create_foreign_key`,
  `op.create_check_constraint`, `op.bulk_insert`, `op.batch_alter_table`;
- verbo ou ação de SQL que o parser não reconhece, incluindo
  `ALTER TYPE ... RENAME`;
- os quatro casos em que o dispatch não sabe a quem entregar o arquivo, na
  seção [Classificador determinístico](#classificador-determinístico);
- arquivo do diff com cara de migração — sob `migrations/` ou
  `alembic/versions/`, ou com nome prefixado por timestamp — que nenhum padrão
  de `migration_paths` casou. O classificador não chegou a lê-lo, e é isso que
  a linha do Slack diz. O conserto é ajustar `migration_paths` no workflow.

Como `unknown` é mais grave que `controlled`, ele governa a severidade do PR
sempre que nada pior aparece. A recomendação de governança é a mesma que a de
`breaking`: bloquear o merge até alguém do time de dados olhar. Um `unknown`
honesto vale mais que um palpite confiante, e foi exatamente o palpite
confiante — quatro semanas de `controlled` com confiança 0.0 para toda migração —
que motivou esta entrega.

### Os três stacks, e o SQL que atravessa todos

Cada um dos três repositórios consumidores escreve migração de um jeito, e cada
um tem um parser:

| Consumidor | Stack | Onde as migrações moram | Parser |
|---|---|---|---|
| Django/MySQL | Django | `django/app/*/migrations/*.py` | `detect/django.py` (+ `detect/history.py`) |
| Alembic/PostgreSQL | Alembic | `**/versions/*.py` | `detect/alembic.py` |
| TypeORM/PostgreSQL | TypeORM | `migrations/*.ts` | `detect/typeorm.py` |

`detect/sql.py` é o quarto parser e não tem repositório próprio: ele lê os
`**/*.sql` soltos e é para onde os outros três delegam quando encontram SQL
literal dentro de um `RunSQL`, de um `op.execute` ou de um
`queryRunner.query`. É por isso que um `DROP COLUMN` escrito à mão dentro de uma
migração do Django sai com a mesma severidade que o `RemoveField` equivalente.

O Django é o único que precisa de mais que o arquivo: `detect/history.py`
reconstrói o estado do app anterior à migração lendo os ancestrais no diretório
`migrations/`. Sem esse estado, `AlterField` — a operação mais comum do corpus —
sai `unknown`, porque a definição nova sozinha não diz o que mudou. Daí o
`fetch-depth: 0` no checkout.

## Contrato de Entrada/Saída da Action

### Inputs sugeridos

- `github_token` (obrigatório): **não é mais lido pela action.** Era a credencial da GitHub Models API; os metadados do PR vêm do contexto `github.event.pull_request`, que não precisa de token. Continua obrigatório por uma versão para não quebrar quem já o passa.
- `ai_api_url` (opcional): **DEPRECADO e inerte.** A action não chama API de IA. Continua declarado por uma versão para não quebrar quem já o passa; passá-lo emite aviso de depreciação. Pode ser removido do workflow.
- `ai_api_key` (opcional): **DEPRECADO e inerte.** Idem.
- `ai_model` (opcional): **DEPRECADO e inerte.** Idem.
- `migration_paths` (opcional): globs de entrada para arquivos de migração.
- `ignore_name_contains` (opcional, default `dump`): termos para ignorar no nome do arquivo.
- `slack_webhook_url` (obrigatório): webhook para envio da mensagem ao canal Slack.
- `slack_channel` (opcional): nome lógico do canal para exibir no payload/log.
- `minimum_confidence` (opcional, default `0.70`): **DEPRECADO e inerte.** Continua declarado para não quebrar quem já o passa, mas nenhum valor tem efeito — não existe mais promoção de `safe` para `controlled` por limiar. Operação que o classificador não reconhece sai como `unknown`.

### Outputs sugeridos

- `has_db_change`: `true|false`
- `highest_severity`: `none|safe|controlled|unknown|breaking`, nessa ordem de gravidade
- `slack_message_ts`: identificador da mensagem no Slack (quando disponível)
- `confidence`: **DEPRECADO.** Mantido só para quem já lê o output. `0.0` quando `highest_severity` é `unknown`, `1.0` em qualquer outro caso. Use `highest_severity == 'unknown'`.

> O job falha quando não consegue classificar. Não existe mais resultado de fallback: erro de leitura de arquivo, severidade fora do vocabulário, step chamado sem arquivo nenhum — todos derrubam o step em vez de mandar um alerta amarelo genérico para o Slack. `slack_webhook_url` vazia também falha, porque sem canal a action não tem como avisar ninguém.
>
> O que o classificador **não** entende não derruba o step: sai como `unknown` (🟠), que é um pedido de revisão manual e chega ao Slack como tal. Um `unknown` honesto vale mais que um palpite confiante.

## Classificador determinístico

Não há provedor de IA, chave, modelo nem chamada de rede. Cada arquivo é
encaminhado ao parser do seu stack por `detect/__init__.py`:

| Stack | Entra por | Confirmado por |
|---|---|---|
| SQL | `.sql` | — |
| TypeORM | `.ts` | o arquivo traz `implements MigrationInterface` |
| Django | `.py` | o módulo declara uma classe `Migration` |
| Alembic | `.py` | o módulo declara uma função `upgrade` |

O stack é decidido pelo **conteúdo** e não pelo caminho. `**/migrations/*.py` e
`**/alembic/versions/*.py` se sobrepõem, e os dois frameworks permitem mudar o
diretório (`script_location`, `MIGRATION_MODULES`); `**/migrations/*.ts` é do
TypeORM tanto quanto de qualquer outro migrador de TypeScript. O marcador de
cada stack é o contrato de runtime do framework — o Django carrega
`module.Migration`, o Alembic chama `module.upgrade()`, o TypeORM exige
`implements MigrationInterface`.

`.sql` é a única linha sem marcador, e por um motivo: `sql.py` classifica todo
statement e devolve `unknown` para o verbo que não reconhece, então um `.sql` de
dialeto estranho já sai visível pelo próprio parser.

Entregar a fonte de um stack ao parser do outro não estoura: os dois devolvem
lista vazia, que se lê como "nada a reportar". Por isso o dispatch nunca
responde silêncio quando não sabe decidir. Saem como `unknown`:

- extensão que não tem parser;
- arquivo `.py` que não é Python válido;
- arquivo que casa os marcadores de Django e de Alembic ao mesmo tempo;
- arquivo que declara operações num framework que este pacote não conhece —
  yoyo, South, peewee, Knex, Prisma. Ele casou `migration_paths`, então alguém o
  considera migração;
- arquivo cujo parser foi escolhido e voltou vazio embora o arquivo declare
  operações. É o caso do South, que traz `class Migration` — casando o marcador
  do Django — mas guarda as operações em `def forwards`.

O que separa "não é uma migração" de "é uma migração que eu não sei ler" é
declarar operações, e em todo dialeto uma operação é uma **chamada**. Um
`__init__.py` de pacote `migrations/`, um módulo auxiliar ou um barrel de
TypeScript não tem chamada nenhuma e sai com zero findings, sem alarme.

No caso do Django, `detect/history.py` reconstrói o estado do app anterior à
migração a partir do diretório `migrations/` em disco, o que exige
`fetch-depth: 0` no checkout (os três workflows já fazem). É ele que permite
dizer se um `AlterField` encurtou a coluna, tornou o campo `NOT NULL` ou só
mexeu num `help_text`: sem esse estado, `AlterField` sai `unknown`.

## Fluxo interno (shell + Python)

### 1) Shell — coleta de arquivos (`Collect | Find changed migration files`)

Responsabilidades:
- Comparar base/head do PR via `git diff --name-only`
- Converter globs de `migration_paths` em expressões regulares
- Filtrar arquivos que não batem com nenhum padrão de inclusão
- Ignorar qualquer arquivo cujo nome contenha os termos de `ignore_name_contains` (ex.: `dump`) — case-insensitive
- Serializar a lista final com delimitador `|` para passar ao step Python via `GITHUB_OUTPUT`

Filtro aplicado (gerado dinamicamente a partir dos globs configurados):

```bash
git diff --name-only "$BASE_SHA" "$HEAD_SHA" \
  | grep -E "(\.sql$|.*/migrations/.*\.py$|.*/alembic/versions/.*\.py$|.*[Aa]uto[Mm]igrate\.ts$)" \
  | grep -Eiv "dump" || true
```

### 2) Python — classificação (`Analyze | Classify migrations`)

Responsabilidades:
- Ler cada arquivo de migração **inteiro** (não há cap de bytes: o cap de 6 KB existia para o contexto do modelo e cortava migrações no meio)
- Encaminhar cada arquivo ao parser do seu stack
- Classificar cada operação (`none`, `safe`, `controlled`, `unknown`, `breaking`)
- Listar **todo** arquivo recebido em `items`, mesmo os que não renderam operação nenhuma
- Consolidar severidade máxima entre todos os arquivos
- Montar mensagem curta para Slack com categoria, resumo e link do PR
- Gravar outputs em `GITHUB_OUTPUT`

Formato de saída sugerido:

```json
{
	"has_db_change": true,
	"highest_severity": "breaking",
	"confidence": 1.0,
	"pr_url": "https://github.com/org/repo/pull/123",
	"slack_text": "🔴 *Breaking Change* detectada em migração de banco\n...",
	"items": [
		{
			"file": "apps/core/migrations/0042_drop_qty.sql",
			"severity": "breaking",
			"operation": "DROP COLUMN",
			"reason": "Coluna `qty` removida de `orders` — quem lê essa coluna quebra."
		},
		{
			"file": "apps/core/migrations/__init__.py",
			"severity": "none",
			"operation": "",
			"reason": "Nenhuma operação de banco encontrada neste arquivo."
		}
	]
}
```

Há um item por operação classificada, mais um item `none` para cada arquivo que
não rendeu operação nenhuma: **todo arquivo recebido aparece em `items`**. Um
arquivo que some da saída é indistinguível de um arquivo sem mudança de banco.

`confidence` é `0.0` quando `highest_severity` é `unknown` e `1.0` em qualquer
outro caso — não há gradação num classificador determinístico. A contagem de
itens não é comparável entre stacks: o parser do TypeORM devolve um item por
statement, enquanto os de Django e Alembic colapsam um `RunSQL`/`op.execute` de
vários statements num item só.

## Formato de mensagem no Slack (obrigatório)

A action deve enviar um post no canal configurado contendo:

- Categoria detectada: `⚪ Sem alteração de banco`, `🟢 Safe Change`,
  `🟡 Mudança Controlada`, `🟠 Não classificado` ou `🔴 Breaking Change`
- Breve descrição da mudança
- Link do PR para detalhes

Uma linha por operação classificada, nomeando o arquivo e a operação antes da
razão. O arquivo e a operação estão ali por causa de `unknown`: "não entendi uma
operação" só serve para quem lê se disser qual arquivo e qual operação. Operações
`none` não viram linha.

```text
🔴 *Breaking Change* detectada em migração de banco
*PR:* <https://github.com/org/repo/pull/123|#123 — Remove qty> por @fulano
• `app/core/migrations/0042_drop_qty.py` — `RemoveField`: Campo `qty` removido de `order` — quem lê essa coluna quebra.
<https://github.com/org/repo/pull/123|Ver PR para detalhes>
```

Razão e operação chegam prontas dos parsers de `detect/`. Nada aqui é gerado a
partir do conteúdo da migração, e é de propósito: migração de dados carrega
linha de paciente, e esta mensagem vai para um canal do Slack.

## Regras de governança recomendadas

Na ordem de gravidade:

- `none`: nenhum dos arquivos do PR mexe no banco.
- `safe`: seguir fluxo normal.
- `controlled`: exigir aprovação do time de dados.
- `unknown`: bloquear o merge e exigir revisão do time de dados. O classificador não entendeu ao menos uma operação, e a operação que ele não leu pode ser qualquer uma.
- `breaking`: bloquear merge automaticamente.

## Segurança e observabilidade

- Nunca imprimir `slack_webhook_url` em logs.
- **Nenhum dado da migração sai da action.** Migração de dados carrega linha de paciente; nem o texto do Slack nem o `analysis_json` podem citar conteúdo de arquivo. Razão e operação vêm prontas dos parsers, que cortam valores antes de citar um statement, e há teste de invariante cruzando um corpus adversarial contra todos os parsers e contra o que sai da action.
- Logar decisões de classificação com justificativa.
- Registrar payload final enviado ao Slack sem segredos.

## Suíte de testes e o corpus versionado

A suíte roda no CI (`.github/workflows/migration-detector-tests.yml`) em todo PR
que toca `migration-detector/`, com piso de 90% de cobertura por linha sobre
`detect/`.

O portão de regressão de determinismo vive em `tests/test_corpus.py` e roda
sobre `tests/fixtures/corpus/`, que tem quatro stacks (`django/`, `alembic/`,
`typeorm/`, `sql/`) mais `dispatch/`, os arquivos que exercitam as razões que o
próprio dispatch produz. Cada arquivo tem uma entrada em `manifest.json` com a
severidade, a operação e a **frase renderizada** de cada finding; o teste
compara a lista inteira, em ordem.

O que o portão prende não é uma taxa: é a partição exata entre o que o
classificador resolve e o que ele não resolve. A constante `RESIDUE` lista, por
nome e com o motivo, todo arquivo que pode voltar `unknown`, e o teste falha nas
duas direções — um `unknown` novo é regressão de cobertura, e um `unknown` que
sumiu é palpite ocupando o lugar de uma resposta honesta.

### O corpus tem que ficar fora de lint e de compilação

`tests/fixtures/corpus/dispatch/broken_syntax_migration.py` é um erro de sintaxe
de propósito: é a fixture da linha `NOT_PYTHON` do dispatch, e só prova o que tem
que provar não compilando. `# ruff: noqa` não ajuda — não é queixa de lint.

**Qualquer passo que venha a rodar `ruff`, `flake8`, `mypy` ou
`python -m compileall` sobre o repositório precisa excluir
`migration-detector/tests/fixtures/corpus/`.** Hoje não há nenhum configurado e
nada quebra; o primeiro a entrar quebra aqui.

Pelo mesmo motivo, `corpus_files()` em `tests/test_corpus.py` filtra
`__pycache__` e `.pyc`. Sem o filtro, um `compileall` ou uma IDE que compile a
árvore de fixtures põe bytecode dentro do corpus e cinco testes ficam vermelhos —
dois deles do portão de determinismo —, com cara de regressão do classificador.

### Por que o corpus é escrito e não copiado

`iclinic/automations` é um repositório **público**. Vendorizar as 638 migrações
dos três repositórios consumidores publicaria o schema de produção dos três
serviços, além dos nomes de parceiros comerciais e da lista de fornecedores que
algumas migrações de dados carregam.

Então cada fixture é escrita, e existe por um motivo declarado: uma linha de uma
tabela de regras, um caso de resíduo, um dos arquivos citados na ADR. **Ao
adicionar uma fixture, escreva-a — não copie de um repositório consumidor.**
`tests/test_pii.py` tem a invariante que arrebenta se alguém copiar: nenhuma
razão do corpus pode citar número longo ou aspa.

Os identificadores das fixtures também são sintéticos — app, modelo, tabela,
coluna e nome de arquivo. Nenhum nome de app, de tabela ou de coluna de produção
aparece no corpus, e nenhum dos três consumidores é citado pelo nome do seu
repositório. **Fixture nova segue a mesma regra.** O mapa que amarra cada
fixture da ADR ao arquivo real que ela reproduz mora em
`tests/fixtures/.identifier-map.json`, fora do repositório e no `.gitignore`.

A medição real dos três consumidores continua registrada em `REAL_CORPUS` e
reconferida por `TestTheRealCorpus`, que pula sozinho quando os clones — ou o
mapa — não estão ao lado, que é sempre o caso no CI, como o aceite exige.

## Resultado esperado para o time de dados

Com essa action, todo PR com possível alteração de banco passa a ter classificação de impacto automática e rastreável, reduzindo risco de quebra em pipelines, dashboards e integrações downstream.
