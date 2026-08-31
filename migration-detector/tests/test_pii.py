"""Nenhum dado da migração pode chegar ao Slack.

Migração de dados no consumidor Django/MySQL carrega linha de paciente. A razão de um
`Finding` já foi endurecida duas vezes — em `sql.py` e em `typeorm.py` — e as
duas correções foram feitas parser a parser. Este módulo é a invariante que
faltava: um único corpus adversarial, plantado nas posições onde cada stack
carrega dado, conferido contra **todos** os parsers e contra tudo o que sai da
action — a linha do Slack e o `analysis_json` do log do job.

O terceiro vazamento veio por `Finding.operation`, que nunca tinha sido
renderizado e por isso nunca tinha sido endurecido. Quando QQ-2159 passou a
nomear a operação na mensagem, `re.match(r"\\s*(\\w+)", masked)` em `sql.py`
publicou o primeiro campo de uma linha de `COPY` como se fosse o verbo do
statement. Daí a invariante cobrir `operation` e `reason` juntos, e não só a
razão.
"""

import json
import re

import pytest

from build_slack_payload import build_payload
from classify import analyze, build_slack_text, write_github_outputs
from detect import alembic, classify_file, django, sql, typeorm
from detect.severity import max_severity

# ---------------------------------------------------------------------------
# O que foi plantado
# ---------------------------------------------------------------------------

CPF = "12345678900"
SUBJECT_ID = "4471902"
SUBJECT_NAME = "Maria Silva"

PLANTED = (CPF, SUBJECT_ID, SUBJECT_NAME)

# Um número longo é CPF, CNPJ ou id; `varchar(255)` e `NUMERIC(10,2)` passam.
# Aspa é o outro jeito de um valor entrar numa frase.
LONG_NUMBER = re.compile(r"\d{4,}")
QUOTES = ("'", '"', "`")


# ---------------------------------------------------------------------------
# O corpus — o mesmo dado, plantado no que cada stack sabe carregar
# ---------------------------------------------------------------------------

# `ignore_name_contains: dump` filtra pelo **nome do arquivo**, então um bloco
# de dados estilo `pg_dump` dentro de `0042_backfill_subjects.sql` chega inteiro
# ao classificador.
COPY_BLOCK = (
    "COPY subjects (id, cpf, name) FROM stdin;\n"
    f"{SUBJECT_ID}\t{CPF}\t{SUBJECT_NAME}\n"
    "\\.\n"
)
INSERT = f"INSERT INTO subjects (cpf, name) VALUES ('{CPF}', '{SUBJECT_NAME}');"
BARE_CALL = f"CALL migrate_subject({SUBJECT_ID});"
UPDATE = f"UPDATE subjects SET name = '{SUBJECT_NAME}' WHERE cpf = '{CPF}';"
BARE_VALUE = f"{CPF};"
NAMED_VALUE = f"cpf_do_paciente_{CPF} algo;"

SQL_CORPUS = (COPY_BLOCK, INSERT, BARE_CALL, UPDATE, BARE_VALUE, NAMED_VALUE)

DJANGO_MIGRATION = f'''from django.db import migrations


class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL("{INSERT}"),
        migrations.RunSQL("COPY subjects (id, cpf) FROM stdin;\\n{SUBJECT_ID}\\t{CPF}"),
        migrations.RunSQL("{BARE_CALL}"),
    ]
'''

ALEMBIC_MIGRATION = f'''from alembic import op


def upgrade():
    op.execute("{INSERT}")
    op.execute("COPY subjects (id, cpf) FROM stdin;\\n{SUBJECT_ID}\\t{CPF}")
    op.execute("{BARE_CALL}")
'''

TYPEORM_MIGRATION = f'''export class BackfillSubjects1700000000000 implements MigrationInterface {{
  public async up(queryRunner: QueryRunner): Promise<void> {{
    await queryRunner.query(`{INSERT}`);
    await queryRunner.query(`{BARE_CALL}`);
    await queryRunner.query(`{UPDATE}`);
  }}
}}
'''


def _sql_findings():
    return [finding for source in SQL_CORPUS for finding in sql.classify_sql(source)]


# O dispatch também produz `Finding` — os `unknown` de "nenhum parser lê esta
# extensão", "não é Python válido" e "define os dois marcadores". Eles entram na
# mesma invariante: uma razão que passasse a interpolar conteúdo do arquivo
# vazaria pelo mesmo caminho que os três vazamentos já corrigidos.
_DISPATCH_CORPUS = (
    ("0001_backfill.rb", INSERT),
    ("0002_backfill.py", f"class Migration(  ::: {INSERT}"),
    ("0003_backfill.py", f'class Migration: pass\ndef upgrade(): pass\n# {INSERT}'),
    ("0004_backfill.sql", COPY_BLOCK),
)


def _dispatch_findings(tmp_path):
    findings = []
    for name, source in _DISPATCH_CORPUS:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        findings.extend(classify_file(path))
    return findings


# Toda entrada recebe `tmp_path` e a maioria ignora. É o preço de o dispatch
# precisar de arquivo em disco — e vale a pena para que ele seja uma linha da
# mesma tabela, e não um teste à parte que a próxima invariante esqueceria de
# incluir.
PARSERS = {
    "sql": lambda tmp_path: _sql_findings(),
    "django": lambda tmp_path: django.classify_migration(DJANGO_MIGRATION),
    "alembic": lambda tmp_path: alembic.classify_migration(ALEMBIC_MIGRATION),
    "typeorm": lambda tmp_path: typeorm.classify_migration(TYPEORM_MIGRATION),
    "dispatch": _dispatch_findings,
}


# ---------------------------------------------------------------------------
# A invariante, nos quatro parsers
# ---------------------------------------------------------------------------


class TestNoParserEchoesPlantedData:
    @pytest.mark.parametrize("parser", sorted(PARSERS))
    def test_corpus_actually_reaches_the_parser(self, parser, tmp_path):
        # Sem isto, todo teste abaixo passa numa lista vazia.
        assert PARSERS[parser](tmp_path)

    @pytest.mark.parametrize("parser", sorted(PARSERS))
    @pytest.mark.parametrize("secret", PLANTED)
    def test_no_finding_echoes_the_secret(self, parser, secret, tmp_path):
        for finding in PARSERS[parser](tmp_path):
            assert secret not in finding.operation
            assert secret not in finding.reason

    @pytest.mark.parametrize("parser", sorted(PARSERS))
    def test_no_operation_carries_a_long_number(self, parser, tmp_path):
        # Sobre este corpus, que planta dado em toda posição de dado das quatro
        # stacks. Nome de classe ou de função escrito no arquivo é código, não
        # dado, e não passa por aqui: `operation` só recebe identificador de
        # posição de chamada ou verbo de statement.
        for finding in PARSERS[parser](tmp_path):
            assert not LONG_NUMBER.search(finding.operation), finding

    @pytest.mark.parametrize("parser", sorted(PARSERS))
    @pytest.mark.parametrize("quote", QUOTES)
    def test_no_operation_carries_a_quote(self, parser, quote, tmp_path):
        for finding in PARSERS[parser](tmp_path):
            assert quote not in finding.operation, finding

    def test_a_statement_that_starts_with_data_has_no_verb_to_name(self):
        # Pareado com o de cima: prova que a operação vira `?` em vez de
        # simplesmente não haver finding. `\\w` casava dígito e devolvia
        # "4471902" como se fosse o verbo.
        findings = sql.classify_sql(COPY_BLOCK)
        assert findings
        assert [f.operation for f in findings] == ["COPY", "?"]

    @pytest.mark.parametrize(
        "statement,expected",
        (
            (BARE_VALUE, "?"),
            (NAMED_VALUE, "?"),
            (BARE_CALL, "CALL"),
            (INSERT, "INSERT"),
            ("VACUUM ANALYZE subjects;", "VACUUM"),
        ),
    )
    def test_verb_survives_when_it_really_is_a_verb(self, statement, expected):
        # O guarda não pode ter engolido os verbos de verdade junto com o dado.
        assert sql.classify_sql(statement)[0].operation == expected


# ---------------------------------------------------------------------------
# A invariante, no que sai da action
# ---------------------------------------------------------------------------


def _result(path: str, findings) -> dict:
    return {
        "has_db_change": True,
        "highest_severity": max_severity(f.severity for f in findings).value,
        "items": [
            {
                "file": path,
                "severity": f.severity.value,
                "operation": f.operation,
                "reason": f.reason,
            }
            for f in findings
        ],
    }


class TestNothingLeavesTheActionCarryingData:
    @pytest.mark.parametrize("parser", sorted(PARSERS))
    @pytest.mark.parametrize("secret", PLANTED)
    def test_slack_text_carries_no_secret(self, parser, secret, tmp_path):
        findings = PARSERS[parser](tmp_path)
        assert findings
        text = build_slack_text(
            _result("db/0042_backfill_subjects.sql", findings),
            "http://pr",
            "Backfill",
            "42",
            "dev",
        )
        assert secret not in text

    @pytest.mark.parametrize("parser", sorted(PARSERS))
    @pytest.mark.parametrize("secret", PLANTED)
    def test_slack_payload_carries_no_secret(self, parser, secret, tmp_path):
        findings = PARSERS[parser](tmp_path)
        assert findings
        result = _result("db/0042_backfill_subjects.sql", findings)
        text = build_slack_text(result, "http://pr", "Backfill", "42", "dev")
        payload = build_payload(text, "#data", result["highest_severity"])
        assert secret not in json.dumps(payload, ensure_ascii=False)

    @pytest.mark.parametrize("parser", sorted(PARSERS))
    @pytest.mark.parametrize("secret", PLANTED)
    def test_analysis_json_carries_no_secret(self, parser, secret, tmp_path):
        # `analysis_json` não é output declarado da action, mas vai para o log
        # do job com os mesmos `operation` e `reason` dentro.
        findings = PARSERS[parser](tmp_path)
        assert findings
        result = _result("db/0042_backfill_subjects.sql", findings)
        out = tmp_path / "github_output"
        write_github_outputs(
            str(out),
            result,
            0.0,
            build_slack_text(result, "http://pr", "Backfill", "42", "dev"),
        )
        content = out.read_text(encoding="utf-8")
        assert "analysis_json=" in content
        assert secret not in content

    def test_the_message_still_says_which_file_and_which_operation(self):
        # Guarda pareado: um redator que apagasse operação e razão passaria em
        # tudo acima.
        findings = sql.classify_sql(INSERT)
        assert findings
        text = build_slack_text(
            _result("db/0042_backfill_subjects.sql", findings),
            "http://pr",
            "Backfill",
            "42",
            "dev",
        )
        assert "• `db/0042_backfill_subjects.sql` — `INSERT`: " in text
        assert "precisa de revisão manual." in text


# ---------------------------------------------------------------------------
# O caminho de produção, e não uma reconstrução dele
# ---------------------------------------------------------------------------


class TestTheRealPipelineCarriesNoData:
    """`analyze` é o que roda no step. As classes acima montam `_result` à mão.

    Um redator que mudasse a forma dos itens em `analyze` — acrescentando um
    campo com o statement, por exemplo — não apareceria em nenhum teste acima,
    porque nenhum deles chama `analyze`.
    """

    def _corpus(self, tmp_path):
        files = [
            ("0001_backfill.sql", "\n".join(SQL_CORPUS)),
            ("0002_backfill.py", DJANGO_MIGRATION),
            ("0003_backfill.py", ALEMBIC_MIGRATION),
            ("0004_backfill.ts", TYPEORM_MIGRATION),
            ("0005_backfill.rb", INSERT),
        ]
        paths = []
        for name, source in files:
            path = tmp_path / name
            path.write_text(source, encoding="utf-8")
            paths.append(str(path))
        return paths

    def test_the_corpus_actually_reaches_analyze(self, tmp_path):
        # Sem isto, tudo abaixo passa num resultado onde nenhum parser achou
        # nada. `analyze` garante um item por arquivo mesmo assim, então contar
        # itens não distingue "classificou" de "não classificou": o que
        # distingue é todo arquivo ter saído com severidade própria.
        paths = self._corpus(tmp_path)
        result = analyze(paths)
        classified = {
            item["file"] for item in result["items"] if item["severity"] != "none"
        }
        assert classified == set(paths)

    @pytest.mark.parametrize("secret", PLANTED)
    def test_analysis_json_of_the_real_pipeline_carries_no_secret(self, tmp_path, secret):
        result = analyze(self._corpus(tmp_path))
        out = tmp_path / "github_output"
        write_github_outputs(
            str(out),
            result,
            0.0,
            build_slack_text(result, "http://pr", "Backfill", "42", "dev"),
        )
        content = out.read_text(encoding="utf-8")
        assert "analysis_json=" in content
        assert secret not in content

    @pytest.mark.parametrize("secret", PLANTED)
    def test_the_slack_payload_of_the_real_pipeline_carries_no_secret(self, tmp_path, secret):
        result = analyze(self._corpus(tmp_path))
        text = build_slack_text(result, "http://pr", "Backfill", "42", "dev")
        payload = build_payload(text, "#data", result["highest_severity"])
        assert secret not in json.dumps(payload, ensure_ascii=False)

    def test_no_item_of_the_real_pipeline_carries_a_long_number_in_the_operation(
        self, tmp_path
    ):
        result = analyze(self._corpus(tmp_path))
        for item in result["items"]:
            assert not LONG_NUMBER.search(item["operation"]), item
            for quote in QUOTES:
                assert quote not in item["operation"], item

    def test_the_message_still_names_the_files(self, tmp_path):
        # Guarda pareado: um redator que apagasse os itens passaria em tudo.
        paths = self._corpus(tmp_path)
        result = analyze(paths)
        text = build_slack_text(result, "http://pr", "Backfill", "42", "dev")
        assert "• `" in text
        assert any(p in text for p in paths)


# ---------------------------------------------------------------------------
# O corpus versionado da QQ-2161
# ---------------------------------------------------------------------------


class TestTheVersionedCorpusCarriesNoDataIntoTheOutput:
    """A mesma invariante, sobre o corpus de `tests/fixtures/corpus/`.

    O corpus deste módulo é adversarial: o dado está plantado exatamente onde
    cada parser costuma vazá-lo. O da QQ-2161 é o oposto — migração comum, do
    formato que os três repositórios escrevem —, e é lá que um identificador
    longo entra sem ninguém plantar: o timestamp no nome de uma classe do
    TypeORM, um `varchar(255)`, o id de revisão de um arquivo do Alembic.

    Os dois corpora precisam da asserção, e por motivos diferentes. Um prova que
    o corte funciona quando alguém tenta; o outro, que ele não depende de
    alguém tentar.
    """

    @staticmethod
    def _findings():
        from test_corpus import corpus_files

        return [f for path in corpus_files() for f in classify_file(path)]

    def test_the_corpus_actually_produces_findings(self):
        assert len(self._findings()) > 100

    def test_no_operation_carries_a_long_number(self):
        offenders = [
            f.operation for f in self._findings() if LONG_NUMBER.search(f.operation)
        ]
        assert offenders == []

    @pytest.mark.parametrize("quote", ['"', "'", "`"])
    def test_no_operation_carries_a_quote(self, quote):
        offenders = [f.operation for f in self._findings() if quote in f.operation]
        assert offenders == []

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_no_reason_carries_a_raw_quote(self, quote):
        """A crase é o delimitador; aspa é valor que passou sem ser refinado.

        Os três vazamentos desta entrega tiveram a mesma forma: um literal da
        migração entrou na frase inteiro. `ref()` cerca identificador com crase,
        então uma aspa numa razão significa que alguma coisa chegou lá por outro
        caminho — que é exatamente o que `sql.py` fazia ao publicar o primeiro
        campo de uma linha de `COPY` como se fosse o verbo do statement.
        """
        offenders = [f.reason for f in self._findings() if quote in f.reason]
        assert offenders == []

    def test_no_reason_carries_a_long_number(self):
        """Nenhuma fixture do corpus faz uma razão citar número longo.

        É o fio que arrebenta no dia em que alguém vendorizar uma migração de
        dados real como fixture nova sem olhar o que ela carrega — id de
        paciente, CPF e CNPJ passam todos por aqui.
        """
        offenders = [f.reason for f in self._findings() if LONG_NUMBER.search(f.reason)]
        assert offenders == []


# ---------------------------------------------------------------------------
# Nenhum identificador de produção sobrevive no que vai ser público
# ---------------------------------------------------------------------------
#
# Os testes acima guardam o **valor** que a migração carrega — CPF, id, nome.
# Os de baixo guardam o **nome** que a fixture usa, que é a outra metade do
# mesmo erro e a que passou três vezes.
#
# A limpeza dos identificadores foi feita em três levas, cada uma guiada por uma
# lista de termos escrita à mão. As três levas acharam o que a anterior não
# tinha procurado: a segunda achou tabela, índice e id de revisão que ninguém
# tinha nomeado; a terceira só apareceu quando se parou de grepar termo e se
# passou a comparar vocabulário. Uma quarta lista escrita à mão teria o mesmo
# defeito — ela prova que os termos lembrados não estão lá, não que não sobrou
# nenhum.
#
# O que segue não tem lista de termos. Extrai **todo nome de tabela, coluna,
# app, índice, constraint, modelo e revisão** dos dois lados pelo mesmo
# extrator, e falha nomeando a colisão. Não depende de ninguém lembrar do termo
# certo na próxima fixture.

_Q = r"""['"`]"""

# As posições onde um nome de schema mora de fato. É isto que separa este teste
# de uma comparação de tokens soltos, que devolvia centenas de falsos positivos
# de boilerplate do Django e de palavra-chave de SQL: nome de API de framework e
# prosa não aparecem nestas posições. Acrescentar uma linha aqui só pode
# aumentar o que o teste vê.
_SCHEMA_POSITIONS = [
    # Alembic / SQLAlchemy
    re.compile(rf"\bop\.[a-z_]+\(\s*{_Q}([^'\"`]+){_Q}"),
    re.compile(rf"\bsa\.Column\(\s*{_Q}([^'\"`]+){_Q}"),
    re.compile(rf"\bsa\.[A-Za-z]*Constraint\([^)]*?name\s*=\s*{_Q}([^'\"`]+){_Q}"),
    re.compile(rf"\btable_name\s*=\s*{_Q}([^'\"`]+){_Q}"),
    re.compile(rf"\b(?:down_)?revision(?:\s*:[^=]*)?\s*=\s*{_Q}([^'\"`]+){_Q}"),
    # Django
    re.compile(
        rf"\b(?:model_name|old_name|new_name|name|to|db_table|related_name)"
        rf"\s*=\s*{_Q}([^'\"`]+){_Q}"
    ),
    re.compile(rf"\bdependencies\s*=\s*\[[^\]]*?{_Q}([^'\"`]+){_Q}", re.DOTALL),
    # SQL, em qualquer stack
    re.compile(
        r"\b(?:TABLE|INDEX|ON|CONSTRAINT|TYPE|INTO|FROM|UPDATE|COLUMN|KEY|SEQUENCE)\s+"
        r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[\"`\[]?([A-Za-z_][A-Za-z0-9_$]*)",
        re.IGNORECASE,
    ),
    re.compile(r"[\"`]([A-Za-z_][A-Za-z0-9_]{2,})[\"`]"),
]

# Forma de identificador: descarta frase (tem espaço) e palavra de duas letras,
# que não identifica coisa alguma.
_IDENTIFIER_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{2,}$")


def schema_names(text: str) -> set[str]:
    """Todo nome de schema que o texto carrega, achado por posição."""
    found: set[str] = set()
    for position in _SCHEMA_POSITIONS:
        for value in position.findall(text):
            value = value.strip()
            if _IDENTIFIER_SHAPE.match(value):
                found.add(value)
    return found


def _name_parts(stem: str) -> set[str]:
    parts = {p for p in stem.split("_") if _IDENTIFIER_SHAPE.match(p)}
    return parts | ({stem} if _IDENTIFIER_SHAPE.match(stem) else set())


# Onde as migrações reais moram em cada consumidor. O glob não identifica
# serviço nenhum; o nome do diretório, que vem do mapa fora do repositório,
# identifica — e é por isso que ele não está escrito aqui.
_REAL_GLOBS = {
    "django": "django/app/*/migrations/*.py",
    "alembic": "**/versions/*.py",
    "typeorm": "migrations/*.ts",
    # O `*` do meio é o diretório do banco: o consumidor Doctrine tem dois, e o
    # vocabulário dos dois entra na varredura. Um glob que cobrisse só um deles
    # deixaria metade do schema real fora da comparação, e uma fixture que
    # copiasse um nome de lá passaria batido.
    "doctrine": "Migrations/*/Version2*.php",
}

# Palavra que colide por ser comum, não por identificar estes serviços.
#
# **Uma entrada só entra aqui com o motivo escrito.** Um nome de tabela ou de
# app não é genérico por ser inconveniente: ele vai para o rename e para o mapa
# fora do repositório. `test_the_allowlist_has_no_compound_table_name` guarda
# essa fronteira, e a revisão que liberou cada linha abaixo olhou uma por uma.
_GENERIC_BY_REVIEW = {
    # --- domínio, e qualquer sistema do ramo tem ---------------------------
    "clinic": "qualquer sistema com mais de um estabelecimento tem",
    "invoice": "qualquer sistema com cobrança tem",
    "schedule": "qualquer sistema com agenda tem",
    "order": "palavra de SQL e de domínio ao mesmo tempo",
    "bill": "idem `invoice`",
    "installments": "idem `invoice` — parcela é conceito de cobrança, não tabela deste serviço",
    "attempt": "substantivo comum; aparece em qualquer fluxo com retentativa",
    "score": "substantivo comum",
    "room": "substantivo comum",
    "tab": "substantivo comum, e é como qualquer UI chama uma aba",
    "section": "substantivo comum, idem `tab`",
    "users": "tabela que existe em todo schema",
    "title": "coluna que existe em todo schema",
    "cpf": "classe de dado que este módulo protege, não objeto de schema",
    "clinic_id": "idem `clinic`; chave estrangeira de nome previsível",
    # --- convenção, não escolha ------------------------------------------
    "uuid": "tipo, não nome",
    "revision": "palavra do Alembic",
    "_updated_at": "coluna de timestamp com prefixo, convenção",
    "schedule_status_enum": (
        "o TypeORM gera `<tabela>_<coluna>_enum` sozinho; deriva de `schedule` e "
        "`status`, os dois já genéricos"
    ),
    "schedule_status_enum_old": "idem, com o sufixo que o TypeORM usa na reescrita de enum",
    "information_schema": "catálogo padrão do PostgreSQL",
    "yes": "valor que o `IS_NULLABLE` do `information_schema` assume, não um nome",
    "django_add_default_value": "pacote público do PyPI, lido pelo dispatch como operação de terceiro",
    # --- artefato do extrator, não identificador ---------------------------
    "__future__": "import do Python",
    "__init__": "nome de arquivo do Python",
    "for": "palavra da linguagem, capturada pela regex de SQL",
    "with": "idem",
    "module": "idem",
    "identifier": "idem",
    "chamada": "prosa em português capturada pela regex de SQL",
    "origem": "idem",
    # --- coluna que existe em todo schema ---------------------------------
    "idx": "abreviação convencional de índice",
    "event": "substantivo comum",
    "event_id": "chave estrangeira de nome previsível",
    "document_id": "idem",
    "version_id": "idem",
    "date_added": "coluna de timestamp, convenção",
    "dt_created": "idem",
    "startDate": "idem",
    "messageId": "chave estrangeira de nome previsível",
    "_deleted": "flag de exclusão lógica, convenção",
    "is_default": "flag de nome previsível",
    "legacy": "adjetivo comum",
    "error_message": "coluna de nome previsível em qualquer tabela que registre falha",
    "updated_by_id": "convenção `<campo>_by_id` de chave estrangeira de auditoria",
    "payment_method": "composto de duas palavras de domínio, nenhuma específica destes serviços",
    "origin": "substantivo comum",
    "suspended": "adjetivo comum",
}

_ALLOWLIST = frozenset(word.lower() for word in _GENERIC_BY_REVIEW)

# Palavra-chave de SQL, de ORM e substantivo universal de schema. Não precisa de
# motivo linha a linha: nenhuma delas nomeia objeto de nenhum serviço.
_VOCABULARY = frozenset("""
id ids name names code codes type types kind status state value values data
title label text description created updated deleted created_at updated_at
deleted_at time_created time_updated date_created timestamp version number
count total order sort index key uuid slug email phone active enabled
disabled default rating comment message content body url link path file
size width height color start end begin first last next prev parent child
user users owner author group role permission token secret hash checksum
document documents item items entry entries record records log logs
integer varchar boolean bigint smallint numeric decimal float double
char character date datetime timestamptz interval enum serial bigserial json
jsonb array now nextval currval setval
select insert update delete create drop alter rename add remove modify change
table column constraint sequence view schema database unique
primary foreign check exists cascade restrict null not set on off
using where from into as and or is in like between limit offset
public private true false none old new
django models migrations migration operations dependencies initial atomic
apps schema_editor connection cursor vendor mysql postgresql sqlite
charfield textfield integerfield bigintegerfield booleanfield datefield
datetimefield decimalfield foreignkey manytomanyfield onetoonefield
autofield uuidfield jsonfield emailfield urlfield
blank choices editable help_text max_length max_digits db_index verbose_name
indexes constraints ordering fields field model model_name options price stock
alembic revision down_revision revises branch_labels depends_on upgrade
downgrade op sa sqlalchemy dialects nullable server_default
typeorm queryrunner migrationinterface promise async await source payload
transaction objects merge adds help non init future reason severity finding
findings test app row list warning headline channel username action history
missing external signature
""".split())


def _real_vocabulary(stacks):
    """Nomes de schema dos clones. `None` quando nenhum está por perto."""
    from test_corpus import IDENTIFIER_MAP, WORKSPACE

    if IDENTIFIER_MAP is None:
        return None
    vocabulary, found_a_clone = set(), False
    for stack, glob in sorted(stacks.items()):
        root = WORKSPACE / IDENTIFIER_MAP["consumers"][stack]
        if not root.is_dir():
            continue
        for path in root.glob(glob):
            found_a_clone = True
            vocabulary |= schema_names(path.read_text(encoding="utf-8", errors="replace"))
            vocabulary |= _name_parts(path.stem)
    return vocabulary if found_a_clone else None


def _package_files():
    from test_corpus import package_files

    return package_files()


def _our_vocabulary():
    """nome de schema -> arquivos nossos que o carregam."""
    hits: dict[str, set[str]] = {}
    for path, text in _package_files():
        for token in schema_names(text) | _name_parts(path.stem):
            hits.setdefault(token, set()).add(path.name)
    return hits


def _synthetic_names() -> set[str]:
    """Os nomes que nós escolhemos.

    Colisão entre um nome sintético e o vocabulário real não conta nada a quem
    lê o repositório público, porque o nome é o nosso. Sai do mapa, então
    acompanha o rename sozinha.
    """
    from test_corpus import IDENTIFIER_MAP

    return set() if IDENTIFIER_MAP is None else set(IDENTIFIER_MAP["identifiers"])


def _leaked(ours, real) -> set[str]:
    exempt = _synthetic_names()
    return {
        token
        for token in set(ours) & real
        if token.lower() not in _VOCABULARY
        and token.lower() not in _ALLOWLIST
        and token not in exempt
    }


@pytest.fixture(scope="module")
def real_vocabulary():
    from test_corpus import NO_MAP, IDENTIFIER_MAP, _why_it_skipped

    if IDENTIFIER_MAP is None:
        pytest.skip(NO_MAP)
    vocabulary = _real_vocabulary(_REAL_GLOBS)
    if vocabulary is None:
        pytest.skip(_why_it_skipped("django"))
    return vocabulary


@pytest.fixture(scope="module")
def our_vocabulary():
    return _our_vocabulary()


class TestNoProductionNameSurvivesInWhatGoesPublic:
    """A varredura que não depende de lembrar do termo certo.

    Pula quando os clones não estão ao lado, igual a `TestTheRealCorpus`. No CI
    público ele não roda — lá não há corpus real com que comparar. Ele roda na
    máquina de quem adiciona fixture, que é onde o erro entra.
    """

    def test_the_extractor_finds_what_it_claims_to_find(self):
        """Sem isto, um extrator quebrado passaria verde por não achar nada."""
        found = schema_names(
            'op.create_table("some_table")\n'
            "migrations.AddField(model_name='some_model', name='some_column')\n"
            "ALTER TABLE `another_table` ADD COLUMN `another_column` int\n"
            'revision = "aabbccddeeff"\n'
        )
        assert {
            "some_table",
            "some_model",
            "some_column",
            "another_table",
            "another_column",
            "aabbccddeeff",
        } <= found

    def test_both_sides_go_through_the_same_extractor(self):
        """Extrator assimétrico passaria verde por construção.

        Se um lado deixar de chamar `schema_names`, a interseção esvazia sozinha
        e o teste principal fica verde sem provar nada.
        """
        assert "schema_names" in _real_vocabulary.__code__.co_names
        assert "schema_names" in _our_vocabulary.__code__.co_names

    def test_no_schema_name_of_the_real_corpus_appears_here(
        self, real_vocabulary, our_vocabulary
    ):
        leaked = _leaked(our_vocabulary, real_vocabulary)
        assert leaked == set(), (
            f"{len(leaked)} nome(s) de schema do corpus real ainda aparecem aqui. "
            "Renomeie e registre o par no mapa fora do repositório; só mova para "
            "`_GENERIC_BY_REVIEW` se for genérico de verdade, com o motivo escrito:\n"
            + "\n".join(
                f"  {token}  em  {', '.join(sorted(our_vocabulary[token]))}"
                for token in sorted(leaked)
            )
        )

    def test_no_long_real_name_is_buried_inside_a_synthetic_one(
        self, real_vocabulary, our_vocabulary
    ):
        """A brecha que o teste acima não vê: contenção, não igualdade.

        A comparação de vocabulário casa identificador inteiro, então um nome
        real escondido **dentro** de um nome sintético mais longo passava —
        `ix_<algo>` construído em volta de uma coluna real, um `fk_` que
        preservava o sufixo real. Foi assim que quatro nomes sobreviveram às
        três primeiras levas.

        O corte de 12 caracteres com `_` é o que separa "distintivo" de
        "palavra": nome curto ou de uma só palavra colide por ser comum e já é
        assunto do allowlist.
        """
        synthetic = _synthetic_names()
        distinctive = {
            name
            for name in real_vocabulary
            if len(name) >= 12
            and "_" in name
            and name.lower() not in _VOCABULARY
            and name.lower() not in _ALLOWLIST
            and name not in synthetic
        }
        texts = {path.name: text for path, text in _package_files()}
        buried = {
            name: sorted(n for n, text in texts.items() if name in text)
            for name in distinctive
        }
        buried = {name: where for name, where in buried.items() if where}
        assert buried == {}, (
            "nome real distintivo aparece dentro de um nome nosso mais longo:\n"
            + "\n".join(
                f"  {name}  em  {', '.join(where)}" for name, where in sorted(buried.items())
            )
        )

    def test_every_allowlisted_word_has_a_written_reason(self):
        empty = {word for word, why in _GENERIC_BY_REVIEW.items() if not why.strip()}
        assert empty == set(), sorted(empty)

    def test_the_allowlist_has_no_compound_table_name(self):
        """`_GENERIC_BY_REVIEW` é para palavra, não para nome de tabela.

        Sem esta fronteira, a saída fácil de um teste vermelho é colar
        `algum_app_algum_modelo` no allowlist e seguir a vida. As exceções são
        as que a revisão liberou com motivo, nomeadas uma a uma aqui para que a
        próxima não entre por descuido.
        """
        exempt = {
            "schedule_status_enum",
            "schedule_status_enum_old",
            "information_schema",
            "django_add_default_value",
            "_updated_at",
            "updated_by_id",
        }
        compound = {
            word
            for word in set(_GENERIC_BY_REVIEW) - exempt
            if not word.startswith("__")  # dunder do Python não é nome de tabela
            and (word.count("_") >= 2 or (word.count("_") == 1 and len(word) > 16))
        }
        assert compound == set(), sorted(compound)

    def test_the_allowlist_names_only_words_that_really_collide(
        self, real_vocabulary, our_vocabulary
    ):
        """Allowlist é liberação revisada, não lugar de guardar nome morto.

        Palavra que já não está nos dois lados sai da lista, senão ela cresce
        para sempre e para de dizer o que foi realmente revisado. As entradas de
        artefato do extrator ficam de fora da checagem: elas existem justamente
        porque a regex as captura, não porque colidem.
        """
        artefacts = {
            "__future__", "__init__", "for", "with", "module", "identifier",
            "chamada", "origem", "information_schema", "django_add_default_value",
        }
        shared = {t.lower() for t in set(our_vocabulary) & real_vocabulary}
        stale = (set(_ALLOWLIST) - artefacts) - shared
        assert stale == set(), (
            "estas entradas do allowlist já não colidem e podem sair: " f"{sorted(stale)}"
        )
