# Jira Issue Required

Essa action se integra ao Jira para garantir que a branch associada ao Pull Request possua um card. A verificação acontece através do nome da branch que deve conter o código do projeto e sua identificação. Exemplos:
- find-1
- feature/find-1
- bugfix/find-1
- hotfix/find-1



## Configuração
1. Veja se as settings abaixo que estão no nível de organização foram herdadas para seu repositório, caso contrário, inclua seu repositório aqui nos `Repository access` de cada uma delas em https://github.com/organizations/iclinic/settings/secrets/actions/<SECRET_NAME>
- JIRA_BASE_URL: Com o valor `https://<sua org>.atlassian.net`;
- JIRA_USER_EMAIL: Um email de um user que acesso ao Jira para validar se o card exista;
- JIRA_API_TOKEN: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens);

Opcional:
- `possible_issue_reference`: Este parâmetro pode ser usado para sobrescrever a string sob a qual a action procurará a *issue reference*. Por padrão, é o nome da branch.

2. Em alguns casos, um repositório tem relação com Jira em organizações diferentes, e para resolver esse problema foi criada uma configuração alternativa de Jira:
- JIRA_ALTERNATIVE_BASE_URL: Com o valor `https://<sua org>.atlassian.net`;
- JIRA_ALTERNATIVE_USER_EMAIL: Um email de um user que acesso ao Jira para validar se o card exista;
- JIRA_ALTERNATIVE_API_TOKEN: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens);

Essas variáveis são de preenchimento opcional.

3. Dentro do seu repositório que usará o workflow, crie o arquivo `.github/workflows/jira-issue-required.yml` com o seguinte conteúdo
```yml
on:
  pull_request:
    types:
      - opened

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
          # configuração opcional:
          jira_alternative_base_url: ${{ secrets.JIRA_ALTERNATIVE_BASE_URL }}
          jira_alternative_user_email: ${{ secrets.JIRA_ALTERNATIVE_USER_EMAIL }}
          jira_alternative_api_token: ${{ secrets.JIRA_ALTERNATIVE_API_TOKEN }}
          default_hotfix_prefix: ${{ vars.DEFAULT_HOTFIX_PREFIX }}
```
