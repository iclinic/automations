# Jira Update Due Date

Action que atualiza o campo **Due Date** de issues do Jira que:

- Estão em um status configurável (padrão: `In Progress`);
- Estão sem `duedate`;
- Possuem `Story Points`.

A data é calculada como `hoje + N dias úteis`, onde `N` vem de um mapa
`Story Points → dias úteis` configurável. Quando a issue tem subtarefas
com `duedate`, o maior entre elas é usado como override.

## Quando usar

Em automatizações de cadência (geralmente agendadas via `cron`) para
manter o `Due Date` de cards em andamento alinhado à estimativa em
Story Points — sem exigir preenchimento manual.

## Configuração

### 1. Segredos / variáveis do repositório consumidor

| Nome | Tipo | Obrigatório | Descrição |
|---|---|---|---|
| `JIRA_BASE_URL` | secret | sim | `https://<sua_org>.atlassian.net` |
| `JIRA_USER_EMAIL` | secret | sim | E-mail da conta de serviço |
| `JIRA_API_TOKEN` | secret | sim | API token criado em [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `JIRA_STORY_POINTS_FIELD` | var | sim | ID(s) do customfield de SP, separados por vírgula. Ex.: `customfield_11030,customfield_10008` |
| `JIRA_STATUS_IN_PROGRESS` | var | não | Nome do status. Padrão: `In Progress` |

### 2. Arquivo de configuração de escopos

Crie um JSON no repositório consumidor (caminho padrão: `.github/jira-scopes.json`)
com o schema abaixo:

```json
{
  "story_points_to_business_days": {
    "1": 1,
    "2": 2,
    "3": 3,
    "5": 5,
    "8": 10
  },
  "scopes": [
    { "project": "ABC" },
    {
      "project": "XYZ",
      "filters": {
        "[CX] Produto": "MeuProduto"
      }
    }
  ]
}
```

- `story_points_to_business_days`: mapa `SP → dias úteis`. SPs fora deste
  mapa fazem a issue ser ignorada (com log).
- `scopes`: lista de combinações `project` + `filters` opcionais (cada
  filtro vira `AND "<field>" = "<value>"`). Múltiplos escopos são
  combinados via `OR`.

### 3. Workflow consumidor

Crie `.github/workflows/jira-update-due-date.yml` no seu repositório:

```yaml
name: Jira - Atualiza Due Date por Story Points

on:
  schedule:
    # GitHub Actions cron é em UTC. BRT = UTC-3.
    # 12h BRT -> 15h UTC | 18h BRT -> 21h UTC
    - cron: '0 15,21 * * *'
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Apenas simular as atualizações (não escreve no Jira)'
        required: false
        default: 'false'
        type: choice
        options:
          - 'false'
          - 'true'

permissions:
  contents: read

jobs:
  update-due-date:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Atualizar Due Date no Jira
        uses: iclinic/automations/jira-update-due-date@v1
        with:
          jira_base_url: ${{ secrets.JIRA_BASE_URL }}
          jira_user_email: ${{ secrets.JIRA_USER_EMAIL }}
          jira_api_token: ${{ secrets.JIRA_API_TOKEN }}
          jira_story_points_field: ${{ vars.JIRA_STORY_POINTS_FIELD }}
          # opcionais:
          jira_status_in_progress: ${{ vars.JIRA_STATUS_IN_PROGRESS }}
          jira_scopes_file: .github/jira-scopes.json
          dry_run: ${{ github.event.inputs.dry_run || 'false' }}
```

## Inputs

| Input | Obrigatório | Default | Descrição |
|---|---|---|---|
| `jira_base_url` | sim | — | URL base da instância Jira |
| `jira_user_email` | sim | — | E-mail da conta de serviço |
| `jira_api_token` | sim | — | Token de API do Jira |
| `jira_story_points_field` | sim | — | Customfield(s) de Story Points, separados por vírgula |
| `jira_scopes_file` | não | `.github/jira-scopes.json` | Caminho do JSON de scopes no repo consumidor |
| `jira_status_in_progress` | não | `In Progress` | Nome do status no JQL |
| `python_version` | não | `3.11` | Versão do Python usada para rodar o script |
| `dry_run` | não | `false` | Se `true`, apenas loga sem escrever no Jira |

## Como a Due Date é calculada

1. **JQL gerada**: `(<escopos>) AND status = "<jira_status_in_progress>" AND duedate is EMPTY AND "Story Points" is not EMPTY`.
2. **Fase 1**: para cada issue retornada, lê o primeiro customfield com SP populado (na ordem informada em `jira_story_points_field`), busca o `SP → dias` no mapa, e calcula `data_candidata = hoje + N dias úteis` (segunda a sexta).
3. **Fase 2**: para subtarefas referenciadas que não estão entre as candidatas, faz uma busca extra para obter o `duedate` atual delas.
4. **Fase 3**: se a issue tem subtarefas (candidatas desta execução ou externas com `duedate`), o `Due Date` final é o **maior** entre todas as datas das subtarefas; caso contrário, usa a `data_candidata`.

## Logs

A action loga, por linha, o que será (ou foi) feito por issue:

```
[update] SHS-123 -> 2025-12-08 | SP=3 (customfield_11030) +3d uteis | Implementa X
[dry-run] B30-456 -> 2025-12-15 | max de 2 subtarefa(s); maior: B30-457=2025-12-15 | Refactor Y
[skip] SHS-789: SP=13 nao mapeado em story_points_to_business_days.
```

Encerra com:

```
[done] atualizados=N ignorados=M falhas=F dry_run=true|false
```

Se houver falhas (`failed > 0`), o step encerra com exit code 1.
