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

### Projetos Javascript - Configuração completa com coverage no formato .lcov

Projetos Javascript que usam coverage, precisam fazer as seguintes configurações:

- O job do sonarqube precisa coletar o relatório no formato .lcov
- O relatório é gerado após execução dos testes
- Necessário configurar os testes para gerar o relatório no formato .lcov

- Exemplo de configuração de cobertura c/ vitest:
```js
export default defineConfig(({ mode }) => {
  return {
    plugins: [react(), tsconfigPaths()],
    test: {
      environment: 'node',
      globals: true,
      include: ['*/**/*.test.ts'],
      coverage: {
        exclude: [
          '**/middleware.ts',
          '**/instrumentation.ts',
        ],
        include: [
          'apps/webapp/src/**/*.ts',
          'packages/**/src/**/*.ts',
        ],
        provider: 'istanbul',
        reporter: 'lcovonly',
      },
    },
  };
});
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
  test-unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Setup dependencies
        uses: ./.github/actions/setup-node

      - name: Runtime tests
        run: pnpm test:coverage

      - name: Upload coverage report
        uses: actions/upload-artifact@v4
        with:
          name: coverage
          path: coverage/lcov.info
          retention-days: 1

  security:
    needs: [test-unit]
    runs-on: runner-security-${{ github.repository_owner }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Download coverage report
        uses: actions/download-artifact@v4
        with:
          name: coverage

      - name: Get Previous tag
        if: github.event_name == 'push'
        id: previoustag
        uses: WyriHaximus/github-action-get-previous-tag@8a0e045f02c0a3a04e1452df58b90fc7e555e950

      - name: Setup dependencies
        uses: ./.github/actions/setup-node

      - name: Run SonarQube
        uses: iclinic/automations/security@v1
        with:
          sonar_token: ${{ secrets.SONAR_FOUNDATION_TOKEN }}
          sonar_sources: 'apps/webapp,packages'
          sonar_qualitygate_wait: true
          sonar_test_inclusions: '**/*.test.ts,**/*.spec.ts'
          sonar_coverage_exclusions: '**/app/**,**/env/**,**/hooks/**,**/actions/**,**/middleware.ts,**/instrumentation.ts'
          sonar_javascript_coverage_lcov_reportpaths: lcov.info
          project_version: ${{ github.event_name == 'push' && steps.previoustag.outputs.tag || '' }}

```
