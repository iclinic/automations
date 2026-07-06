# Jira Issue Required

Essa action se integra ao Jira para garantir que a branch associada ao Pull Request possua um card. A verificação acontece através do nome da branch (ou título do PR) que deve conter o código do projeto e sua identificação. Exemplos:
- find-1
- feature/find-1
- bugfix/find-1
- hotfix/find-1



## Configuração
1. Veja se as settings abaixo que estão no nível de organização foram herdadas para seu repositório, caso contrário, inclua seu repositório aqui nos `Repository access` de cada uma delas em https://github.com/organizations/iclinic/settings/secrets/actions/<SECRET_NAME>
- JIRA_BASE_URL: Com o valor `https://<sua org>.atlassian.net`;
- JIRA_USER_EMAIL: Um email de um user que acesso ao Jira para validar se o card exista;
- JIRA_API_TOKEN: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens);
- DEFAULT_HOTFIX_PREFIX: Prefixo padrão para branches de Hotfix isentas de seguir o fluxo completo em prol da resolução rápida em produção. Valor padrão: `hotfix/`
- JIRA_STATUS_ALLOWED_TO_MERGE: Status do Jira que permitirá o merge. **Obrigatório** (sem default), ex.: `To Deployment`. Recomenda-se configurá-lo como *variable* de organização/repositório.

2. Em alguns casos, um repositório tem relação com Jira em organizações diferentes, e para resolver esse problema foi criada uma configuração alternativa de Jira:
- JIRA_ALTERNATIVE_BASE_URL: Com o valor `https://<sua org>.atlassian.net`;
- JIRA_ALTERNATIVE_USER_EMAIL: Um email de um user que acesso ao Jira para validar se o card exista;
- JIRA_ALTERNATIVE_API_TOKEN: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens);

As três variáveis `JIRA_ALTERNATIVE_*` são de preenchimento opcional.

3. Dentro do seu repositório que usará o workflow, crie o arquivo `.github/workflows/jira-issue-required.yml` com o seguinte conteúdo
```yml
on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

  push:
    branches:
      - '**'
      - '!main'

jobs:
  jira_issue_required:
    runs-on: ubuntu-latest
    steps:
      - name: Check Whether PR/commit is associated to a Jira issue
        uses: iclinic/automations/jira-issue-required@v1
        with:
          jira_base_url: ${{ secrets.JIRA_BASE_URL }}
          jira_user_email: ${{ secrets.JIRA_USER_EMAIL }}
          jira_api_token: ${{ secrets.JIRA_API_TOKEN }}
          # Jira alternativo (opcional):
          jira_alternative_base_url: ${{ secrets.JIRA_ALTERNATIVE_BASE_URL }}
          jira_alternative_user_email: ${{ secrets.JIRA_ALTERNATIVE_USER_EMAIL }}
          jira_alternative_api_token: ${{ secrets.JIRA_ALTERNATIVE_API_TOKEN }}
          # Obrigatório (sem default):
          jira_status_allowed_to_merge: ${{ vars.JIRA_STATUS_ALLOWED_TO_MERGE }}
          # Configuração opcional:
          default_hotfix_prefix: ${{ vars.default_hotfix_prefix }}
          default_revert_prefix: ${{ vars.default_revert_prefix }}
          check_jira_valid_project_prefixes: "True"
          jira_valid_project_prefixes: "PROJ1,PROJ2"
          approved_field_for_development_by_field_is_empty: "customfield_11758"
```

## Inputs

| Input | Obrigatório | Default | Descrição |
| --- | --- | --- | --- |
| `jira_base_url` | sim | — | URL base do Jira, ex.: `https://<sua org>.atlassian.net`. |
| `jira_user_email` | sim | — | Email de um usuário com acesso ao Jira. |
| `jira_api_token` | sim | — | Token de API criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens). |
| `jira_status_allowed_to_merge` | sim | — | Status do Jira que libera o merge, ex.: `To Deployment`. Não possui default: se não for informado, o status recebido nunca casará e todos os merges serão bloqueados. |
| `jira_alternative_base_url` | não | — | URL base de um Jira alternativo (organização diferente). |
| `jira_alternative_user_email` | não | — | Email do usuário no Jira alternativo. |
| `jira_alternative_api_token` | não | — | Token de API do Jira alternativo. |
| `possible_issue_reference` | não | nome da branch | String onde a action procura a *issue reference*. Por padrão usa o nome da branch (e o título do PR em eventos de pull request). |
| `default_hotfix_prefix` | não | — | Prefixo de branches de hotfix isentas do check (ex.: `hotfix/`). |
| `default_revert_prefix` | não | — | Prefixo de branches de revert isentas do check (ex.: `revert/`). |
| `check_jira_valid_project_prefixes` | não | `True` | Habilita a validação dos prefixos de projeto (spaces). Valores `false`/`0`/`no`/`off` (case-insensitive) desabilitam; qualquer outro valor mantém habilitado (*fail-secure*). |
| `jira_valid_project_prefixes` | não | — | Prefixos de projetos válidos, separados por vírgula, ex.: `PROJ1,PROJ2`. Obrigatório quando `check_jira_valid_project_prefixes` está habilitado: o card encontrado deve pertencer a um desses spaces. |
| `approved_field_for_development_by_field_is_empty` | não | — | Nome do campo do Jira (ex.: `customfield_11758`) que indica quem aprovou o card para desenvolvimento. Quando informado, o merge só é liberado se o campo não estiver vazio. A validação só ocorre quando o card existe. |

## Outputs

| Output | Descrição |
| --- | --- |
| `issue_key` | A chave do issue detectada na referência (branch/título), ex.: `SHS-491`. |
| `issue_status` | O status do issue retornado pelo Jira, quando encontrado. |

### Exceções ao check (hotfix / revert)

Branches cujo nome começa com `default_hotfix_prefix` ou `default_revert_prefix` são isentas da verificação do card no Jira, permitindo respostas rápidas em produção.

### Validação de prefixos de projeto (SOX)

Quando `check_jira_valid_project_prefixes` está habilitado, o prefixo do card (ex.: `PROJ` em `PROJ-123`) precisa constar em `jira_valid_project_prefixes`. Ao usar o Jira alternativo, informe em `jira_valid_project_prefixes` a **união** dos prefixos válidos das duas instâncias, pois a validação roda em ambos os checks.
