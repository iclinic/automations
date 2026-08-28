# ADR 0001 — Classificação de impacto do migration-detector após a retirada do GitHub Models

- Status: proposta
- Data: 2026-08-27
- Revisão: 2026-08-28 — escopo ampliado para as três stacks consumidoras; decisão trocada de "provedor pago" (A4/A6/A8) para "classificador determinístico" (A3), com base na medição registrada abaixo
- Decisores: time de plataforma (dono do repo `iclinic/automations`), time de dados (consumidor do alerta)
- Componente afetado: `migration-detector/`
- Repositórios consumidores: `iclinic/iclinic-api`, `iclinic/iclinic-pep-api`, `iclinic/iclinic-video-conference-api`
- Substitui: a escolha de provedor registrada em `migration-detector/README.md` (seção "Provedor de IA")

## Contexto

A action `migration-detector` detecta arquivos de migração em PRs, classifica o impacto em `safe`, `controlled` ou `breaking` e avisa o time de dados no Slack. O provedor de IA padrão era a GitHub Models API em `https://models.inference.ai.azure.com`, autenticada com o próprio `GITHUB_TOKEN` e a permissão `models: read`. Não havia secret extra nem custo.

Esse provedor não existe mais.

### Evidência 1 — o job em produção

Log do job em `iclinic/iclinic-api` PR #4836, de 2026-08-11 (arquivo `job-logs.txt` na raiz deste repo):

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
| `iclinic-api` | Django (2.2) | MySQL | `django/app/*/migrations/*.py` | 591 |
| `iclinic-pep-api` | Alembic + SQLAlchemy (FastAPI) | PostgreSQL | `app/alembic/versions/*.py` | 13 |
| `iclinic-video-conference-api` | TypeORM 0.2 | PostgreSQL | `migrations/*.ts` | 34 |

Os três chamam `iclinic/automations/migration-detector@v3` com workflows idênticos — `github_token`, `slack_webhook_url` e `slack_channel`, e nada mais. Nenhum dos três passa `ai_api_url`, `ai_api_key` ou `migration_paths`. Consequência que pesa na decisão: qualquer alternativa que exija um input novo obrigatório custa três PRs; qualquer alternativa que se resolva dentro do `action.yml` custa zero.

Detalhe relevante para o esforço: `call_ai` não tem teste. Das 40 asserções em `tests/test_classify.py`, nenhuma exercita a chamada HTTP; o único vínculo com a OpenAI é uma docstring e um helper de mock não usado. Trocar de SDK, ou remover o SDK, não quebra a suíte.

### Evidência 4 — o video-conference nunca foi coberto

O default de `migration_paths` no [action.yml](../../migration-detector/action.yml) é:

```
**/*.sql,**/migrations/*.py,**/alembic/versions/*.py,**/*AutoMigrate.ts,**/*automigrate.ts
```

Os arquivos do `iclinic-video-conference-api` vivem em `migrations/*.ts`. O glob `**/migrations/*.py` não os pega, porque a extensão é `.ts`. Sobra `**/*AutoMigrate.ts`, que casa apenas com os arquivos gerados pelo `typeorm migration:generate -n AutoMigrate`. Contagem no repo: 23 dos 34 casam; 11 não casam.

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

São exatamente as 11 migrações mais recentes do repositório, todas de dezembro de 2021 em diante, ou seja, todas as que foram escritas à mão em vez de geradas. Na prática, o `iclinic-video-conference-api` está fora do detector desde que o time parou de usar o nome `AutoMigrate`, e o alerta nunca disparou para nenhuma delas. Isso é anterior e independente da queda do provedor de IA.

Vale registrar também que a conversão de glob para regex no step `Collect` é feita com três `sed` encadeados e trata só `*`, `**` e ponto literal. Ela não trata `?`, `[]` nem chaves. É frágil, mas suficiente para os padrões atuais.

## Fatores de decisão

1. Restaurar a classificação por severidade. Sem ela o alerta virou ruído.
2. Falhar de forma visível. Job verde sem sinal não pode acontecer de novo.
3. Custo de mudança nos repos consumidores. A action é composite e consumida por três repos; mexer em inputs obrigatórios significa abrir PR em cada workflow que a chama.
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

`claude-haiku-4-5` custa US$ 1 por milhão de tokens de entrada e US$ 5 de saída. O volume desta action é pequeno: os quatro arquivos do PR #4836 somam 9,3 KB, algo perto de 3.200 tokens de entrada e 400 de saída, ou cerca de US$ 0,005 por PR. A cem PRs com migração por mês, menos de US$ 1.

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

O segundo é que todo o resto vira SQL. O TypeORM do `iclinic-video-conference-api` não usa a API de schema builder: os 34 arquivos são 100% `queryRunner.query()` com DDL literal dentro de template string, 133 statements no total. O `migrations.RunSQL` do Django (57 ocorrências) e o `op.execute()` do Alembic caem no mesmo lugar. Então o classificador de SQL não é um parser a mais para o TypeScript: é o núcleo compartilhado pelas três stacks, e o front-end de TypeScript é uma regex que extrai template literals do corpo de `up()`.

A diferença real entre os três não é a linguagem, é o dialeto. O `iclinic-api` é MySQL e escreve `ALTER TABLE x MODIFY col BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE`. Os outros dois são PostgreSQL e escrevem `ALTER TABLE x ALTER COLUMN col SET NOT NULL` e `ALTER COLUMN col TYPE`. O classificador precisa das duas gramáticas, o que são duas famílias de regex, não dois parsers.

### O `AlterField` é resolúvel, ao contrário do que esta ADR dizia antes

A versão anterior deste documento afirmava que `AlterField` no Django e `alter_column` no Alembic carregam só o estado final do campo e que distinguir "varchar de 50 para 100" de "varchar para int" exigiria o histórico de migrações ou o diff do modelo. A primeira metade está certa. A segunda ignora que o histórico de migrações está no checkout: os workflows dos três repos usam `actions/checkout@v4` com `fetch-depth: 0`, e o diretório `migrations/` inteiro está em disco quando a action roda.

O estado anterior de um campo é o último `CreateModel`, `AddField` ou `AlterField` que mencionou aquele par (modelo, campo) antes da migração atual, dentro do mesmo app. Isso é indexável com `ast` em uma passada por diretório.

Medição sobre as 248 ocorrências de `AlterField` do `iclinic-api`:

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
accounts/0042_modify_physician_public_id_to_non_null.py         breaking  campo passou a NOT NULL
accounts/0043_modify_physicianprocedure_public_id_to_non_null.py breaking  campo passou a NOT NULL
ai_integration/0005_auto_20240705_1127.py                        breaking  tipo do campo mudou: ForeignKey -> UUIDField
```

As duas primeiras são a família do PR que aparece no log da Evidência 1. A versão anterior desta ADR usava esse PR como argumento contra a A3 — "o PR do log é justamente um caso desses". É o contrário: o classificador determinístico acerta esse caso duas vezes, por dois caminhos independentes. Pelo estado anterior reconstruído, como acima. E pelo SQL, porque essas migrações usam `SeparateDatabaseAndState` e o `database_operations` carrega o DDL literal `ALTER TABLE accounts_physicianprocedure MODIFY public_id BIGINT NOT NULL`. O que o modelo errou por estar fora do ar, uma regex acerta lendo a string.

### Cobertura medida nas três stacks

Rodando o protótipo completo, com resolução de `AlterField`, sobre todo o corpus:

| Stack | Repositório | Arquivos | `none` | `safe` | `controlled` | `breaking` | `unknown` | Determinístico |
|---|---|---|---|---|---|---|---|---|
| Django | `iclinic-api` | 591 | 94 | 273 | 159 | 45 | 20 | 571 (96,6%) |
| Alembic | `iclinic-pep-api` | 13 | 0 | 11 | 0 | 2 | 0 | 13 (100%) |
| TypeORM / SQL | `iclinic-video-conference-api` | 34 | 0 | 10 | 15 | 8 | 1 | 33 (97,1%) |
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

Do lado dos consumidores, nada muda. Os três workflows passam `github_token`, `slack_webhook_url` e `slack_channel`, e continuam passando exatamente isso. Zero PR nos repos consumidores, zero secret novo.

O único ajuste no `action.yml` é o default de `migration_paths`, que precisa ganhar `**/migrations/*.ts` para fechar o buraco da Evidência 4. Isso também é interno à action.

## Comparação

| Alternativa | Esforço | Secret novo | PR nos 3 repos | Custo/mês | Dependência externa | Cobre as 3 stacks | Restaura severidade |
|---|---|---|---|---|---|---|---|
| A0 fallback atual | nenhum | não | não | 0 | morta | não | não |
| A1 `models.github.ai` | baixo | não | não | 0 | morta (410) | não | não |
| A2 sem IA | baixo | não | não | 0 | nenhuma | n/a | não |
| A3 determinístico | alto | não | não | 0 | nenhuma | sim (96,7%) | sim |
| A4 Anthropic | baixo | sim | sim | < US$ 1 | um fornecedor | sim | sim |
| A5 OpenAI | mínimo | sim | sim | < US$ 1 | um fornecedor | sim | sim |
| A6 Azure / Foundry | médio | sim | sim | < US$ 1 | contrato existente | sim | sim |
| A7 Bedrock / Vertex | médio-alto | não (OIDC) | sim, 3× | < US$ 1 | contrato existente | sim | sim |
| A8 gateway interno | mínimo | sim | sim | interno | interna | sim | sim |
| A10 híbrido | alto | sim | sim | centavos | parcial | sim | sim |

Custo não é critério de decisão aqui. A diferença entre a opção mais cara e a mais barata da tabela é menor que um dólar por mês. O que separa as alternativas é esforço, governança e dependência.

## Decisão

Adotar a A3, o classificador determinístico, para as três stacks.

O que sustenta a troca em relação à versão anterior desta ADR, que apontava para um provedor pago: a objeção que empurrava a A3 para uma fase 2 condicional era a cobertura, e a cobertura foi medida em 96,7% sobre 638 arquivos reais dos três repositórios. O caso concreto que a ADR citava como fora do alcance determinístico — `modify_public_id_to_non_null` — é classificado corretamente como `breaking` por duas vias independentes. A objeção não se sustenta.

A A3 também é a única alternativa da tabela que atende os fatores 3, 4, 6 e 8 ao mesmo tempo: nenhum PR nos consumidores, nenhum secret, nenhum fornecedor, e as três stacks cobertas pelo mesmo módulo.

### O que entra junto, e não é negociável

Falhar alto. Sai o disfarce de `controlled` com `confidence: 0.0`. O que entra:

- severidade `unknown`, distinta e visível, para o resíduo que o classificador não resolve e para qualquer erro de parsing. A mensagem no Slack diz que aquele arquivo precisa de olho humano, com o nome da operação que não foi entendida;
- erro de execução do próprio classificador derruba o step. Se o módulo lança exceção, o job fica vermelho. Não existe caminho em que o job passa verde sem ter classificado nada;
- o campo `confidence` perde sentido num classificador determinístico. Ou sai dos outputs, ou fica fixo em `1.0` para as severidades resolvidas e `0.0` para `unknown`, mantendo compatibilidade com quem lê o output. `minimum_confidence` vira input inerte e deve ser marcado como deprecado no `action.yml`.

### Ordem de execução

1. Corrigir `migration_paths` para incluir `**/migrations/*.ts`. É uma linha e devolve o `iclinic-video-conference-api` ao detector, que está fora dele desde dezembro de 2021 por um motivo independente da IA. Vale ir antes e sozinho.
2. Implementar `detect/` com `sql.py` primeiro, porque as três stacks dependem dele.
3. Ligar Django e Alembic; ligar TypeORM.
4. Trocar o corpo de `classify.py`, remover o SDK `openai` e os steps de `pip`.
5. Rodar o classificador contra o histórico completo dos três repos e levar a amostra de `breaking` e `controlled` para o time de dados conferir antes de subir a `@v4`.

Os inputs `ai_api_url`, `ai_api_key` e `ai_model` ficam no `action.yml` marcados como deprecados por uma versão, para não quebrar nenhum consumidor fora destes três que porventura os passe.

### Fase 2 — A10, condicional

Chamar modelo só no resíduo `unknown`. Gatilho explícito: quando o time de dados apontar que os casos `unknown` estão atrapalhando a triagem na prática, com exemplos. Sem isso, a fase 1 basta — e as duas maiores fontes do resíduo, `AlterUniqueTogether` e `AlterField` sem estado anterior, são atacáveis sem IA antes de considerar a A10.

## Consequências

Positivas:

- a classificação por severidade volta, sem secret, sem custo e sem fornecedor;
- os três repositórios passam a ser cobertos pelo mesmo módulo, e o `iclinic-video-conference-api` entra no detector pela primeira vez;
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
- o README precisa perder a seção que apresenta o GitHub Models como caminho preferencial, e a nota sobre `Settings > Copilot > Policies > Allow GitHub Models`, que não existe mais.

## Perguntas abertas

As duas primeiras seguem valendo caso a A3 seja revertida ou caso a fase 2 seja acionada. As outras são novas e dizem respeito à A3.

1. A Afya tem gateway de IA interno com endpoint compatível com o protocolo OpenAI? Quem é o dono? (Relevante só para a fase 2.)
2. Existem outros repos da organização usando `actions/ai-inference` ou a GitHub Models API? Todos estão fora do ar pelo mesmo motivo.
3. Quem é o dono da tabela de regras de severidade? A proposta é o time de dados.
4. `breaking` deve bloquear o merge? Hoje só emite `::warning` e o PR segue. Com classificação determinística e auditável, bloquear passa a ser defensável — antes não era.
5. Existe um quarto repositório com migrações que deveria estar coberto e não está? A Evidência 4 mostra que dá para estar "integrado" e invisível ao mesmo tempo.
6. `RunPython` está classificado como `controlled` no protótipo, por ser mudança de dados sem DDL. O time de dados concorda, ou quer `unknown`?

## Referências

- [GitHub Models is now retired](https://github.blog/changelog/2026-07-30-github-models-is-now-retired/) — changelog de 2026-07-30
- [Deprecation of Azure endpoint for GitHub Models](https://github.blog/changelog/2025-07-17-deprecation-of-azure-endpoint-for-github-models/) — changelog de 2025-07-17
- [GitHub Models — docs](https://docs.github.com/en/github-models)
- `job-logs.txt` na raiz do repo — log do job que falhou em 2026-08-11
- [classify.py](../../migration-detector/classify.py), [action.yml](../../migration-detector/action.yml), [README.md](../../migration-detector/README.md)
- Workflows consumidores: `iclinic-api/.github/workflows/migration-detector.yml`, `iclinic-pep-api/.github/workflows/migration-detector.yml`, `iclinic-video-conference-api/.github/workflows/migration-detector.yml`
