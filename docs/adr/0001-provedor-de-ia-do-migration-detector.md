# ADR 0001 — Classificação de impacto do migration-detector após a retirada do GitHub Models

- Status: proposta
- Data: 2026-08-27
- Revisão: 2026-08-28 — escopo ampliado para as três stacks consumidoras; decisão trocada de "provedor pago" (A4/A6/A8) para "classificador determinístico" (A3), com base na medição registrada abaixo
- Revisão: 2026-09-02 — revisão de @almeidaraphael no PR #40: corrigido o caminho de publicação (a `@v4` já existia e apontava para trabalho não relacionado), o fator 3 deixou de sustentar a decisão, declaradas as regras de agregação de severidade e a leitura de `SeparateDatabaseAndState`, `RunPython` deixou de ser severidade fixa, e a conferência da amostra virou condição de aceite
- Decisores: time de plataforma (dono do repo `iclinic/automations`), time de dados (consumidor do alerta)
- Componente afetado: `migration-detector/`
- Repositórios consumidores: os três serviços com banco que hoje chamam a action (um Django/MySQL, um Alembic/PostgreSQL, um TypeORM/PostgreSQL)
- Substitui: a escolha de provedor registrada em `migration-detector/README.md` (seção "Provedor de IA")

## Contexto

A action `migration-detector` detecta arquivos de migração em PRs, classifica o impacto em `safe`, `controlled` ou `breaking` e avisa o time de dados no Slack. O provedor de IA padrão era a GitHub Models API em `https://models.inference.ai.azure.com`, autenticada com o próprio `GITHUB_TOKEN` e a permissão `models: read`. Não havia secret extra nem custo.

Esse provedor não existe mais.

### Evidência 1 — o job em produção

Log do job no consumidor Django, de 2026-08-11 (arquivo `job-logs.txt` na raiz deste repo):

```
==> Provedor de IA: GitHub Models API
==> URL: https://models.inference.ai.azure.com  |  Modelo: gpt-4o-mini
[WARN] Tentativa 1 falhou: Error code: 404. Retentando...
[WARN] Tentativa 2 falhou: Error code: 404. Retentando...
[ERRO] Falha na análise com IA: Error code: 404
```

As três tentativas falharam com 404. A permissão estava correta (`Models: read` aparece no bloco de permissões do log), então não é problema de autorização, que retornaria 401 ou 403.

### Evidência 2 — sondagem dos dois endpoints (2026-08-27)

```
$ curl -X POST https://models.inference.ai.azure.com/chat/completions ...
000                                    # host não resolve

$ curl -X POST https://models.github.ai/inference/chat/completions ...
410 {"error":{"code":"github_models_retirement_brownout",
              "message":"GitHub Models is temporarily unavailable as part of a scheduled retirement brownout."}}

$ curl https://models.github.ai/catalog/models
410
```

O host antigo saiu do ar. O host sucessor responde 410 Gone com um código de erro que nomeia a própria retirada. Inferência e catálogo, os dois.

### Evidência 3 — o anúncio

O changelog do GitHub de 2026-07-30 diz que o GitHub Models foi retirado por completo: playground, catálogo de modelos, API de inferência e BYOK, para todos os clientes, incluindo quem tinha uso ativo. O endpoint Azure já havia sido descontinuado em 2025-07-17, com suporte removido em 2025-10-17. As alternativas que o GitHub indica são o Microsoft Foundry, para acesso a modelos, e o GitHub Copilot, para workflows de IA dentro do GitHub.

Conclusão: não sobrou nenhum endpoint de inferência hospedado pelo GitHub. Trocar a URL não resolve.

### O segundo problema, no mesmo log

A falha da IA não derruba nada. `make_fallback_result` em [classify.py](../../migration-detector/classify.py) devolve `has_db_change: true`, `highest_severity: controlled`, `confidence: 0.0` e a razão "Análise automática falhou — revisão manual necessária". O step termina com `outcome=success`, o Slack recebe HTTP 200 e o job fica verde.

O efeito prático: desde 30/07 todo PR com migração gera a mesma mensagem amarela de "Mudança Controlada" no canal, sem nenhuma informação sobre o que mudou. Um `DROP COLUMN` de verdade chega ao canal com a mesma cor de um `ADD COLUMN` opcional. E como o job passa, ninguém foi avisado: a falha rodou por cerca de quatro semanas antes de ser investigada.

Qualquer alternativa escolhida abaixo precisa resolver os dois problemas. O provedor morto é a causa; o fallback silencioso é o motivo de ter demorado tanto para aparecer.

## Escopo

Um `grep` por provedores de IA em `*.yml`, `*.py` e `*.md` deste repo encontra `openai` apenas em `migration-detector/`. As outras oito automações do repo não usam IA. O raio de alcance da decisão é uma action.

Do lado consumidor, são três repositórios, com três stacks de migração diferentes:

| Repositório | Stack | Banco | Onde ficam as migrações | Arquivos hoje |
|---|---|---|---|---|
| Consumidor Django | Django (2.2) | MySQL | `django/app/*/migrations/*.py` | 591 |
| Consumidor Alembic | Alembic + SQLAlchemy (FastAPI) | PostgreSQL | `app/alembic/versions/*.py` | 13 |
| Consumidor TypeORM | TypeORM 0.2 | PostgreSQL | `migrations/*.ts` | 34 |

Os três chamam `iclinic/automations/migration-detector@v3` com workflows idênticos — `github_token`, `slack_webhook_url` e `slack_channel`, e nada mais. Nenhum dos três passa `ai_api_url`, `ai_api_key` ou `migration_paths`. Consequência que pesa na decisão: uma alternativa que exija input novo obrigatório precisa que alguém edite o bloco `with:` dos três workflows; uma que se resolva dentro do `action.yml` só precisa que a tag mude. Os dois casos custam três PRs — ver "Publicação e versionamento", abaixo —, mas o segundo é uma linha por repo e não arrasta secret de organização junto.

Detalhe relevante para o esforço: `call_ai` não tem teste. Das 40 asserções em `tests/test_classify.py`, nenhuma exercita a chamada HTTP; o único vínculo com a OpenAI é uma docstring e um helper de mock não usado. Trocar de SDK, ou remover o SDK, não quebra a suíte.

### Evidência 4 — o video-conference nunca foi coberto

O default de `migration_paths` no [action.yml](../../migration-detector/action.yml) é:

```
**/*.sql,**/migrations/*.py,**/alembic/versions/*.py,**/*AutoMigrate.ts,**/*automigrate.ts
```

Os arquivos do consumidor TypeORM vivem em `migrations/*.ts`. O glob `**/migrations/*.py` não os pega, porque a extensão é `.ts`. Sobra `**/*AutoMigrate.ts`, que casa apenas com os arquivos gerados pelo `typeorm migration:generate -n AutoMigrate`. Contagem no repo: 23 dos 34 casam; 11 não casam.

Os 11 que escapam:

```
1638505769114-AlterScheduleAddColumns.ts        1654840673329-AlterAddColumnCreateAt.ts
1639668169422-AddTableVersionControl.ts         1658787237018-AlterAddColumnDescription.ts
1642539651840-AddTableScore.ts                  1690314330495-AddColumnCleanedAt.ts
1647407813130-AddRecordUrlVideo.ts              1698075718143-CreateTableConferenceTranscription.ts
1651511856366-AddTableConferenceFiles.ts
1652715387257-AddTableModifyVersion.ts
1653671782546-AddTableVideoConferenceLogs.ts
```

São exatamente as 11 migrações mais recentes do repositório, todas de dezembro de 2021 em diante, ou seja, todas as que foram escritas à mão em vez de geradas. Na prática, o consumidor TypeORM está fora do detector desde que o time parou de usar o nome `AutoMigrate`, e o alerta nunca disparou para nenhuma delas. Isso é anterior e independente da queda do provedor de IA.

Vale registrar também que a conversão de glob para regex no step `Collect` é feita com três `sed` encadeados e trata só `*`, `**` e ponto literal. Ela não trata `?`, `[]` nem chaves. É frágil, mas suficiente para os padrões atuais.

### Publicação e versionamento

Como a versão nova chega nos três consumidores é parte da decisão, e não é o que a versão anterior desta ADR supunha.

As tags deste repositório valem para o repositório inteiro, não por action. O estado no remoto:

| Tag | Commit | Data |
|---|---|---|
| `v3` | 979e965 | 2026-06-08 |
| `v3.4` | 0932855 | 2026-06-22 |
| `v4`, `v4.0`, `v4.0.0` | b586b35 | 2026-07-06 |

Mover major e minor é a convenção do repo, e é automatizada: [update-semver-tags-on-release.yml](../../.github/workflows/update-semver-tags-on-release.yml) roda em `release: published`, faz `git tag -f` na major e na minor derivadas do nome da release, e dá `git push -f --tags`.

A `v3` ter ficado para trás não é política, é um passo pulado: existe Release publicada para a `v3.3.0` e para a `v4.0.0`, e nenhuma para a `v3.4`. Sem Release, a automação não roda, e a `v3` continuou no commit da `v3.3.0`.

Isso fecha duas saídas e abre uma:

- **Subir na linha `v3.x`.** Alcançaria os três consumidores sem PR nenhum, porque eles pinam `@v3`. Descartada: uma `v3.5.0` cortada da main faria a tag `v3` saltar por cima da `v3.4` e da `v4.0.0` e, como a tag é do repositório inteiro e o repo é público, isso trocaria o código embaixo de qualquer consumidor — interno ou não — que tenha pinado qualquer uma das outras oito automações em `@v3`.
- **Subir como `v4.1.0`.** Não alcança ninguém sem PR, e subestima a mudança.
- **Subir como `v5.0.0`.** Escolhida. `highest_severity` ganha o valor `unknown`, que é valor novo num output em que consumidor pode ramificar — a pergunta aberta 4 cogita exatamente isso —, e `minimum_confidence` vira inerte sem aviso. É quebra de contrato, e major é o lugar dela. A Release move `v5` e `v5.0` pela automação.

Custo: três PRs de uma linha, trocando `@v3` por `@v5` nos três workflows. Nenhuma alternativa desta ADR escapa desse custo, então ele não separa a A3 das demais — ver o fator 3.

## Fatores de decisão

1. Restaurar a classificação por severidade. Sem ela o alerta virou ruído.
2. Falhar de forma visível. Job verde sem sinal não pode acontecer de novo.
3. Custo de mudança nos repos consumidores. A action é composite e consumida por três repos; mexer em inputs obrigatórios significa abrir PR em cada workflow que a chama. Ressalva registrada em "Publicação e versionamento": os três pinam `@v3`, e qualquer alternativa que suba exige um PR de uma linha em cada um só para trocar a tag. Esse piso é comum a todas as alternativas e por isso não separa nenhuma delas. O que este fator pesa é o custo *adicional* de input obrigatório novo.
4. Gestão de secrets. Hoje são zero secrets de IA. Qualquer provedor externo passa a exigir um secret de organização.
5. Custo de execução.
6. Dependência de fornecedor. Foi um produto gratuito descontinuado que causou isto. Vale evitar a repetição.
7. Auditabilidade. O repo já concentra automações ligadas a controles SOX. Uma classificação determinística é mais fácil de defender numa auditoria do que a opinião de um modelo. Hoje o alerta é informativo e não bloqueia merge, então o peso deste fator é médio.
8. Cobertura das três stacks. Uma solução que resolva só Django deixa dois repos com o mesmo problema.

## Alternativas consideradas

### A0 — Manter o fallback como está

Não fazer nada. Esforço zero. O canal segue recebendo uma mensagem amarela genérica por PR e o time de dados perde a triagem. Uma breaking change real passa sem sinal. Rejeitada.

### A1 — Repointar para `models.github.ai` com IDs namespaced

Era a migração oficial anunciada em 2025: trocar a base URL para `https://models.github.ai/inference` e o modelo para `openai/gpt-4o-mini`. É o primeiro palpite de quem lê a documentação antiga ou os posts de 2025, e está registrada aqui para ninguém gastar tempo nela: a sondagem da Evidência 2 devolve 410 `github_models_retirement_brownout` neste endpoint hoje. Rejeitada por evidência direta.

### A2 — Remover a IA e notificar sem severidade

Apagar o step de análise e mandar ao Slack só "esse PR toca migração, revisem". Esforço baixo: `classify.py` encolhe, `action.yml` perde quatro inputs. Custo zero, dependência externa zero, nenhum secret. Perde a triagem: o time de dados volta a abrir todo PR com migração na mão. A action continua útil como roteador de aviso.

### A3 — Classificador determinístico, sem IA

Ler as operações do arquivo em vez de pedir opinião a um modelo. Esta alternativa foi prototipada e medida contra o corpus real dos três repositórios antes de ser escolhida. O resultado está na seção "Viabilidade da A3", abaixo.

### A4 — Anthropic Claude direto

`claude-haiku-4-5` custa US$ 1 por milhão de tokens de entrada e US$ 5 de saída. O volume desta action é pequeno: os quatro arquivos do o PR do log somam 9,3 KB, algo perto de 3.200 tokens de entrada e 400 de saída, ou cerca de US$ 0,005 por PR. A cem PRs com migração por mês, menos de US$ 1.

Exige um secret `ANTHROPIC_API_KEY` na organização e uma conta com billing. O código muda: o caminho suportado é o SDK `anthropic`, não um shim compatível com a OpenAI, então `call_ai` é reescrito. Como `call_ai` não tem teste, a suíte não quebra.

### A5 — OpenAI direto

Apontar `ai_api_url` para `https://api.openai.com/v1`, preencher `ai_api_key` e manter `gpt-4o-mini`. Zero linha de código alterada: é exatamente o caminho de "hub externo" que `resolve_credentials` já implementa. Exige secret e billing.

Risco a considerar: `gpt-4o-mini` é um modelo antigo, e as chamadas atuais passam `temperature` e `max_tokens`, parâmetros que os modelos de raciocínio mais novos da OpenAI restringem ou rejeitam. A troca é grátis hoje, mas a próxima atualização de modelo não é. Confirmar a tabela de preços vigente antes de fechar.

### A6 — Azure OpenAI / Microsoft Foundry

É a recomendação do próprio GitHub. Contrato empresarial, residência de dados e trilha de auditoria no tenant. O Foundry também oferece Claude, cobrado via Microsoft Marketplace pelos mesmos preços da API de primeira mão.

O código muda pouco mas muda: o Azure exige `api-version` e nome de deployment, o que troca o client `OpenAI` por `AzureOpenAI`. Depende de a Afya já ter subscription e de alguém provisionar o deployment, então é a alternativa mais lenta para colocar de pé. É a melhor em governança.

### A7 — AWS Bedrock ou Google Vertex

Cabe se a nuvem da organização for AWS ou GCP. A vantagem de segurança é concreta: autenticação por OIDC, sem secret de longa duração no GitHub. O custo é o esforço: SDK diferente, mais uma role IAM e uma relação de confiança OIDC por repo consumidor — três, neste caso.

### A8 — Gateway de IA interno

A action já suporta isto. Preencher `ai_api_url` e `ai_api_key` com um gateway corporativo compatível com o protocolo OpenAI resolve sem tocar em uma linha de código, porque é para isso que o seam de provedor existe. Um secret, controle central de custo e um único ponto para trocar de modelo depois, sem novo PR neste repo.

Depende de esse gateway existir na Afya.

### A9 — Caminho GitHub-native via Copilot

O GitHub indica o Copilot para workflows de IA na plataforma. Investigada e descartada por forma: não há endpoint de inferência chamável de dentro de uma composite action equivalente ao antigo `models: read` com API compatível com OpenAI. As superfícies do Copilot são o agente de codificação e o editor, não uma API de classificação por PR.

Registro colateral: a action `actions/ai-inference` quebrou pelo mesmo motivo. Vale varrer a organização por workflows que a usem, porque eles estão fora do ar do mesmo jeito.

### A10 — Híbrido: determinístico primeiro, IA no ambíguo

A3 nas operações inequívocas, e chamada ao modelo só no resíduo. Depende de A3 existir, então só faz sentido como fase seguinte.

## Viabilidade da A3

A pergunta que decide esta ADR é se um único módulo no `automations` consegue classificar as três stacks sem chamar modelo. Para responder com número em vez de opinião, foi escrito um protótipo de ~200 linhas e rodado sobre todos os arquivos de migração dos três repositórios.

### Como as três stacks convergem

As três parecem diferentes na superfície e convergem em dois pontos.

O primeiro é que Django e Alembic são Python, e a lista de operações de ambos é declarativa e legível com `ast`, sem importar Django, sem importar SQLAlchemy e sem banco. Em Django é a lista `operations` da classe `Migration`; em Alembic é o corpo da função `upgrade()`. Duas funções de parsing, mesma biblioteca padrão.

O segundo é que todo o resto vira SQL. O TypeORM do consumidor TypeORM não usa a API de schema builder: os 34 arquivos são 100% `queryRunner.query()` com DDL literal dentro de template string, 133 statements no total. O `migrations.RunSQL` do Django (57 ocorrências) e o `op.execute()` do Alembic caem no mesmo lugar. Então o classificador de SQL não é um parser a mais para o TypeScript: é o núcleo compartilhado pelas três stacks, e o front-end de TypeScript é uma regex que extrai template literals do corpo de `up()`.

A diferença real entre os três não é a linguagem, é o dialeto. O consumidor Django é MySQL e escreve `ALTER TABLE x MODIFY col BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE`. Os outros dois são PostgreSQL e escrevem `ALTER TABLE x ALTER COLUMN col SET NOT NULL` e `ALTER COLUMN col TYPE`. O classificador precisa das duas gramáticas, o que são duas famílias de regex, não dois parsers.

### O `AlterField` é resolúvel, ao contrário do que esta ADR dizia antes

A versão anterior deste documento afirmava que `AlterField` no Django e `alter_column` no Alembic carregam só o estado final do campo e que distinguir "varchar de 50 para 100" de "varchar para int" exigiria o histórico de migrações ou o diff do modelo. A primeira metade está certa. A segunda ignora que o histórico de migrações está no checkout: os workflows dos três repos usam `actions/checkout@v4` com `fetch-depth: 0`, e o diretório `migrations/` inteiro está em disco quando a action roda.

O estado anterior de um campo é o último `CreateModel`, `AddField` ou `AlterField` que mencionou aquele par (modelo, campo) antes da migração atual, dentro do mesmo app. Isso é indexável com `ast` em uma passada por diretório.

Medição sobre as 248 ocorrências de `AlterField` do consumidor Django:

| Resultado | Ocorrências |
|---|---|
| estado anterior localizado no grafo | 243 (98,0%) |
| sem estado anterior — campo herdado de app externo ou do estado base | 5 (2,0%) |

E a classificação que sai da comparação:

| Severidade | Ocorrências | Regra que disparou |
|---|---|---|
| `none` | 140 (56,5%) | só `choices`, `verbose_name`, `default`, `help_text` — sem DDL |
| `controlled` | 75 (30,2%) | `max_length` aumentou, `null` False→True, índice alterado |
| `breaking` | 28 (11,3%) | classe do campo mudou, `null` True→False, `unique` False→True |
| `unknown` | 5 (2,0%) | sem estado anterior |

Vale olhar duas linhas dessa saída:

```
ledger/0042_modify_supplier_external_id_to_non_null.py         breaking  campo passou a NOT NULL
ledger/0043_modify_supplierservice_external_id_to_non_null.py breaking  campo passou a NOT NULL
ml_gateway/0005_auto_20260115_0900.py                        breaking  tipo do campo mudou: ForeignKey -> UUIDField
```

As duas primeiras são a família do PR que aparece no log da Evidência 1. A versão anterior desta ADR usava esse PR como argumento contra a A3 — "o PR do log é justamente um caso desses". É o contrário: o classificador determinístico acerta esse caso duas vezes, por dois caminhos independentes. Pelo estado anterior reconstruído, como acima. E pelo SQL, porque essas migrações usam `SeparateDatabaseAndState` e o `database_operations` carrega o DDL literal `ALTER TABLE ledger_supplierservice MODIFY external_id BIGINT NOT NULL`. O que o modelo errou por estar fora do ar, uma regex acerta lendo a string.

### Como `SeparateDatabaseAndState` é lido

A família `modify_*_external_id_to_non_null` que a Evidência 1 usa como caso de prova são oito arquivos no consumidor Django. Todos os oito envolvem `SeparateDatabaseAndState`. Seis carregam o DDL literal em `RunSQL`, dentro de `database_operations`. Os outros dois carregam um `RunPython` que monta o `ALTER TABLE` com f-string, na forma:

```python
cursor.execute(
    f"ALTER TABLE ledger_supplierservice "
    f"MODIFY external_id BIGINT {null_clause}, ALGORITHM=INPLACE, LOCK=NONE"
)
```

E os oito carregam um `AlterField` dentro de `state_operations`.

Isso precisa estar escrito porque é onde a decisão se apoia. `state_operations` por construção não emite DDL: existe só para sincronizar o state do Django com uma mudança feita à mão. Um classificador que raciocine "`state_operations` → sem DDL → `none`" está seguindo a semântica do Django corretamente — e nesse caminho os dois arquivos com `RunPython` caem para `controlled`, e o acerto da Evidência 1 vira 6 de 8 em vez de 8 de 8.

A regra adotada é a outra: os dois blocos de um `SeparateDatabaseAndState` são lidos, `state_operations` como declaração da intenção de schema e `database_operations` como o DDL efetivo, e o arquivo fica com a máxima das duas. Nos dois arquivos com `RunPython` isso dá `max(AlterField null True→False = breaking, RunPython = controlled) = breaking`, que é a resposta certa.

### `RunPython` não é uma severidade fixa

O protótipo classificava todo `RunPython` como `controlled`, por ser mudança de dados sem DDL. A contagem no consumidor Django mostra o tamanho do que isso decide por constante: `RunPython` aparece em 65 dos 591 arquivos, e em 44 deles é a única operação — 7,4% do corpus, e cerca de um quarto dos 159 `controlled` daquele repositório.

Além do tamanho, a premissa "sem DDL" é falsa em pelo menos dois arquivos do próprio corpus: são justamente os dois da subseção anterior, que montam `ALTER TABLE ... MODIFY ... NOT NULL` com f-string dentro do corpo do `RunPython`.

A regra adotada: o corpo do `RunPython` é varrido por strings com cara de DDL — literais e f-strings — e o que sair vai para o `sql.py`, que já existe no desenho por causa das três stacks. `controlled` fica sendo o piso para as migrações que são de dados de verdade, não a resposta para todas. String de DDL que não seja parseável dentro do corpo cai em `unknown` pela regra de piso.

### Cobertura medida nas três stacks

Rodando o protótipo completo, com resolução de `AlterField`, sobre todo o corpus:

| Stack | Repositório | Arquivos | `none` | `safe` | `controlled` | `breaking` | `unknown` | Determinístico |
|---|---|---|---|---|---|---|---|---|
| Django | consumidor Django | 591 | 94 | 273 | 159 | 45 | 20 | 571 (96,6%) |
| Alembic | consumidor Alembic | 13 | 0 | 11 | 0 | 2 | 0 | 13 (100%) |
| TypeORM / SQL | consumidor TypeORM | 34 | 0 | 10 | 15 | 8 | 1 | 33 (97,1%) |
| **Total** | | **638** | 94 | 294 | 174 | 55 | 21 | **617 (96,7%)** |

"Determinístico" aqui quer dizer que o classificador produziu uma severidade em vez de `unknown`. É cobertura, não acurácia: os casos foram conferidos por amostragem, não um a um. Uma revisão manual de uma amostra pelo time de dados é o passo que valida a tabela de regras antes de subir.

O resíduo de 21 arquivos se concentra em quatro padrões:

- `AlterUniqueTogether` (18 no Django) — a operação carrega o conjunto final de constraints, e distinguir "adicionou uma unique" de "removeu uma" tem a mesma forma do problema do `AlterField`; é resolúvel pelo mesmo mecanismo, apenas não foi implementado no protótipo;
- `AlterField` sem estado anterior no grafo (5);
- `RunSQL` com SQL montado dinamicamente, não literal (1);
- um `ALTER TYPE` no TypeORM que não é `ADD VALUE` (1).

Nenhum desses vira classificação errada. Todos viram `unknown`, que é o caminho de falha visível.

### O que muda no `automations`

O protótipo está em `/tmp` e não é entregável, mas o desenho que ele valida é este:

```
migration-detector/
  detect/
    __init__.py      dispatch por extensão e caminho
    django.py        ast sobre a lista `operations`
    alembic.py       ast sobre o corpo de `upgrade()`
    typeorm.py       extrai template literals de `up()` e delega
    sql.py           classificador de DDL, MySQL + PostgreSQL (núcleo comum)
    history.py       reconstrói estado anterior de campo pelo grafo de migrações
    severity.py      tabela de regras e ordenação de severidade
  classify.py        entrypoint: hoje chama a IA, passa a chamar detect/
```

Ordem de grandeza: 500 a 700 linhas mais testes. O `requirements.txt` fica vazio — sai o SDK `openai`, não entra nada, e os steps de `pip install` e cache saem junto. A suíte atual não quebra, porque nenhuma das 40 asserções toca a chamada HTTP.

Do lado dos consumidores, os inputs não mudam. Os três workflows passam `github_token`, `slack_webhook_url` e `slack_channel`, e continuam passando exatamente isso. Muda só a tag: um PR de uma linha por repo, pelo motivo em "Publicação e versionamento". Zero secret novo.

O único ajuste no `action.yml` é o default de `migration_paths`, que precisa ganhar `**/migrations/*.ts` para fechar o buraco da Evidência 4. Isso também é interno à action.

## Comparação

| Alternativa | Esforço | Secret novo | PR nos 3 repos | Custo/mês | Dependência externa | Cobre as 3 stacks | Restaura severidade |
|---|---|---|---|---|---|---|---|
| A0 fallback atual | nenhum | não | nenhum (nada sobe) | 0 | morta | não | não |
| A1 `models.github.ai` | baixo | não | 1 linha (tag) | 0 | morta (410) | não | não |
| A2 sem IA | baixo | não | 1 linha (tag) | 0 | nenhuma | n/a | não |
| A3 determinístico | alto | não | 1 linha (tag) | 0 | nenhuma | sim (96,7%) | sim |
| A4 Anthropic | baixo | sim | tag + bloco `with:` | < US$ 1 | um fornecedor | sim | sim |
| A5 OpenAI | mínimo | sim | tag + bloco `with:` | < US$ 1 | um fornecedor | sim | sim |
| A6 Azure / Foundry | médio | sim | tag + bloco `with:` | < US$ 1 | contrato existente | sim | sim |
| A7 Bedrock / Vertex | médio-alto | não (OIDC) | tag + `with:` + OIDC 3× | < US$ 1 | contrato existente | sim | sim |
| A8 gateway interno | mínimo | sim | tag + bloco `with:` | interno | interna | sim | sim |
| A10 híbrido | alto | sim | tag + bloco `with:` | centavos | parcial | sim | sim |

Custo não é critério de decisão aqui. A diferença entre a opção mais cara e a mais barata da tabela é menor que um dólar por mês. A coluna de PRs também parou de separar: toda alternativa que suba precisa de um PR por consumidor só para trocar a tag, pelo motivo em "Publicação e versionamento". O que separa as alternativas é esforço, governança e dependência.

## Decisão

Adotar a A3, o classificador determinístico, para as três stacks.

O que sustenta a troca em relação à versão anterior desta ADR, que apontava para um provedor pago: a objeção que empurrava a A3 para uma fase 2 condicional era a cobertura, e a cobertura foi medida em 96,7% sobre 638 arquivos reais dos três repositórios. O caso concreto que a ADR citava como fora do alcance determinístico — a família `modify_external_id_to_non_null` — é classificado corretamente como `breaking`. Seis dos oito arquivos dessa família chegam lá por duas vias independentes, porque carregam `RunSQL` com o DDL literal. Os outros dois montam o `ALTER TABLE` com f-string dentro de um `RunPython`: chegam pela reconstrução de estado e também pelo `sql.py`, depois da regra registrada em "`RunPython` não é uma severidade fixa". Nenhum dos oito depende de via única. A objeção não se sustenta.

A A3 também é a única alternativa da tabela que atende os fatores 4, 6, 7 e 8 ao mesmo tempo: nenhum secret, nenhum fornecedor, classificação auditável, e as três stacks cobertas pelo mesmo módulo. O fator 3 saiu do argumento: como toda alternativa que suba exige trocar a tag nos três workflows, ele não separa mais a A3 da A5. A A3 continua ganhando, por outro motivo — o PR dela é uma linha e não vem acompanhado de um secret de organização com billing.

### O que entra junto, e não é negociável

Falhar alto. Sai o disfarce de `controlled` com `confidence: 0.0`. O que entra:

- severidade `unknown`, distinta e visível, para o resíduo que o classificador não resolve e para qualquer erro de parsing. A mensagem no Slack diz que aquele arquivo precisa de olho humano, com o nome da operação que não foi entendida;
- **agregação por máxima, declarada.** A severidade de um arquivo é a maior entre as severidades das operações dele — nunca a mais comum, nunca a da última operação lida. Vale dentro de cada arquivo e entre arquivos, no `highest_severity`. Isto não é detalhe de implementação: é o que faz os dois arquivos com `RunPython` da família da Evidência 1 saírem `breaking` em vez de `controlled`, e é o que torna seguro ter um piso `controlled` para `RunPython` de dados;
- **`unknown` é piso, não override.** Operação não reconhecida dentro de um arquivo impede que ele saia `none` ou `safe`, e o rebaixa para `unknown`. Mas não derruba severidade que outra operação do mesmo arquivo já estabeleceu: um arquivo com um `DROP COLUMN` e uma operação não parseada sai `breaking`, não `unknown`. Perder o vermelho para dizer "precisa de olho humano" seria trocar um sinal forte por um fraco;
- erro de execução do próprio classificador derruba o step. Se o módulo lança exceção, o job fica vermelho;
- **o step `Collect` deixa de ter caminho verde silencioso.** Hoje ele sai com `exit 0` em quatro pontos ([action.yml](../../migration-detector/action.yml), linhas 97, 110, 147 e 165). Três escrevem `has_files=false` e são legítimos. O quarto, quando `slack_webhook_url` está vazia, sai sem escrever `has_files` nenhum: os steps com `if: has_files == 'true'` pulam, o step 4 com `if: has_files == 'false'` também pula, e o `Consolidate` cai no default `${HIGHEST_SEVERITY:-none}`. O job fecha verde sem ter classificado nada e sem nem registrar que não havia arquivos. Esse caminho passa a escrever `has_files` explicitamente e a emitir `::warning::`. Só com isso a frase "não existe caminho em que o job passa verde sem ter classificado nada" passa a ser verdade;
- **migração que não casa glob nenhum vira aviso, não silêncio.** Arquivo no diff com cara de migração — sob um diretório `migrations/` ou `alembic/versions/`, ou com nome prefixado por timestamp — que não case nenhum padrão de `migration_paths` gera `::warning::` e entra na mensagem do Slack. Corrigir o default para incluir `**/migrations/*.ts` fecha o caso do consumidor TypeORM; esta regra fecha a classe. A Evidência 4 rodou por quatro anos porque `has_files=false` por glob errado e `has_files=false` por não haver migração são indistinguíveis hoje. Isto responde, do lado do detector, metade da pergunta aberta 5;
- o campo `confidence` perde sentido num classificador determinístico. Ou sai dos outputs, ou fica fixo em `1.0` para as severidades resolvidas e `0.0` para `unknown`, mantendo compatibilidade com quem lê o output. `minimum_confidence` vira input inerte e deve ser marcado como deprecado no `action.yml`. Vale registrar o que sai junto: `apply_confidence_threshold` ([classify.py:167](../../migration-detector/classify.py)) hoje promove `safe` para `controlled` quando a confiança fica abaixo do limiar, e é o único hedge que existe contra um `safe` errado. Ele morre com o `confidence`, e são as duas regras de agregação acima que ocupam o lugar dele. Registrando também, porque é fácil errar a leitura: um `safe` errado não é ausência de aviso — o step do Slack gateia em `has_files`, não em severidade, então sai mensagem 🟢. É pior mesmo assim, porque verde é feito para ser passado batido.

### Ordem de execução

1. Corrigir `migration_paths` para incluir `**/migrations/*.ts`. É uma linha e devolve o consumidor TypeORM ao detector, que está fora dele desde dezembro de 2021 por um motivo independente da IA. Vale ir antes e sozinho.
2. Implementar `detect/` com `sql.py` primeiro, porque as três stacks dependem dele.
3. Ligar Django e Alembic; ligar TypeORM.
4. Trocar o corpo de `classify.py`, remover o SDK `openai` e os steps de `pip`.
5. Rodar o classificador contra o histórico completo dos três repos e levar a amostra para o time de dados conferir. Condição de aceite, não passo opcional — ver abaixo.
6. Publicar a Release `v5.0.0`, que move `v5` e `v5.0` pela automação de tags, e abrir os três PRs de uma linha trocando `@v3` por `@v5`.

Os inputs `ai_api_url`, `ai_api_key` e `ai_model` ficam no `action.yml` marcados como deprecados por uma versão, para não quebrar nenhum consumidor fora destes três que porventura os passe.

### Condição de aceite

A cobertura está medida em 96,7%; a acurácia não está. Cobertura diz que o classificador respondeu, não que respondeu certo, e é a acurácia que decide se o alerta parou de mentir. Antes da Release `v5.0.0`:

- os 55 `breaking` e os 21 `unknown` conferidos integralmente pelo time de dados;
- amostra dos 294 `safe`, dos 94 `none` e dos 174 `controlled`, com tamanho acordado com o time de dados, pesando os baldes `safe` e `none` — é neles que mora o único modo de falha silencioso;
- critério de passagem: **zero falso `safe` e zero falso `none`**. Um `breaking` classificado como `controlled` é ruído, e ruído a gente conserta na tabela de regras depois. Um `breaking` classificado como `safe` é uma mensagem verde no canal, e ninguém abre PR por causa de mensagem verde.

Falso `controlled` e falso `unknown` entram como dívida na tabela de regras, não como bloqueio da subida.

### Fase 2 — A10, condicional

Chamar modelo só no resíduo `unknown`. Gatilho explícito: quando o time de dados apontar que os casos `unknown` estão atrapalhando a triagem na prática, com exemplos. Sem isso, a fase 1 basta — e as duas maiores fontes do resíduo, `AlterUniqueTogether` e `AlterField` sem estado anterior, são atacáveis sem IA antes de considerar a A10.

## Consequências

Positivas:

- a classificação por severidade volta, sem secret, sem custo e sem fornecedor;
- os três repositórios passam a ser cobertos pelo mesmo módulo, e o consumidor TypeORM entra no detector pela primeira vez;
- a classificação vira testável em CI: as regras são código, e o corpus dos três repos é um conjunto de fixtures pronto;
- o resultado é determinístico e auditável, o que ajuda no fator SOX;
- a próxima falha aparece no primeiro PR, porque `unknown` e erro de parsing são visíveis;
- some a dependência de rede no caminho crítico da action, e com ela os retries e o timeout de 30s.

Negativas:

- 500 a 700 linhas novas para manter, com um parser por stack;
- o classificador precisa acompanhar mudanças de framework. Um upgrade de Django que troque a forma das operações, ou a migração do TypeORM 0.2 para 0.3+, exige revisitar o parser correspondente;
- cerca de 3% dos arquivos ficam em `unknown` e voltam para revisão manual. É menos ruído que os 100% amarelos de hoje, mas não é zero;
- a tabela de regras de severidade vira uma decisão de produto, não uma opinião do modelo. Alguém precisa ser dono dela — a sugestão é o time de dados, já que é quem consome o alerta.

Neutras:

- `migration_paths`, a coleta em shell e o payload do Slack não mudam de forma, só de conteúdo;
- a subida custa uma Release major e três PRs de uma linha nos consumidores, trocando `@v3` por `@v5`. É piso comum a qualquer alternativa desta ADR, não preço da A3;
- o README precisa perder a seção que apresenta o GitHub Models como caminho preferencial, e a nota sobre `Settings > Copilot > Policies > Allow GitHub Models`, que não existe mais.

## Perguntas abertas

As duas primeiras seguem valendo caso a A3 seja revertida ou caso a fase 2 seja acionada. As outras são novas e dizem respeito à A3.

1. A Afya tem gateway de IA interno com endpoint compatível com o protocolo OpenAI? Quem é o dono? (Relevante só para a fase 2.)
2. Existem outros repos da organização usando `actions/ai-inference` ou a GitHub Models API? Todos estão fora do ar pelo mesmo motivo.
3. Quem é o dono da tabela de regras de severidade? A proposta é o time de dados.
4. `breaking` deve bloquear o merge? Hoje só emite `::warning` e o PR segue. Com classificação determinística e auditável, bloquear passa a ser defensável — antes não era.
5. Existe um quarto repositório com migrações que deveria estar coberto e não está? A regra de "migração que não casa glob nenhum vira aviso" resolve o repo que chama a action e não é visto por ela; não resolve o repo que não chama a action. Este segundo caso segue aberto e é varredura de organização, não código.
6. A tabela de regras deixa `controlled` como piso para `RunPython` de dados puros, depois de extrair o DDL do corpo. O time de dados concorda com esse piso, ou uma migração de dados que toque milhões de linhas merece severidade própria?

## Referências

- [GitHub Models is now retired](https://github.blog/changelog/2026-07-30-github-models-is-now-retired/) — changelog de 2026-07-30
- [Deprecation of Azure endpoint for GitHub Models](https://github.blog/changelog/2025-07-17-deprecation-of-azure-endpoint-for-github-models/) — changelog de 2025-07-17
- [GitHub Models — docs](https://docs.github.com/en/github-models)
- `job-logs.txt` na raiz do repo — log do job que falhou em 2026-08-11
- [classify.py](../../migration-detector/classify.py), [action.yml](../../migration-detector/action.yml), [README.md](../../migration-detector/README.md)
- Workflows consumidores: `.github/workflows/migration-detector.yml` nos três serviços com banco (byte-idênticos entre si)

---

## Apêndice — identificadores nesta ADR

Os nomes de tabela, coluna, app e arquivo citados como evidência foram substituídos
por sintéticos. Este repositório é público, e os originais nomeavam o schema de
produção de três serviços privados.

A substituição é biunívoca e o mapa fica fora do repositório, em
`migration-detector/tests/fixtures/.identifier-map.json`, que é gitignorado. Quem
tem o mapa reconfere cada evidência contra o arquivo real; quem não tem lê o
argumento sem conseguir auditar a origem, que é o custo aceito.

As medições — 638 arquivos, 248 ocorrências de `AlterField`, 96,7% de cobertura —
são dos arquivos reais e não foram alteradas. A suíte do `migration-detector` tem
um teste que refaz a comparação entre fixture sintética e arquivo real sempre que
os clones estão presentes, e pula quando não estão.
