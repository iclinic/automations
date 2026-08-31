"""Testes do parser de migrações do TypeORM.

As migrações usadas aqui foram derivadas do corpus real do consumidor
TypeORM/PostgreSQL — 34 arquivos em `migrations/`, duas populações
com formatação diferente: 23 gerados pelo `typeorm migration:generate`
(`*-AutoMigrate.ts`, `Promise<any>`, segundo argumento `undefined`) e 11
escritos à mão de dezembro de 2021 em diante (`Promise<void>`, propriedade
`name`, sem o segundo argumento). As duas formas estão em `TestCorpusShapes`,
porque foi a formatação da segunda que ficou invisível para o detector até a
QQ-2153.

O resto do arquivo é o que o corpus não tem e o parser precisa aguentar mesmo
assim: `${}`, chave dentro de literal, comentário com `down(`, `up` definido
duas vezes, corpo que não fecha.

Nenhum arquivo do corpus é copiado para cá: a suíte de fixtures é da QQ-2161.
"""

import textwrap

import pytest

from detect import typeorm
from detect.alembic import classify_migration as classify_alembic
from detect.severity import MANUAL, Severity, max_severity
from detect.sql import classify_statement
from detect.typeorm import _GATES, classify_migration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HEAD = 'import {MigrationInterface, QueryRunner} from "typeorm";\n'


def migration(up: str, down: str = "", head: str = HEAD) -> str:
    """Um arquivo de migração do TypeORM com `up` e `down` no corpo da classe."""
    return (
        f"{head}\n"
        "export class AutoMigrate1574996267518 implements MigrationInterface {\n"
        "\n"
        "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
        f"{textwrap.indent(textwrap.dedent(up).strip(), ' ' * 8)}\n"
        "    }\n"
        "\n"
        "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
        f"{textwrap.indent(textwrap.dedent(down).strip(), ' ' * 8)}\n"
        "    }\n"
        "\n"
        "}\n"
    )


def query(sql: str) -> str:
    """Uma chamada `queryRunner.query()` com o SQL numa template string."""
    return f"await queryRunner.query(`{sql}`);"


def severities(source: str) -> list[Severity]:
    return [finding.severity for finding in classify_migration(source)]


def reasons(source: str) -> list[str]:
    return [finding.reason for finding in classify_migration(source)]


def only(source: str):
    """O único finding da migração — falha se houver mais de um."""
    findings = classify_migration(source)
    assert len(findings) == 1, findings
    return findings[0]


def one(sql: str):
    """O único finding de uma migração cujo `up()` roda um SQL só."""
    return only(migration(query(sql)))


CREATE = 'CREATE TABLE "schedule" ("meetingId" uuid NOT NULL)'
DROP_COLUMN = 'ALTER TABLE "schedule" DROP COLUMN "date"'
ADD_COLUMN = 'ALTER TABLE "schedule" ADD "suspended" boolean NOT NULL DEFAULT false'


# ---------------------------------------------------------------------------
# Isolar up() de down()
# ---------------------------------------------------------------------------


class TestUpBody:
    def test_returns_one_finding_per_statement_in_order(self):
        source = migration(f"{query(CREATE)}\n{query(DROP_COLUMN)}")
        findings = classify_migration(source)
        assert [finding.operation for finding in findings] == ["CREATE TABLE", "DROP COLUMN"]
        assert findings[0].reason == "Tabela `schedule` criada."
        assert findings[1].reason == (
            "Coluna `date` removida de `schedule` — quem lê essa coluna quebra."
        )

    def test_down_is_ignored(self):
        source = migration(query(CREATE), down=query('DROP TABLE "schedule"'))
        assert severities(source) == [Severity.SAFE]

    def test_down_declared_before_up_is_still_ignored(self):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query('DROP TABLE ' + chr(34) + 'schedule' + chr(34))}\n"
            "    }\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(CREATE)}\n"
            "    }\n"
            "}\n"
        )
        assert severities(source) == [Severity.SAFE]

    def test_a_file_without_up_yields_no_findings(self):
        source = (
            f"{HEAD}\n"
            "export class NotAMigration {\n"
            "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert classify_migration(source) == []

    def test_an_empty_up_yields_no_findings(self):
        assert classify_migration(migration("")) == []

    def test_a_method_whose_name_only_starts_with_up_is_not_up(self):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async upgradeSchema(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert classify_migration(source) == []

    def test_a_method_whose_name_only_ends_with_up_is_not_up(self):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async cleanup(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert classify_migration(source) == []


class TestUpThatIsNotADefinition:
    """As formas de `up` aparecer no arquivo sem ser uma definição.

    O parser não tem escopo: ele acha a palavra e decide pelo que vem depois.
    Cada fonte aqui põe um bloco no caminho — é o bloco que o parser erraria em
    adotar como corpo, e o erro sai como uma segunda definição de `up`, que
    vira `unknown` e derruba a classificação do arquivo que estava certa.

    Todas classificam o mesmo `up()` de verdade, então o `safe` sozinho é a
    afirmação: nenhuma delas virou definição.
    """

    def source(self, tail: str) -> str:
        return (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(CREATE)}\n"
            "    }\n"
            f"{tail}"
            "}\n"
        )

    def test_a_call_to_up_before_another_method(self):
        # `down()` chamando `up()` não pode fazer o corpo do método seguinte
        # virar o corpo lido.
        assert severities(
            self.source(
                "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
                "        up(queryRunner);\n"
                "    }\n"
                "    private log(message: string): void {\n"
                "        console.log(message);\n"
                "    }\n"
            )
        ) == [Severity.SAFE]

    def test_a_qualified_call_to_up_before_a_block(self):
        assert severities(
            self.source(
                "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
                "        await this.up(queryRunner)\n"
                "        {\n"
                f"            {query(DROP_COLUMN)}\n"
                "        }\n"
                "    }\n"
            )
        ) == [Severity.SAFE]

    def test_a_reference_to_up_with_no_block_after_it(self):
        assert severities(self.source("") + "export default up;\n") == [Severity.SAFE]

    def test_up_passed_as_an_argument_to_a_callback(self):
        assert severities(
            self.source("")
            + "registerReversible(up, (queryRunner) => {\n"
            f"    {query(DROP_COLUMN)}\n"
            "});\n"
        ) == [Severity.SAFE]

    def test_a_binding_named_up_before_a_callback(self):
        # `up` recebe um valor, e o `=>` que vem depois é de outra função. O
        # `;` no meio é o que separa uma coisa da outra.
        assert severities(
            self.source("")
            + "export const up = rollback;\n"
            "const log = (message: string) => {\n"
            f"    {query(DROP_COLUMN)}\n"
            "};\n"
        ) == [Severity.SAFE]

    def test_a_binding_named_up_that_wraps_a_callback(self):
        # Sem `;` nenhum, e o `=>` é do parâmetro de dentro: o parêntese que
        # não fechou é o que denuncia que a seta não é a de `up`.
        assert severities(
            self.source("")
            + "export const up = compose(reverse, (queryRunner) => {\n"
            f"    {query(DROP_COLUMN)}\n"
            "});\n"
        ) == [Severity.SAFE]


class TestUpDeclarationForms:
    """As formas de declarar `up` que o parser tem que reconhecer."""

    @pytest.mark.parametrize(
        "header",
        [
            "public async up(queryRunner: QueryRunner): Promise<any> {",
            "public async up(queryRunner: QueryRunner): Promise<void> {",
            "async up(queryRunner) {",
            "up(queryRunner: QueryRunner) {",
            "public async up(queryRunner: QueryRunner = defaultRunner()): Promise<void> {",
            "up?(queryRunner: QueryRunner): Promise<void> {",
            "up = async (queryRunner: QueryRunner): Promise<void> => {",
            "public up = (queryRunner: QueryRunner) => {",
        ],
    )
    def test_the_declaration_form_does_not_change_the_reading(self, header):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            f"    {header}\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert only(source).reason == (
            "Coluna `date` removida de `schedule` — quem lê essa coluna quebra."
        )

    def test_a_type_only_declaration_of_up_is_not_a_body(self):
        # Com um `up()` de verdade depois, a declaração de tipo só pode ser
        # rejeitada pelo portão: sobrando as duas, o arquivo sai `unknown`.
        source = (
            f"{HEAD}\n"
            "export interface Reversible {\n"
            "    up: (queryRunner: QueryRunner) => Promise<void>;\n"
            "}\n"
            "\n"
            "export class AutoMigrate1 implements Reversible {\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert only(source).reason == (
            "Coluna `date` removida de `schedule` — quem lê essa coluna quebra."
        )


class TestBodyDelimitation:
    """Onde o corpo de `up()` termina, quando o texto tenta enganar a chave."""

    def test_a_brace_inside_a_template_literal_does_not_close_the_body(self):
        source = migration(
            "await queryRunner.query(`COMMENT ON TABLE \"schedule\" IS 'a } b'`);\n"
            f"{query(DROP_COLUMN)}"
        )
        assert severities(source) == [Severity.NONE, Severity.BREAKING]

    def test_a_brace_inside_a_string_literal_does_not_close_the_body(self):
        source = migration(f'const suffix = "}}";\n{query(DROP_COLUMN)}')
        assert severities(source) == [Severity.BREAKING]

    def test_a_brace_inside_a_line_comment_does_not_close_the_body(self):
        source = migration(f"// fecha aqui? }} não\n{query(DROP_COLUMN)}")
        assert severities(source) == [Severity.BREAKING]

    def test_a_brace_inside_a_block_comment_does_not_close_the_body(self):
        source = migration(f"/* }} down( */\n{query(DROP_COLUMN)}")
        assert severities(source) == [Severity.BREAKING]

    def test_an_unterminated_string_does_not_swallow_the_rest_of_the_body(self):
        # Aspa que não fecha vale até o fim da linha, como no JavaScript. Sem
        # isso ela engoliria até a próxima aspa do arquivo, que é uma aspa
        # dentro do DDL — e o statement inteiro sumiria da leitura.
        source = migration(f'const label = "não fecha;\n{query(DROP_COLUMN)}')
        assert severities(source) == [Severity.BREAKING]

    def test_an_escaped_quote_does_not_close_the_string(self):
        source = migration(f'const label = "a \\" }}";\n{query(DROP_COLUMN)}')
        assert severities(source) == [Severity.BREAKING]

    def test_a_brace_inside_an_interpolation_does_not_close_the_body(self):
        source = migration(
            "await queryRunner.query(`SELECT ${JSON.stringify({ a: 1 })}`);\n"
            f"{query(DROP_COLUMN)}"
        )
        assert severities(source) == [Severity.UNKNOWN, Severity.BREAKING]

    def test_a_comment_mentioning_down_does_not_start_the_rollback(self):
        source = migration(
            f"// o inverso disso mora em down(queryRunner)\n{query(CREATE)}",
            down=query('DROP TABLE "schedule"'),
        )
        assert severities(source) == [Severity.SAFE]

    def test_a_nested_block_in_the_body_does_not_close_the_body(self):
        source = migration(f"if (true) {{\n    {query(CREATE)}\n}}\n{query(DROP_COLUMN)}")
        assert severities(source) == [Severity.SAFE, Severity.BREAKING]

    def test_an_escaped_backtick_does_not_close_the_template(self):
        source = migration(f"await queryRunner.query(`SELECT \\` }}`);\n{query(DROP_COLUMN)}")
        assert severities(source)[-1] == Severity.BREAKING

    def test_a_nested_template_inside_interpolation_does_not_close_the_template(self):
        source = migration(
            "await queryRunner.query(`ALTER TABLE ${`schedule`} DROP COLUMN \"date\"`);\n"
            f"{query(CREATE)}"
        )
        assert severities(source) == [Severity.UNKNOWN, Severity.SAFE]

    def test_a_query_call_inside_an_interpolation_is_not_read_as_literal(self):
        # Crase dentro de crase: a de dentro está sendo montada junto com o
        # texto de fora, e se ela roda é coisa que o parser não sabe. Ler o DDL
        # de dentro seria afirmar uma execução que não está escrita.
        source = migration(
            "await queryRunner.query(`SELECT ${await qr.query(`"
            'DROP TABLE "schedule"`)}`);'
        )
        assert severities(source) == [Severity.UNKNOWN, Severity.UNKNOWN]

    def test_a_statement_after_an_unreadable_one_is_still_classified(self):
        source = migration(f"await queryRunner.query(sql);\n{query(DROP_COLUMN)}")
        assert severities(source) == [Severity.UNKNOWN, Severity.BREAKING]


class TestUpThatCannotBeRead:
    def test_up_defined_twice_is_unknown(self):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(CREATE)}\n"
            "    }\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["ambiguous_up"].reason

    def test_a_body_that_never_closes_is_unknown(self):
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["unterminated_up"].reason

    def test_a_parameter_list_that_never_closes_is_unknown(self):
        # Arquivo truncado dentro do cabeçalho é a mesma coisa que arquivo
        # truncado dentro do corpo, e tem que dar a mesma resposta — uma lista
        # vazia aqui se leria como "não é migração".
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async up(queryRunner: QueryRunner"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["unterminated_up"].reason

    def test_an_unterminated_body_does_not_report_the_statements_it_did_not_delimit(self):
        # O `DROP COLUMN` está lá dentro, mas o parser não sabe onde o corpo
        # acaba — e "não sei ler" não pode sair como uma leitura.
        source = (
            f"{HEAD}\n"
            "export class AutoMigrate1 implements MigrationInterface {\n"
            "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
            f"        {query(DROP_COLUMN)}\n"
        )
        assert severities(source) == [Severity.UNKNOWN]


# ---------------------------------------------------------------------------
# SQL que o parser não consegue ler
# ---------------------------------------------------------------------------


class TestSqlThatIsNotLiteral:
    def test_an_interpolated_template_is_unknown(self):
        finding = only(migration("await queryRunner.query(`DROP TABLE ${table}`);"))
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["dynamic_sql"].reason

    def test_an_interpolated_template_does_not_classify_the_part_it_can_read(self):
        # `DROP TABLE ${t}` tem cara de breaking e `SELECT ${x}` não tem cara de
        # nada; as duas são a mesma resposta, porque o que roda não está escrito.
        assert severities(migration("await queryRunner.query(`SELECT ${x}`);")) == [
            Severity.UNKNOWN
        ]

    def test_a_variable_argument_is_unknown(self):
        finding = only(migration("await queryRunner.query(sql);"))
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["dynamic_sql"].reason

    def test_a_concatenated_string_argument_is_unknown(self):
        assert severities(migration('await queryRunner.query("DROP TABLE " + table);')) == [
            Severity.UNKNOWN
        ]

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_a_quoted_string_argument_is_unknown(self, quote):
        # Só template literal é lido. Uma string com aspas é SQL do mesmo jeito,
        # mas o corpus não tem nenhuma e adivinhar o formato não é ler.
        finding = only(migration(f"await queryRunner.query({quote}DROP TABLE t{quote});"))
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["unread_sql"].reason

    def test_the_quoted_string_reason_does_not_call_a_static_literal_dynamic(self):
        # A razão do `unread_sql` existe porque `dynamic_sql` seria falsa aqui:
        # o SQL está escrito inteiro no arquivo, só não está entre crases.
        assert "dinamicamente" not in _GATES["unread_sql"].reason

    def test_a_template_followed_by_a_concatenation_is_unknown(self):
        # O que roda pode ser `... NOT NULL`, que o `sql.py` classifica
        # diferente do que está entre crases. Ler só o pedaço escrito seria
        # afirmar o que a concatenação desmente.
        source = migration(
            'await queryRunner.query(`ALTER TABLE "schedule" ADD "c" character varying`'
            " + notNull);"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["dynamic_sql"].reason

    def test_a_template_followed_by_a_method_call_is_unknown(self):
        source = migration(
            'await queryRunner.query(`ALTER TABLE "schedule" ADD "c" ${type}`'
            ".replace(a, b));"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_template_after_a_prefix_is_unknown(self):
        source = migration('await queryRunner.query(prefix + `ADD "c" character varying`);')
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_whole_template_with_a_second_argument_is_read(self):
        # O contrário do teste acima: a vírgula depois da crase é o que separa
        # "o argumento acabou" de "o argumento continua".
        assert severities(migration(f'await queryRunner.query(`{DROP_COLUMN}`, [])')) == [
            Severity.BREAKING
        ]

    def test_a_query_call_without_sql_does_not_alter_the_schema(self):
        finding = only(migration("await queryRunner.query();"))
        assert finding.severity is Severity.NONE
        assert finding.reason == _GATES["empty_sql"].reason

    def test_an_empty_template_does_not_alter_the_schema(self):
        assert severities(migration("await queryRunner.query(``);")) == [Severity.NONE]

    def test_a_template_with_only_a_comment_does_not_alter_the_schema(self):
        assert severities(migration("await queryRunner.query(`-- nada aqui`);")) == [
            Severity.NONE
        ]

    def test_the_dynamic_sql_reason_is_the_one_the_alembic_parser_gives(self):
        # Mesma situação nas duas stacks: o SQL não está escrito no arquivo. O
        # time de dados lê a mesma frase no Slack.
        alembic = classify_alembic(
            "from alembic import op\n\n\ndef upgrade():\n    op.execute(sql)\n"
        )
        assert [finding.reason for finding in alembic] == [_GATES["dynamic_sql"].reason]


class TestQueryCallForms:
    def test_the_second_argument_of_the_generated_form_is_ignored(self):
        source = migration(f"await queryRunner.query(`{DROP_COLUMN}`, undefined);")
        assert severities(source) == [Severity.BREAKING]

    def test_a_multiline_call_is_read(self):
        source = migration(
            "await queryRunner.query(\n"
            f"    `{DROP_COLUMN}`,\n"
            "    undefined,\n"
            ");"
        )
        assert severities(source) == [Severity.BREAKING]

    def test_a_receiver_that_is_not_queryrunner_is_read(self):
        source = migration(f"await runner.query(`{DROP_COLUMN}`);")
        assert severities(source) == [Severity.BREAKING]

    def test_a_template_that_is_not_a_query_argument_is_not_read(self):
        # Limite conhecido e deliberado: fora do argumento de uma `.query()`,
        # template é texto qualquer. Ler todos punha texto livre da migração na
        # razão que vai para o Slack e subia para `unknown` arquivo lido
        # inteiro por causa de um `console.log`.
        source = migration(f"await this.run(`{DROP_COLUMN}`);")
        assert classify_migration(source) == []

    def test_a_statement_inside_a_loop_is_read(self):
        source = migration(f"for (const t of tables) {{\n    {query(DROP_COLUMN)}\n}}")
        assert severities(source) == [Severity.BREAKING]

    def test_a_call_in_down_with_sql_that_is_not_literal_is_not_reported(self):
        # O portão de SQL dinâmico vale dentro de `up()`. Ligado no arquivo
        # inteiro, todo rollback com SQL montado viraria um `unknown` de uma
        # migração que o parser leu inteira.
        source = migration(query(CREATE), down="await queryRunner.query(sql);")
        assert severities(source) == [Severity.SAFE]

    def test_two_statements_in_one_call_yield_two_findings(self):
        source = migration(query(f"{CREATE}; {DROP_COLUMN}"))
        assert severities(source) == [Severity.SAFE, Severity.BREAKING]


# ---------------------------------------------------------------------------
# Delegação para detect/sql.py
# ---------------------------------------------------------------------------


CORPUS_DDL = [
    # Os verbos que a QQ-2153 contou nos 34 arquivos, um exemplo de cada.
    'CREATE TABLE "score" ("ratingId" SERIAL NOT NULL)',
    'ALTER TABLE "schedule" ADD "suspended" boolean NOT NULL DEFAULT false',
    'ALTER TABLE "schedule" DROP COLUMN "date"',
    'ALTER TABLE "schedule" ADD CONSTRAINT "FK_1" FOREIGN KEY ("id") REFERENCES "s"("id")',
    'ALTER TABLE "schedule" DROP CONSTRAINT "FK_1"',
    'ALTER TABLE "schedule" ALTER COLUMN "date" SET DEFAULT now()',
    'ALTER TABLE "schedule" RENAME COLUMN "date" TO "startDate"',
    'ALTER TABLE "schedule" RENAME TO "appointment"',
    'CREATE INDEX "IDX_1" ON "schedule" ("tenantId")',
    'CREATE UNIQUE INDEX "IDX_2" ON "schedule" ("tenantId")',
    'DROP INDEX "IDX_1"',
    'CREATE TYPE "schedule_status_enum" AS ENUM(\'open\')',
    'ALTER TYPE "schedule_status_enum" RENAME TO "schedule_status_enum_old"',
    'DROP TYPE "schedule_status_enum_old"',
    'CREATE SEQUENCE "score_id_seq"',
]


class TestDelegationToSql:
    @pytest.mark.parametrize("sql", CORPUS_DDL)
    def test_the_finding_is_the_one_the_sql_classifier_gives(self, sql):
        # A frase e a severidade saem inteiras de `detect/sql.py`: o mesmo DDL
        # tem que sair igual no TypeORM, no Django e no Alembic.
        expected = classify_statement(sql)
        assert one(sql) == expected

    def test_a_statement_the_sql_classifier_does_not_recognise_is_unknown(self):
        finding = one("VACUUM FULL")
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason.endswith(MANUAL)

    def test_an_unrecognised_statement_does_not_take_down_the_others(self):
        source = migration(f"{query('VACUUM FULL')}\n{query(CREATE)}\n{query(DROP_COLUMN)}")
        assert severities(source) == [Severity.UNKNOWN, Severity.SAFE, Severity.BREAKING]

    def test_the_file_severity_is_the_worst_of_its_statements(self):
        source = migration(f"{query(CREATE)}\n{query(ADD_COLUMN)}\n{query(DROP_COLUMN)}")
        assert max_severity(severities(source)) is Severity.BREAKING

    def test_an_unknown_statement_outranks_the_benign_ones(self):
        source = migration(f"{query(CREATE)}\n{query('VACUUM FULL')}")
        assert max_severity(severities(source)) is Severity.UNKNOWN


# ---------------------------------------------------------------------------
# As duas populações do corpus
# ---------------------------------------------------------------------------

GENERATED = (
    'import {MigrationInterface, QueryRunner} from "typeorm";\n'
    "\n"
    "export class AutoMigrate1577725059548 implements MigrationInterface {\n"
    "\n"
    "    public async up(queryRunner: QueryRunner): Promise<any> {\n"
    '        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "date"`, undefined);\n'
    '        await queryRunner.query(`ALTER TABLE "schedule" ADD "startDate" TIMESTAMP'
    ' NOT NULL`, undefined);\n'
    "    }\n"
    "\n"
    "    public async down(queryRunner: QueryRunner): Promise<any> {\n"
    '        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "startDate"`,'
    " undefined);\n"
    '        await queryRunner.query(`ALTER TABLE "schedule" ADD "date" TIMESTAMP NOT NULL`,'
    " undefined);\n"
    "    }\n"
    "\n"
    "}\n"
)

HANDWRITTEN = (
    "import { MigrationInterface, QueryRunner } from 'typeorm';\n"
    "\n"
    "export class AutoMigrate1700000000012 implements MigrationInterface {\n"
    "    name = 'AutoMigrate1700000000012'\n"
    "\n"
    "    public async up(queryRunner: QueryRunner): Promise<void> {\n"
    '        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "date"`);\n'
    '        await queryRunner.query(`ALTER TABLE "schedule" ADD "startDate" TIMESTAMP'
    ' NOT NULL`);\n'
    "    }\n"
    "\n"
    "    public async down(queryRunner: QueryRunner): Promise<void> {\n"
    '        await queryRunner.query(`ALTER TABLE "schedule" DROP COLUMN "startDate"`);\n'
    '        await queryRunner.query(`ALTER TABLE "schedule" ADD "date" TIMESTAMP NOT NULL`);\n'
    "    }\n"
    "\n"
    "}\n"
)


class TestCorpusShapes:
    """As duas formatações reais têm que sair idênticas.

    Foi a diferença entre elas — 23 arquivos gerados e 11 escritos à mão — que
    o glob da QQ-2153 tratava de forma diferente. Se o isolamento de `up()` só
    funcionasse na primeira, o defeito voltaria aqui dentro.
    """

    @pytest.mark.parametrize("source", [GENERATED, HANDWRITTEN], ids=["gerado", "à mão"])
    def test_both_populations_read_the_same_two_statements(self, source):
        findings = classify_migration(source)
        assert [finding.operation for finding in findings] == ["DROP COLUMN", "ADD COLUMN"]
        assert findings[0].severity is Severity.BREAKING
        assert findings[1].severity is Severity.CONTROLLED

    def test_both_populations_yield_the_same_findings(self):
        assert classify_migration(GENERATED) == classify_migration(HANDWRITTEN)

    @pytest.mark.parametrize("source", [GENERATED, HANDWRITTEN], ids=["gerado", "à mão"])
    def test_neither_population_reads_the_rollback(self, source):
        # O `down()` das duas devolve a coluna `date`, que o `up()` remove. Uma
        # coluna `date` adicionada na saída significa que o corpo errado foi
        # lido — e é a coluna, não a severidade, que denuncia.
        added = [
            finding for finding in classify_migration(source) if finding.operation == "ADD COLUMN"
        ]
        assert [finding.reason for finding in added] == [
            "Coluna `startDate` adicionada em `schedule` como NOT NULL sem DEFAULT — "
            "tabela que já tem linha impede a migração."
        ]


# ---------------------------------------------------------------------------
# Forma dos findings e invariantes da tabela
# ---------------------------------------------------------------------------

EVERY_KIND_OF_MIGRATION = [
    migration(query(CREATE)),
    migration(query(DROP_COLUMN)),
    migration(query("VACUUM FULL")),
    migration("await queryRunner.query(`DROP TABLE ${table}`);"),
    migration("await queryRunner.query(sql);"),
    migration('await queryRunner.query("DROP TABLE t");'),
    migration("await queryRunner.query();"),
    f"{HEAD}class M {{ up() {{}} up() {{}} }}\n",
    f"{HEAD}class M {{ up() {{ {query(CREATE)}\n",
]

# Uma fonte por linha da tabela, para provar que cada uma é alcançável.
GATE_SOURCES = {
    "dynamic_sql": migration("await queryRunner.query(`DROP TABLE ${table}`);"),
    "unread_sql": migration('await queryRunner.query("DROP TABLE t");'),
    "empty_sql": migration("await queryRunner.query();"),
    "ambiguous_up": f"{HEAD}class M {{ up() {{}} up() {{}} }}\n",
    "unterminated_up": f"{HEAD}class M {{ up() {{ {query(CREATE)}\n",
}


class TestGateTable:
    @pytest.mark.parametrize("name", sorted(_GATES))
    def test_every_reason_in_the_table_is_a_sentence(self, name):
        reason = _GATES[name].reason
        assert reason[0].isupper(), (name, reason)
        assert reason.endswith("."), (name, reason)

    @pytest.mark.parametrize("name", sorted(_GATES))
    def test_every_unknown_row_ends_with_the_manual_review_sentence(self, name):
        gate = _GATES[name]
        if gate.severity is Severity.UNKNOWN:
            assert gate.reason.endswith(MANUAL), (name, gate.reason)

    @pytest.mark.parametrize("name", sorted(_GATES))
    def test_every_row_in_the_table_is_reachable(self, name):
        assert name in GATE_SOURCES, name
        findings = classify_migration(GATE_SOURCES[name])
        assert _GATES[name].reason in [finding.reason for finding in findings], name

    def test_no_source_claims_a_row_that_left_the_table(self):
        assert set(GATE_SOURCES) == set(_GATES)

    def test_every_gate_the_module_defines_is_in_the_table(self):
        # A tabela é o artefato que a QQ-2162 leva ao time de dados. Um portão
        # criado ao lado dela sairia no Slack sem estar na tabela.
        defined = {
            value for value in vars(typeorm).values() if isinstance(value, typeorm._Gate)
        }
        assert defined == set(_GATES.values())

    @pytest.mark.parametrize("name", sorted(_GATES))
    def test_every_row_carries_an_operation(self, name):
        assert _GATES[name].operation


class TestFindingShape:
    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_every_finding_has_an_operation_and_a_reason(self, source):
        findings = classify_migration(source)
        assert findings, source
        for finding in findings:
            assert finding.operation, source
            assert finding.reason, source

    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_every_reason_is_a_portuguese_sentence(self, source):
        for finding in classify_migration(source):
            assert finding.reason[0].isupper(), finding.reason
            assert finding.reason.endswith("."), finding.reason

    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_no_finding_carries_a_path_or_a_line_number(self, source):
        # `Finding` descreve um statement, não um arquivo — juntar com o
        # caminho é trabalho da QQ-2160.
        for finding in classify_migration(source):
            assert not hasattr(finding, "path")
            assert not hasattr(finding, "line")

    def test_a_file_that_is_not_typescript_at_all_yields_no_findings(self):
        assert classify_migration("") == []
        assert classify_migration("não é código nenhum") == []


class TestReasonsNeverLeakLiterals:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO subjects (cpf) VALUES ('12345678900')",
            "UPDATE subjects SET cpf = '12345678900'",
            "ALTER TABLE subjects ALTER COLUMN cpf SET DEFAULT '12345678900'",
        ],
    )
    def test_no_reason_echoes_a_value_from_the_migration(self, sql):
        findings = classify_migration(migration(query(sql)))
        assert findings, sql
        for finding in findings:
            assert "12345678900" not in finding.reason, finding.reason

    def test_no_reason_echoes_an_interpolated_expression(self):
        source = migration("await queryRunner.query(`SELECT ${subject.cpf}`);")
        findings = classify_migration(source)
        assert findings, source
        for finding in findings:
            assert "cpf" not in finding.reason, finding.reason

    @pytest.mark.parametrize(
        "statement",
        [
            "this.logger.info(`enviando cpf 12345678900 ao parceiro`);",
            "const mensagem = `paciente 12345678900 migrado`;",
        ],
    )
    def test_a_template_that_is_not_a_query_argument_never_reaches_a_reason(self, statement):
        # O `_echo` do `sql.py` corta no primeiro literal entre aspas, e é o que
        # segura o vazamento nas outras duas stacks — mas lá ele só recebe o
        # argumento de `execute`/`RunSQL`. Mandar texto livre do arquivo para
        # ele reabria o vazamento por outro lado.
        assert classify_migration(migration(statement)) == []

    def test_an_unrecognised_statement_does_not_echo_an_unquoted_value(self):
        # Chega pelo argumento de uma `.query()`, então é SQL de verdade e vira
        # `unknown` — mas o número não pode ir junto para o Slack. Quem corta é
        # o `_echo` do `detect/sql.py`, para as três stacks.
        finding = only(migration(query("CALL migrate_subject(12345678900)")))
        assert finding.severity is Severity.UNKNOWN
        assert "12345678900" not in finding.reason

    def test_a_log_call_does_not_raise_the_severity_of_a_file_that_was_read(self):
        source = migration(f"console.log(`migrando schedule`);\n{query(CREATE)}")
        assert severities(source) == [Severity.SAFE]
