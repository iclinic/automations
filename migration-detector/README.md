# Migration Detector

Action para detectar alterações de banco de dados em Pull Requests, usando IA para classificar o impacto para o time de dados.

## Objetivo

Garantir visibilidade e governança sobre mudanças de schema/migração antes do merge, com classificação automática de risco:

- 🟢 **Safe Change**
- 🟡 **Mudança Controlada**
- 🔴 **Breaking Change**

Com isso, o time de dados consegue priorizar review, avaliar impacto em pipelines e reduzir incidentes em produção.

## Como funciona (spec)

1. A action é disparada em eventos de `pull_request`.
2. Um script shell coleta os arquivos alterados no PR e filtra possíveis migrações/DDL.
3. Um script Python envia o contexto da mudança para um modelo de IA.
4. A IA classifica a mudança por severidade e gera justificativa.
5. A action publica o resultado em um canal do Slack com breve descrição e link do PR.
6. A action pode falhar o job em caso de `Breaking Change`.

## Configuração

### 1. No repositório `iclinic/automations`

A action já está disponível em `iclinic/automations/migration-detector@v1` e não requer alteração de código para uso. É necessário apenas garantir que os secrets abaixo existam no nível da organização, acessíveis aos repositórios que usarão a action.

Verifique (ou crie) cada secret em `https://github.com/organizations/iclinic/settings/secrets/actions/<NOME_DO_SECRET>` e confira se o repositório consumidor está listado em **Repository access**:

| Secret | Descrição | Obrigatório |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Incoming Webhook do canal de alertas do time de dados. Crie em [api.slack.com/apps](https://api.slack.com/apps) | Sempre |
| `AI_HUB_URL` | URL base do hub externo de IA (ex.: `https://ia.suaempresa.com/v1`) | Apenas se **não** usar GitHub Models |
| `AI_HUB_API_KEY` | Chave de API do hub externo de IA | Apenas se **não** usar GitHub Models |

> **Nota sobre GitHub Models:** a action usa a GitHub Models API por padrão (`https://models.inference.ai.azure.com`) sem precisar de secret extra. Para isso, a organização precisa ter o **GitHub Copilot Business ou Enterprise** habilitado. Verifique em `Settings > Copilot > Policies > Allow GitHub Models`.

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
      models: read  # necessário para GitHub Models API (padrão)

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Detect DB migration impact
        id: migration_detector
        uses: iclinic/automations/migration-detector@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          slack_webhook_url: ${{ secrets.SLACK_WEBHOOK_URL }}
          slack_channel: "#data-impact-alerts"
          fail_on_breaking: "true"
```

**2.3** Se o repositório usa **hub externo de IA** em vez da GitHub Models API, adicione os três inputs abaixo ao step:

```yml
          ai_api_url: ${{ secrets.AI_HUB_URL }}
          ai_api_key: ${{ secrets.AI_HUB_API_KEY }}
          ai_model: nome-do-modelo-no-hub
```

**2.4 (Opcional — recomendado para Breaking Change)** Configure uma **branch protection rule** na branch principal para exigir que o job `migration-detector` passe antes do merge:

1. Acesse `Settings > Branches > Add rule` no repositório.
2. Em **Require status checks to pass before merging**, adicione `migration-detector`.
3. Isso garante que um `🔴 Breaking Change` bloqueie o merge automaticamente.

**2.5 (Opcional)** Se o projeto tiver um padrão diferente de estrutura de migrações, sobrescreva os globs padrão:

```yml
          migration_paths: "**/db/migrate/*.rb,**/*.sql,**/migrations/*.py"
          ignore_name_contains: "dump,seed,fixture"
```

---

## Regras de detecção de mudança de banco

A detecção **não** deve se limitar a arquivos `.sql`.

### Inclusões obrigatórias

- Arquivos SQL (`**/*.sql`)
- Migrações Django (`**/migrations/*.py`)
- Migrações Alembic/FastAPI (`**/alembic/versions/*.py`)
- Scripts TypeScript de migração como `AutoMigrate.ts` (ou variações de nome)

### Exclusões obrigatórias

- Qualquer arquivo cujo nome contenha `dump` (ex.: `db_dump.sql`, `users_dump_2026.sql`), pois representa uso local.

### Estratégia recomendada

- Filtrar por padrões de inclusão.
- Remover da lista final qualquer item com `dump` no nome do arquivo (case-insensitive).
- Se restar ao menos um arquivo candidato, seguir para classificação com IA.

## Critérios de Classificação

### 🟢 Safe Change

**Descrição**
Adição de algo em alguma tabela:

**Exemplos**
- Adicionar coluna opcional
- Adicionar nova tabela não consumida
- Adicionar novo valor enum com fallback
- Adicionar índice
- Adicionar campo novo na camada analítica

---

### 🟡 Mudança Controlada

**Descrição**
Alteração de colunas em alguma tabela:

**Exemplos**
- Alterar tamanho de varchar
- Alterar precisão numérica
- Tornar campo nullable
- Alterar valor default

---

### 🔴 Breaking Change

**Descrição**
Remoção de alguma coluna ou tabela:

**Exemplos**
- Remover campo
- Renomear campo
- Alterar tipo de dado
- Alterar chave primária
- Remoção de tabela consumida

## Contrato de Entrada/Saída da Action

### Inputs sugeridos

- `github_token` (obrigatório): token do GitHub para ler metadados do PR. Quando `ai_api_key` não é fornecido, também é usado como credencial na GitHub Models API.
- `ai_api_url` (opcional, default `https://models.inference.ai.azure.com`): URL base da API de IA. Altere apenas para integração com hub externo.
- `ai_api_key` (opcional): chave de API do hub externo de IA. **Quando omitido**, a action usa o `github_token` contra a GitHub Models API sem nenhum secret adicional.
- `ai_model` (opcional, default `gpt-4o-mini`): modelo de IA a utilizar. O default é compatível com GitHub Models; ajuste para o modelo disponível no hub externo.
- `migration_paths` (opcional): globs de entrada para arquivos de migração.
- `ignore_name_contains` (opcional, default `dump`): termos para ignorar no nome do arquivo.
- `slack_webhook_url` (obrigatório): webhook para envio da mensagem ao canal Slack.
- `slack_channel` (opcional): nome lógico do canal para exibir no payload/log.
- `fail_on_breaking` (opcional, default `true`): falha o job em `Breaking Change`.
- `minimum_confidence` (opcional, default `0.70`): limiar mínimo de confiança da IA. Abaixo desse valor, a classificação é promovida para `controlled`.

### Outputs sugeridos

- `has_db_change`: `true|false`
- `highest_severity`: `safe|controlled|breaking|none`
- `slack_message_ts`: identificador da mensagem no Slack (quando disponível)
- `confidence`: confiança da classificação (0-1)

## Provedor de IA: GitHub Models (padrão) vs. Hub externo

A action suporta dois provedores de IA intercambiáveis via protocolo OpenAI-compatible:

| Provedor | Quando ativa | Secret necessário |
|---|---|---|
| **GitHub Models API** (padrão) | `ai_api_key` vazio | Apenas `GITHUB_TOKEN` |
| **Hub externo** | `ai_api_key` fornecido | `AI_HUB_API_KEY` + `AI_HUB_URL` |

A GitHub Models API (`https://models.inference.ai.azure.com`) é o caminho preferencial: não exige secret adicional, usa o `GITHUB_TOKEN` já disponível em qualquer workflow, e requer apenas a permissão `models: read`.

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

### 2) Python — análise com IA (`Analyze | Classify migrations with AI`)

Responsabilidades:
- Resolver provedor de IA: `ai_api_key` presente → hub externo; ausente → GitHub Models API com `github_token`
- Ler cada arquivo de migração (cap de 6 KB por arquivo para respeitar o contexto do modelo)
- Montar prompt estruturado com os critérios de classificação embutidos no system prompt
- Classificar cada alteração (`safe`, `controlled`, `breaking`, `none`)
- Elevar para `controlled` quando confiança abaixo de `minimum_confidence`
- Consolidar severidade máxima entre todos os arquivos
- Montar mensagem curta para Slack com categoria, resumo e link do PR
- Gravar outputs em `GITHUB_OUTPUT`

Formato de saída sugerido:

```json
{
	"has_db_change": true,
	"highest_severity": "breaking",
	"confidence": 0.92,
	"pr_url": "https://github.com/org/repo/pull/123",
	"slack_text": "🔴 Breaking Change detectada em migração de banco. Remoção de coluna consumida. PR: https://github.com/org/repo/pull/123",
	"items": [
		{
			"file": "apps/core/migrations/202602250001_drop_qty.sql",
			"severity": "breaking",
			"reason": "Remove coluna consumida por camada analítica"
		}
	]
}
```

## Formato de mensagem no Slack (obrigatório)

A action deve enviar um post no canal configurado contendo:

- Categoria detectada (`🟢 Safe Change`, `🟡 Mudança Controlada`, `🔴 Breaking Change`)
- Breve descrição da mudança
- Link do PR para detalhes

Exemplo:

```text
🔴 Breaking Change
Remoção do campo qty na tabela database.table.
Detalhes: https://github.com/org/repo/pull/123
```

## Regras de governança recomendadas

- Se `highest_severity == breaking`: bloquear merge automaticamente.
- Se `highest_severity == controlled`: exigir aprovação do time de dados.
- Se `highest_severity == safe`: seguir fluxo normal.
- Em baixa confiança da IA (`confidence < minimum_confidence`): classificar como `controlled` por segurança.

## Boas práticas de prompt para IA

- Fornecer contexto técnico: DDL, nome da tabela, coluna, tipo anterior/novo.
- Pedir resposta em JSON estrito para facilitar parsing.
- Incluir exemplos positivos e negativos por categoria.
- Pedir incerteza explícita para evitar falso positivo/negativo silencioso.

## Segurança e observabilidade

- Nunca imprimir `ai_api_key` em logs.
- Nunca imprimir `slack_webhook_url` em logs.
- Sanitizar SQL antes de enviar para IA quando houver dados sensíveis.
- Logar decisões de classificação com justificativa.
- Registrar payload final enviado ao Slack sem segredos.

## Resultado esperado para o time de dados

Com essa action, todo PR com possível alteração de banco passa a ter classificação de impacto automática e rastreável, reduzindo risco de quebra em pipelines, dashboards e integrações downstream.
