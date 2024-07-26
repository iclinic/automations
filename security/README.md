# Security

Essa action se integra ao Sonar para a analise de código e envio das métricas a ferramenta

## Configuração

1. A secret SONAR_FOUNDATION_TOKEN foi criada a nivel de organização, caso tenha problema para acessar do seu repositório, favor contatar o time de SRE
2. É necessário a adequação do campo BRANCHES caso seu gitflow não utilize as branches pré configuradas
3. Dentro do seu repositório que usará o workflow, crie o arquivo `.github/workflows/security.yml` com o seguinte conteúdo

```yml
on:
  push:
    branches: [main, master, production, staging]
    tags:
      - '[0-9]+.[0-9]+.[0-9]+'
  pull_request:
    branches: [main, master, production, staging]
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

### Projetos Python - Configuração completa com coverage

Projetos Python que usam coverage, precisam fazer as seguintes configurações:

- O job do sonarqube precisa estar no mesmo workflow que roda os testes
- Precisa rodar depois do job de tests (adicionando `needs: [test]`)
- No job do teste, após rodar o pytest ou coverage, fazer o upload do xml gerado usando o upload-artifact
- No job do sonarqube precisa fazer o download do relatório salvo antes
- É necessário adicionar a informação abaixo no arquivo `.coveragerc`

```conf
[run]
relative_files = True
branch = True
```

- Exemplo completo:

```yml
on:
  push:
    branches: [main, master, production, staging]
    tags:
      - '[0-9]+.[0-9]+.[0-9]+'
  pull_request:
    branches: [main, master, production, staging]
  workflow_dispatch:

jobs:
  test:
    steps:
      - run: pytest --cov=apps/ --cov-report=term-missing:skip-covered --cov-report=xml --cov-config=.coveragerc

      - name: Upload code coverage results
        uses: actions/upload-artifact@v4
        with:
          name: code-coverage-report
          path: coverage.xml

  Security:
    runs-on: runner-security-${{ github.repository_owner }}
    needs: [test]
    steps:
      - name: Checkout Automations Repository
        uses: actions/checkout@v4

      - name: Download code coverage results
        uses: actions/download-artifact@v4
        with:
          name: code-coverage-report

      - name: Run SonarQube
        uses: iclinic/automations/security@v1
        with:
          sonar_token: ${{ secrets.SONAR_FOUNDATION_TOKEN }}
          sonar_sources: 'apps'
          sonar_language: 'python'
          sonar_python_version: '3.12'
          sonar_core_coverage_plugin: 'cobertura'
          sonar_python_coverage_reportpaths: 'coverage.xml'
          sonar_tests: 'tests'
          sonar_exclusions: '**/migrations/**,**/apps.py,**/admin.py,**/urls.py,**/*.html,**/healthcheck.py,**/wsgi.py,**/asgi.py'
```
