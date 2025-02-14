# MOVE JIRA TICKET AFTER MERGE

Essa action se integra ao Jira para garantir que o card não seja associado a múltiplos PRs, desta forma tudo que entrar em produção deverá passar pela esteira completa. Quando o PR for mergeado, o card será automaticamente movido para a coluna seguinte, impedindo demais PR (que fazem uso do jira_issue_required@v2) de fazer o merge.


## Configuração
1. Veja se as settings abaixo que estão no nível de organização foram herdadas para seu repositório, caso contrário, inclua seu repositório aqui nos `Repository access` de cada uma delas em https://github.com/organizations/iclinic/settings/secrets/actions/<SECRET_NAME>
- `JIRA_BASE_URL_MERGE_WORKFLOW`: Com o valor `https://<sua org>.atlassian.net`;
- `JIRA_API_MERGE_USEREMAIL`: Um email de um user que acesso ao Jira para mover o card;
- `JIRA_API_MERGE_TOKEN`: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens), e o valor que deve ser colocado aqui é o `base64(email:api_token)`
- `JIRA_API_MERGE_TRANSITION_ID_SUBTASK_TYPE`: ID da transição no jira para tipos de subtasks (int). Ex: 341
- `JIRA_API_MERGE_TRANSITION_ID_STANDARD_TYPE`: ID da transição no jira para tipos standard (int). Ex: 371
- **Atenção**: Todas as settings acima ficam em variáveis ***exceto*** o token codificado como exemplo e ficar em secrets


3. Dentro do seu repositório que usará o workflow, crie o arquivo `.github/workflows/merge-workflow.yml` com o seguinte conteúdo
```yml
on:
  pull_request:
    types:
      - closed

jobs:
  jira_issue_required:
    runs-on: ubuntu-latest
    steps:
      - name: Move Jira ticket after Merge
        uses: iclinic/automations/move_jira_ticket_after_merge@v1
        with:
          jira_base_url_merge_workflow: ${{ vars.JIRA_BASE_URL_MERGE_WORKFLOW }}
          jira_api_merge_transition_id_subtask_type: ${{ vars.JIRA_API_MERGE_TRANSITION_ID_SUBTASK_TYPE }}
          jira_api_merge_transition_id_standard_type: ${{ vars.JIRA_API_MERGE_TRANSITION_ID_STANDARD_TYPE }}
          jira_api_merge_useremail: ${{ vars.JIRA_API_MERGE_USEREMAIL }}
          jira_api_merge_token: ${{ secrets.JIRA_API_MERGE_TOKEN }}

```
