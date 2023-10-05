# Jira Issue Required

Essa action se integra ao Jira para garantir que a branch associada ao Pull Request possua um card. A verificação acontece através do nome da branch que deve conter o código do projeto e sua identificação. Exemplos:
- find-1
- feature/find-1
- bugfix/find-1
- hotfix/find-1



## Configuração
1. Nos secrets do repositório que estiver usando configure três variáveis:
- JIRA_BASE_URL: Com o valor `https://<sua org>.atlassian.net`;
- JIRA_USER_EMAIL: Um email de um user que acesso ao Jira para validar se o card exista;
- JIRA_API_TOKEN: Token criado para este fim deve ser criado em [API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens);
