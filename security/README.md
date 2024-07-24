# Security

Essa action se integra ao Sonar para a analise de código e envio das métricas a ferramenta



## Configuração
1. A secret SONAR_FOUNDATION_TOKEN foi criada a nivel de organização, caso tenha problema para acessar do seu repositório, favor contatar o time de SRE
2. É necessário a adequação do campo BRANCHES caso seu gitflow não utilize as branches pré configuradas


3. Dentro do seu repositório que usará o workflow, crie o arquivo `.github/workflows/security.yml` com o seguinte conteúdo

```yml
on:
  push:
    branches: [staging, main]
    tags:
      - '[0-9]+.[0-9]+.[0-9]+'
  workflow_dispatch:

jobs:
  Security:
    runs-on: runner-security-${{ github.repository_owner }}
    steps:
      - name: Checkout Automations Repository
        uses: actions/checkout@v4

      - name: Run SonarQube
        uses: iclinic/automations/security@v1
        with:
          sonar_token: ${{ secrets.SONAR_FOUNDATION_TOKEN }}

```