"""Testes do parser de migrações do Doctrine Migrations, em PHP.

As migrações usadas aqui foram derivadas do corpus real do consumidor: 18
arquivos, divididos em **dois diretórios, um por banco** — 7 no MySQL, que
guarda o financeiro do SaaS, e 11 no PostgreSQL, que guarda o resto do sistema.
Nenhum arquivo do corpus é copiado para cá: `iclinic/automations` é público, e a
suíte de fixtures é da QQ-2161.

O corpus real mediu 40 `addSql()` com string de aspa simples, 3 com nowdoc e
**nenhum** heredoc interpolado. As três primeiras formas estão em
`TestTheFourStringSyntaxes` porque são as que existem; a quarta está porque a
diferença entre nowdoc e heredoc é uma aspa no rótulo, e é ela que separa "o SQL
está escrito" de "o SQL é montado em tempo de execução".

O que o corpus real **não** tem, e que este módulo precisa cobrir mesmo assim:
`DROP TABLE`, `DROP COLUMN` e a passagem de coluna para NOT NULL dentro de
`up()`. É o mesmo achado da QQ-2161 no Alembic — as três operações mais
destrutivas do vocabulário viviam só no `down()`, que o parser ignora por
especificação, então as regras que as classificam nunca eram alcançadas por dado
real. `TestDestructionInUp` é o par sintético dessas regras, nos dois dialetos.
"""

import re
import textwrap
from pathlib import Path

import pytest

from detect import doctrine
from detect.alembic import classify_migration as classify_alembic
from detect.severity import MANUAL, Severity, max_severity
from detect.doctrine import _GATES, classify_migration, declares_migration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HEAD = """\
<?php

declare(strict_types=1);

namespace Migrations;

use Doctrine\\DBAL\\Schema\\Schema;
use Doctrine\\Migrations\\AbstractMigration;
"""


def migration(up: str, down: str = "", head: str = HEAD) -> str:
    """Um arquivo de migração do Doctrine com `up` e `down` no corpo da classe.

    A chave do corpo na linha seguinte à assinatura é PSR-12, que é como o
    consumidor escreve — e é a formatação que faz `_open_brace` ter que pular a
    anotação de tipo de retorno em vez de casar a chave logo depois do
    parêntese.
    """
    return (
        f"{head}\n"
        "final class Version20250101120000 extends AbstractMigration\n"
        "{\n"
        "    public function up(Schema $schema): void\n"
        "    {\n"
        f"{textwrap.indent(textwrap.dedent(up).strip(), ' ' * 8)}\n"
        "    }\n"
        "\n"
        "    public function down(Schema $schema): void\n"
        "    {\n"
        f"{textwrap.indent(textwrap.dedent(down).strip(), ' ' * 8)}\n"
        "    }\n"
        "}\n"
    )


def add_sql(sql: str) -> str:
    """Uma chamada `$this->addSql()` com o SQL numa string de aspa simples."""
    return f"$this->addSql('{sql}');"


def severities(source: str) -> list[Severity]:
    return [finding.severity for finding in classify_migration(source)]


def only(source: str):
    """O único finding da migração — falha se houver mais de um."""
    findings = classify_migration(source)
    assert len(findings) == 1, findings
    return findings[0]


def one(sql: str):
    """O único finding de uma migração cujo `up()` roda um SQL só."""
    return only(migration(add_sql(sql)))


# O vocabulário sintético deste módulo. Verificado ausente do clone do consumidor
# antes de ser usado — `tests/test_pii.py` reconfere a cada execução comparando o
# vocabulário de posição de schema dos dois lados.
CREATE = "CREATE TABLE freight_note (id INT UNSIGNED NOT NULL, carrier_code VARCHAR(40) NULL)"
ADD_COLUMN = "ALTER TABLE freight_note ADD COLUMN settled TINYINT(1) NULL"
DROP_COLUMN = "ALTER TABLE freight_note DROP COLUMN settled"
DROP_TABLE = "DROP TABLE freight_note"

# As duas formas do mesmo risco, uma por dialeto. É o par que o cartão pede: a
# mesma mudança nas duas bases do consumidor tem que sair com a mesma cor.
MYSQL_NOT_NULL = "ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) NOT NULL"
PGSQL_NOT_NULL = 'ALTER TABLE stock."freight_note" ALTER COLUMN carrier_code SET NOT NULL'


# ---------------------------------------------------------------------------
# Isolar up() de down()
# ---------------------------------------------------------------------------


class TestUpBody:
    def test_returns_one_finding_per_statement_in_order(self):
        source = migration(f"{add_sql(CREATE)}\n{add_sql(DROP_COLUMN)}")
        findings = classify_migration(source)
        assert [f.operation for f in findings] == ["CREATE TABLE", "DROP COLUMN"]
        assert findings[0].reason == "Tabela `freight_note` criada."
        assert findings[1].reason == (
            "Coluna `settled` removida de `freight_note` — quem lê essa coluna quebra."
        )

    def test_down_is_ignored(self):
        source = migration(add_sql(CREATE), down=add_sql(DROP_TABLE))
        assert severities(source) == [Severity.SAFE]

    def test_down_declared_before_up_is_still_ignored(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function down(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(DROP_TABLE)}\n"
            "    }\n"
            "\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(CREATE)}\n"
            "    }\n"
            "}\n"
        )
        assert severities(source) == [Severity.SAFE]

    def test_a_file_without_up_yields_no_findings(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function down(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(DROP_TABLE)}\n"
            "    }\n"
            "}\n"
        )
        assert classify_migration(source) == []

    def test_an_empty_up_yields_no_findings(self):
        assert classify_migration(migration("")) == []

    def test_a_method_whose_name_only_starts_with_up_is_not_up(self):
        source = migration("").replace("function up(", "function upgrade(")
        assert classify_migration(source) == []

    def test_a_method_whose_name_only_ends_with_up_is_not_up(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function cleanup(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert classify_migration(source) == []

    def test_a_method_named_up_on_another_object_is_not_the_definition(self):
        # `$this->up()` dentro de `down()` é chamada, não definição. O que
        # rejeita é a palavra `function` faltando antes do nome.
        source = migration(add_sql(CREATE), down="$this->up($schema);")
        assert severities(source) == [Severity.SAFE]


class TestUpThatIsNotADefinitionWithABody:
    """As formas de `function up` aparecer sem trazer um corpo.

    O parser não resolve escopo: ele acha `function up` e decide pelo que vem
    depois. Cada fonte aqui põe um bloco no caminho, e é esse bloco que o parser
    erraria em adotar — o erro sairia como uma segunda definição de `up`, que
    vira `unknown` e apaga a classificação que estava certa.
    """

    def test_an_abstract_declaration_without_a_body_is_not_a_definition(self):
        source = (
            f"{HEAD}\n"
            "abstract class BaseMigration extends AbstractMigration\n"
            "{\n"
            "    abstract public function up(Schema $schema): void;\n"
            "\n"
            "    public function noop(): void\n"
            "    {\n"
            f"        {add_sql(DROP_TABLE)}\n"
            "    }\n"
            "}\n"
        )
        # Nenhuma definição de `up` com corpo: nada a dizer, e não um `unknown`
        # nem o corpo de `noop()` lido no lugar.
        assert classify_migration(source) == []

    def test_an_interface_declaration_does_not_swallow_the_real_up(self):
        # A declaração sem corpo não vira uma segunda definição: se virasse, o
        # arquivo sairia `unknown` por `ambiguous_up` em vez de classificado.
        source = (
            f"{HEAD}\n"
            "interface Reversible\n"
            "{\n"
            "    public function up(Schema $schema): void;\n"
            "}\n"
            "\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(CREATE)}\n"
            "    }\n"
            "}\n"
        )
        assert severities(source) == [Severity.SAFE]

    def test_the_word_up_in_a_comment_is_not_a_definition(self):
        source = migration(
            add_sql(CREATE),
            down="// function up(Schema $schema): void { drop everything }\n"
            + add_sql(DROP_TABLE),
        )
        assert severities(source) == [Severity.SAFE]

    def test_the_word_up_inside_a_string_is_not_a_definition(self):
        source = migration(
            add_sql(CREATE),
            down="$note = 'function up(Schema $schema): void {';\n" + add_sql(DROP_TABLE),
        )
        assert severities(source) == [Severity.SAFE]


class TestBodyDelimitation:
    def test_a_nested_block_in_the_body_does_not_end_it(self):
        source = migration(
            "if ($schema->hasTable('freight_note')) {\n"
            f"    {add_sql(DROP_COLUMN)}\n"
            "}\n"
            f"{add_sql(ADD_COLUMN)}"
        )
        assert [f.operation for f in classify_migration(source)] == [
            "DROP COLUMN",
            "ADD COLUMN",
        ]

    def test_a_brace_inside_a_string_does_not_close_the_body(self):
        # A chave dentro do literal foi apagada pelo scanner, então o corpo
        # continua até a chave de verdade. Sem isso, o `DROP COLUMN` de baixo
        # cairia fora do corpo lido e sumiria.
        source = migration(
            "$this->addSql('COMMENT ON TABLE freight_note IS ' || 'x}y');\n"
            f"{add_sql(DROP_COLUMN)}"
        )
        assert Severity.BREAKING in severities(source)

    def test_a_brace_inside_a_nowdoc_does_not_close_the_body(self):
        source = migration(
            "$this->addSql(<<<'SQL'\n"
            "CREATE FUNCTION noop() RETURNS void AS $body$ BEGIN { } END $body$ LANGUAGE plpgsql\n"
            "SQL);\n"
            f"{add_sql(DROP_COLUMN)}"
        )
        assert Severity.BREAKING in severities(source)

    def test_a_body_that_never_closes_is_unknown(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(DROP_COLUMN)}\n"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["unterminated_up"].reason

    def test_a_parameter_list_that_never_closes_is_unknown(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema\n"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["unterminated_up"].reason

    def test_two_definitions_of_up_are_unknown_and_not_the_first_one(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(CREATE)}\n"
            "    }\n"
            "}\n"
            "\n"
            "final class Version20250101120000Legacy extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            f"        {add_sql(DROP_TABLE)}\n"
            "    }\n"
            "}\n"
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["ambiguous_up"].reason
        # Pareado: nem o `safe` da primeira nem o `breaking` da segunda saem no
        # lugar. Um parser que escolhesse a primeira passaria no `unknown`
        # acima só se a asserção fosse por severidade.
        assert "Tabela" not in finding.reason


# ---------------------------------------------------------------------------
# As quatro sintaxes de string do PHP
# ---------------------------------------------------------------------------


class TestTheFourStringSyntaxes:
    """Duas leem, duas não, e a regra é a forma do delimitador.

    `'...'` e `<<<'SQL'` não interpolam: o que está escrito é o que roda.
    `"..."` e `<<<SQL` interpolam, então o SQL que roda pode não ser o que está
    escrito — e classificar a parte legível seria afirmar o que a interpolação
    pode desmentir.
    """

    def test_a_single_quoted_string_is_read(self):
        assert one(DROP_COLUMN).severity is Severity.BREAKING

    def test_a_nowdoc_is_read(self):
        source = migration("$this->addSql(<<<'SQL'\n" f"{DROP_COLUMN}\n" "SQL);")
        assert severities(source) == [Severity.BREAKING]

    def test_a_double_quoted_string_is_unknown(self):
        source = migration(f'$this->addSql("{DROP_COLUMN}");')
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["interpolating_sql"].reason

    def test_an_interpolating_heredoc_is_unknown(self):
        source = migration("$this->addSql(<<<SQL\n" f"{DROP_COLUMN}\n" "SQL);")
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["interpolating_sql"].reason

    def test_a_heredoc_with_a_double_quoted_label_is_unknown(self):
        source = migration('$this->addSql(<<<"SQL"\n' f"{DROP_COLUMN}\n" "SQL);")
        assert only(source).reason == _GATES["interpolating_sql"].reason

    def test_the_interpolating_form_never_gets_the_classification_of_what_is_written(self):
        """O par que dá dente ao teste de cima.

        As quatro fontes têm o **mesmo** SQL dentro. As duas literais saem
        `breaking`; as duas que interpolam saem `unknown`. Se o parser lesse o
        conteúdo do heredoc, as quatro sairiam iguais e os testes acima
        continuariam verdes se afirmassem só a severidade.
        """
        literal = [
            migration(add_sql(DROP_COLUMN)),
            migration("$this->addSql(<<<'SQL'\n" f"{DROP_COLUMN}\n" "SQL);"),
        ]
        interpolating = [
            migration(f'$this->addSql("{DROP_COLUMN}");'),
            migration("$this->addSql(<<<SQL\n" f"{DROP_COLUMN}\n" "SQL);"),
        ]
        assert [severities(s) for s in literal] == [[Severity.BREAKING]] * 2
        assert [severities(s) for s in interpolating] == [[Severity.UNKNOWN]] * 2

    def test_the_reason_for_interpolating_does_not_claim_the_sql_is_dynamic(self):
        # As duas linhas dizem coisas diferentes e não podem convergir: uma é
        # "o autor escolheu a forma que interpola", a outra é "não há literal
        # nenhum aqui".
        assert "dinamicamente" not in _GATES["interpolating_sql"].reason
        assert _GATES["interpolating_sql"].reason != _GATES["dynamic_sql"].reason

    def test_no_reason_of_this_module_carries_a_raw_quote(self):
        # As duas sintaxes literais têm aspa no nome, e escrevê-las na razão
        # punha aspa crua numa frase que vai para o Slack. `tests/test_pii.py`
        # guarda a mesma fronteira sobre o corpus; aqui ela é sobre a tabela.
        for name, gate in sorted(_GATES.items()):
            assert "'" not in gate.reason, name
            assert '"' not in gate.reason, name


class TestSingleQuotedEscapes:
    """Na aspa simples do PHP só `\\'` e `\\\\` são escapes. O resto é literal."""

    def test_an_escaped_quote_does_not_end_the_string(self):
        # A forma que o corpus real escreve um default de char:
        # `status char(1) NOT NULL DEFAULT \\'0\\'`.
        source = migration(
            r"$this->addSql('ALTER TABLE freight_note ADD COLUMN settled "
            r"char(1) NOT NULL DEFAULT \'0\'');"
        )
        finding = only(source)
        assert finding.operation == "ADD COLUMN"
        assert finding.reason.startswith("Coluna `settled` adicionada em `freight_note`")

    def test_an_escaped_backslash_does_not_escape_the_closing_quote(self):
        source = migration(r"$this->addSql('COMMENT ON TABLE freight_note IS \\');" "\n" + add_sql(DROP_COLUMN))
        # O segundo `addSql` só é encontrado se a primeira string tiver fechado
        # no lugar certo.
        assert Severity.BREAKING in severities(source)

    def test_a_backslash_n_in_a_single_quoted_string_is_two_characters(self):
        # Não é quebra de linha, então não separa statements. Um `\n` decodificado
        # aqui faria o statement virar dois.
        source = migration(r"$this->addSql('DROP TABLE freight_note \n');")
        assert [f.operation for f in classify_migration(source)] == ["DROP TABLE"]


class TestNowdocDelimitation:
    def test_the_closing_label_may_be_followed_by_the_call_closing(self):
        # `SQL);` na mesma linha é como o corpus real fecha os três nowdocs dele.
        source = migration("$this->addSql(<<<'SQL'\n" f"{DROP_TABLE}\n" "SQL);")
        assert [f.operation for f in classify_migration(source)] == ["DROP TABLE"]

    def test_an_indented_closing_label_dedents_the_body(self):
        source = migration(
            "$this->addSql(<<<'SQL'\n"
            f"            {DROP_COLUMN}\n"
            "            SQL);"
        )
        assert severities(source) == [Severity.BREAKING]

    def test_a_label_like_word_inside_the_body_does_not_close_the_nowdoc(self):
        # O rótulo fecha só quando abre a linha e **não é seguido por caractere
        # de identificador** — é a regra do PHP 7.3+, e é a que o scanner
        # aplica. Fechando cedo em `SQLSTATE_NOTE`, o `DROP TABLE` ficaria fora
        # do literal e o arquivo sairia sem ele.
        source = migration(
            "$this->addSql(<<<'SQL'\n"
            "SQLSTATE_NOTE;\n"
            f"{DROP_TABLE}\n"
            "SQL);"
        )
        assert [f.operation for f in classify_migration(source)] == ["?", "DROP TABLE"]

    def test_a_nowdoc_that_never_closes_makes_the_body_unreadable(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            "        $this->addSql(<<<'SQL'\n"
            f"{DROP_COLUMN}\n"
        )
        assert only(source).reason == _GATES["unterminated_up"].reason

    def test_an_empty_nowdoc_is_a_call_with_nothing_to_run(self):
        source = migration("$this->addSql(<<<'SQL'\nSQL);")
        finding = only(source)
        assert finding.severity is Severity.NONE
        assert finding.reason == _GATES["empty_sql"].reason


# ---------------------------------------------------------------------------
# O argumento que não é um literal inteiro
# ---------------------------------------------------------------------------


class TestSqlThatIsNotALiteral:
    def test_a_variable_is_unknown(self):
        source = migration("$sql = 'DROP TABLE freight_note';\n$this->addSql($sql);")
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert finding.reason == _GATES["dynamic_sql"].reason

    def test_a_concatenation_is_unknown_even_with_the_literal_first(self):
        source = migration(
            "$this->addSql('ALTER TABLE freight_note MODIFY carrier_code VARCHAR(40) ' "
            ". $nullability);"
        )
        assert only(source).reason == _GATES["dynamic_sql"].reason

    def test_a_method_call_on_the_literal_is_unknown(self):
        source = migration(
            "$this->addSql(str_replace('%t', 'freight_note', 'DROP TABLE %t'));"
        )
        assert only(source).reason == _GATES["dynamic_sql"].reason

    def test_a_class_constant_is_unknown(self):
        source = migration("$this->addSql(self::DROP_STATEMENT);")
        assert only(source).reason == _GATES["dynamic_sql"].reason

    def test_a_call_with_no_argument_is_none_and_not_unknown(self):
        finding = only(migration("$this->addSql();"))
        assert finding.severity is Severity.NONE
        assert finding.reason == _GATES["empty_sql"].reason

    def test_the_phrase_is_the_one_alembic_gives_for_the_same_situation(self):
        """A mesma frase que `op.execute(sql)` recebe.

        O SQL montado em tempo de execução é a mesma situação nas duas stacks, e
        as duas têm que dizer a mesma coisa — senão o time de dados aprende duas
        frases para um problema.
        """
        alembic = classify_alembic(
            "from alembic import op\n\n\ndef upgrade():\n    op.execute(sql)\n"
        )
        assert [f.reason for f in alembic] == [_GATES["dynamic_sql"].reason]

    def test_a_statement_after_an_unreadable_one_is_still_classified(self):
        # Parar na primeira esconderia o `DROP COLUMN` que vem depois.
        source = migration(f"$this->addSql($sql);\n{add_sql(DROP_COLUMN)}")
        assert severities(source) == [Severity.UNKNOWN, Severity.BREAKING]
        assert max_severity(severities(source)) is Severity.BREAKING


# ---------------------------------------------------------------------------
# O segundo argumento: os parâmetros nomeados
# ---------------------------------------------------------------------------


class TestNamedParameters:
    """`addSql($sql, $params)` é a forma que metade do corpus real usa.

    O segundo argumento carrega **valor**, não schema, e a razão de um finding
    vai para um canal do Slack. Ele nunca é lido, e o primeiro argumento continua
    sendo classificado como qualquer outro.
    """

    DDL_WITH_PARAMS = (
        "$this->addSql(\n"
        "    'ALTER TABLE freight_note DROP COLUMN settled',\n"
        "    []\n"
        ");"
    )

    def test_the_first_argument_is_still_read_when_a_second_one_follows(self):
        finding = only(migration(self.DDL_WITH_PARAMS))
        assert finding.severity is Severity.BREAKING
        assert finding.operation == "DROP COLUMN"

    def test_a_data_statement_with_named_parameters_is_controlled(self):
        source = migration(
            "$this->addSql(\n"
            "    'UPDATE freight_note SET carrier_code = :code WHERE id = :id',\n"
            "    ['code' => 'none', 'id' => 1]\n"
            ");"
        )
        finding = only(source)
        assert finding.severity is Severity.CONTROLLED
        assert finding.operation == "UPDATE"
        # A tabela saiu, e o array de parâmetros não.
        assert "`freight_note`" in finding.reason
        assert "none" not in finding.reason

    @pytest.mark.parametrize(
        "value",
        [
            "'12345678900'",  # CPF entre aspas
            "'Maria Silva'",  # nome entre aspas
            '"Maria Silva"',  # o mesmo nome entre aspas duplas, que no MySQL é string
            "12345678900",  # o mesmo CPF solto
        ],
    )
    def test_no_value_of_the_parameter_array_reaches_the_reason(self, value):
        source = migration(
            "$this->addSql(\n"
            "    'UPDATE freight_note SET carrier_code = :code WHERE id = :id',\n"
            f"    ['code' => {value}, 'id' => {value}]\n"
            ");"
        )
        finding = only(source)
        assert "12345678900" not in finding.reason
        assert "Maria Silva" not in finding.reason
        assert "12345678900" not in finding.operation
        assert "Maria Silva" not in finding.operation

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO freight_note (carrier_code) VALUES ('12345678900')",
            'INSERT INTO freight_note (carrier_code) VALUES ("Maria Silva")',
            "UPDATE freight_note SET carrier_code = '12345678900'",
            "REPLACE INTO freight_note (carrier_code) VALUES ('12345678900')",
            'REPLACE INTO freight_note (carrier_code) VALUES ("Maria Silva")',
            "CALL settle_freight(12345678900)",
        ],
    )
    def test_the_truncation_of_sql_py_holds_on_this_path_too(self, sql):
        """O corte de `detect/sql.py`, exercitado por dentro do `addSql`.

        Migração de dados é a maioria do corpus deste consumidor, e a razão de um
        statement não reconhecido é o statement ecoado. O corte tem que valer
        aqui igual: nem valor entre aspas — das três aspas — nem número longo
        solto pode sobreviver. Vale para o DML, que tem linha na tabela, e para o
        verbo que não tem e sai ecoado.
        """
        finding = only(migration(add_sql(sql)) if "'" not in sql else migration(
            "$this->addSql(<<<'SQL'\n" f"{sql}\n" "SQL);"
        ))
        assert "12345678900" not in finding.reason
        assert "Maria Silva" not in finding.reason

    def test_the_reason_still_names_the_verb_it_did_not_understand(self):
        # Guarda pareado: um corte que apagasse a frase inteira passaria em tudo
        # acima. O que o time de dados precisa ver é o verbo.
        finding = only(
            migration(
                "$this->addSql(<<<'SQL'\n"
                "REPLACE INTO freight_note (carrier_code) VALUES ('12345678900')\n"
                "SQL);"
            )
        )
        assert finding.severity is Severity.UNKNOWN
        assert finding.operation == "REPLACE"
        assert finding.reason.endswith(MANUAL)
        assert "REPLACE INTO freight_note (carrier_code) VALUES" in finding.reason


# ---------------------------------------------------------------------------
# A destruição que o corpus real não tem
# ---------------------------------------------------------------------------


class TestDestructionInUp:
    """As três operações mais destrutivas, em `up()`, nos dois dialetos.

    O corpus real do consumidor não tem nenhuma delas em `up()`: os dois
    `DROP TABLE` que existem lá estão no `down()`, que o parser ignora por
    especificação. É exatamente o que a QQ-2161 achou no Alembic, onde os drops
    viviam só no `downgrade()` — e a consequência é a mesma: sem estas fixturas,
    um `DROP TABLE` classificado como `safe` passaria a suíte inteira.
    """

    @pytest.mark.parametrize(
        "dialect, sql, operation, fragment",
        [
            (
                "mysql",
                "DROP TABLE freight_note",
                "DROP TABLE",
                "Tabela `freight_note` removida — quem lê essa tabela quebra.",
            ),
            (
                "pgsql",
                'DROP TABLE stock."freight_note"',
                "DROP TABLE",
                "Tabela `stock.freight_note` removida — quem lê essa tabela quebra.",
            ),
            (
                "mysql",
                "ALTER TABLE freight_note DROP COLUMN settled",
                "DROP COLUMN",
                "Coluna `settled` removida de `freight_note` — quem lê essa coluna quebra.",
            ),
            (
                "pgsql",
                'ALTER TABLE stock."freight_note" DROP COLUMN "settled"',
                "DROP COLUMN",
                "Coluna `settled` removida de `stock.freight_note` — "
                "quem lê essa coluna quebra.",
            ),
            (
                "mysql",
                MYSQL_NOT_NULL,
                "MODIFY ... NOT NULL",
                "Coluna `carrier_code` de `freight_note` passou a NOT NULL — "
                "insert sem valor passa a falhar.",
            ),
            (
                "pgsql",
                PGSQL_NOT_NULL,
                "ALTER COLUMN ... SET NOT NULL",
                "Coluna `carrier_code` de `stock.freight_note` passou a NOT NULL — "
                "insert sem valor passa a falhar.",
            ),
        ],
        ids=lambda value: value if isinstance(value, str) and len(value) < 32 else "",
    )
    def test_the_destructive_operation_is_breaking_and_says_what_breaks(
        self, dialect, sql, operation, fragment
    ):
        # A frase renderizada inteira, e não um fragmento solto: onde a razão
        # nomeia duas coisas — a coluna e a tabela —, uma transposição tem que
        # falhar.
        finding = one(sql)
        assert finding.severity is Severity.BREAKING, dialect
        assert finding.operation == operation, dialect
        assert finding.reason == fragment, dialect

    def test_the_two_dialects_say_the_same_thing_about_the_same_risk(self):
        """A passagem para NOT NULL nos dois vocabulários.

        `MODIFY ... NOT NULL` e `ALTER COLUMN ... SET NOT NULL` são a mesma
        mudança escrita de dois jeitos, uma em cada banco do consumidor. As duas
        têm que sair `breaking` e dizer a mesma frase — é o que garante que a
        base do financeiro e a do resto do sistema recebam o mesmo alerta.
        """
        mysql = one(MYSQL_NOT_NULL)
        pgsql = one(PGSQL_NOT_NULL)
        assert mysql.severity is pgsql.severity is Severity.BREAKING
        # A operação difere porque o statement difere; a frase, tirando o nome
        # qualificado da tabela, é a mesma.
        assert mysql.operation != pgsql.operation
        assert mysql.reason.replace("`freight_note`", "X") == pgsql.reason.replace(
            "`stock.freight_note`", "X"
        )

    def test_a_drop_only_in_down_does_not_paint_the_migration(self):
        # O outro lado da mesma decisão, e o que a torna segura: o rollback de
        # uma migração aditiva não pode virar `breaking`.
        source = migration(add_sql(CREATE), down=add_sql(DROP_TABLE))
        assert severities(source) == [Severity.SAFE]

    def test_the_same_drop_moved_into_up_is_breaking(self):
        # Mutante pareado do teste acima: a diferença é só de qual método o
        # statement está, então o parser realmente separa os dois corpos.
        source = migration(add_sql(DROP_TABLE), down=add_sql(CREATE))
        assert severities(source) == [Severity.BREAKING]


# ---------------------------------------------------------------------------
# A tabela de portões
# ---------------------------------------------------------------------------

GATE_SOURCES = {
    "dynamic_sql": migration("$this->addSql($sql);"),
    "interpolating_sql": migration(f'$this->addSql("{DROP_COLUMN}");'),
    "empty_sql": migration("$this->addSql();"),
    "ambiguous_up": (
        f"{HEAD}\nclass A extends AbstractMigration\n{{\n"
        "    public function up(Schema $schema): void\n    {\n    }\n}\n"
        "class B extends AbstractMigration\n{\n"
        "    public function up(Schema $schema): void\n    {\n    }\n}\n"
    ),
    "unterminated_up": (
        f"{HEAD}\nclass A extends AbstractMigration\n{{\n"
        "    public function up(Schema $schema): void\n    {\n"
        f"        {add_sql(DROP_COLUMN)}\n"
    ),
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
        assert _GATES[name].reason in [f.reason for f in findings], name

    def test_no_source_claims_a_row_that_left_the_table(self):
        assert set(GATE_SOURCES) == set(_GATES)

    def test_every_gate_the_module_defines_is_in_the_table(self):
        # A tabela é o artefato que a QQ-2162 leva ao time de dados. Um portão
        # criado ao lado dela sairia no Slack sem estar na tabela.
        defined = {
            value for value in vars(doctrine).values() if isinstance(value, doctrine._Gate)
        }
        assert defined == set(_GATES.values())

    @pytest.mark.parametrize("name", sorted(_GATES))
    def test_every_row_carries_an_operation(self, name):
        assert _GATES[name].operation

    def test_no_row_has_a_placeholder_it_cannot_fill(self):
        # As razões deste módulo não são template: não há nome de objeto a citar
        # quando o parser não leu o SQL. Um `{table}` aqui sairia literal no
        # Slack.
        for name, gate in sorted(_GATES.items()):
            assert "{" not in gate.reason, name
            assert "}" not in gate.reason, name

    def test_every_row_has_a_distinct_reason(self):
        # Duas linhas com a mesma frase são uma linha a menos de informação, e
        # `test_every_row_in_the_table_is_reachable` passaria com a errada.
        assert len({gate.reason for gate in _GATES.values()}) == len(_GATES)


class TestFindingShape:
    EVERY_KIND = (
        migration(add_sql(CREATE)),
        migration(add_sql(DROP_COLUMN)),
        migration(f"{add_sql(CREATE)}\n{add_sql(DROP_TABLE)}"),
        migration("$this->addSql(<<<'SQL'\n" f"{MYSQL_NOT_NULL}\n" "SQL);"),
        *GATE_SOURCES.values(),
    )

    @pytest.mark.parametrize("source", EVERY_KIND)
    def test_every_finding_has_an_operation_and_a_reason(self, source):
        findings = classify_migration(source)
        assert findings, source
        for finding in findings:
            assert finding.operation, source
            assert finding.reason, source

    @pytest.mark.parametrize("source", EVERY_KIND)
    def test_every_reason_is_a_portuguese_sentence(self, source):
        for finding in classify_migration(source):
            assert finding.reason[0].isupper(), finding.reason
            assert finding.reason.endswith("."), finding.reason

    @pytest.mark.parametrize("source", EVERY_KIND)
    def test_no_finding_carries_a_path_or_a_line_number(self, source):
        # `Finding` descreve um statement, não um arquivo. Associá-lo ao caminho
        # — que é onde fica escrito qual dos dois bancos foi mexido — é trabalho
        # de quem chamou o parser.
        for finding in classify_migration(source):
            assert not hasattr(finding, "path")
            assert not hasattr(finding, "line")

    @pytest.mark.parametrize("source", EVERY_KIND)
    def test_no_operation_carries_a_long_number_or_a_quote(self, source):
        # A invariante que atravessa os parsers, e que já foi violada três
        # vezes. As migrações do consumidor têm o timestamp no nome da classe.
        for finding in classify_migration(source):
            assert not re.search(r"\d{4,}", finding.operation), finding
            for quote in ("'", '"', "`"):
                assert quote not in finding.operation, finding

    def test_a_file_that_is_not_php_at_all_yields_no_findings(self):
        assert classify_migration("") == []
        assert classify_migration("não é código nenhum") == []


# ---------------------------------------------------------------------------
# O marcador do dispatch
# ---------------------------------------------------------------------------


GENERATOR = '''<?php

namespace Migrations\\Migrator\\Command;

class MakeCommand
{
    private function generateTemplate(string $className, string $name): string
    {
        return <<<PHP
<?php

use Doctrine\\Migrations\\AbstractMigration;

final class {$className} extends AbstractMigration
{
    public function up(Schema \\$schema): void
    {
        // TODO
    }
}
PHP;
    }
}
'''


class TestTheDispatchMarker:
    """O marcador é `extends AbstractMigration` **em posição de código**.

    A parte que importa é "em posição de código". O consumidor tem um gerador de
    migrações que monta o arquivo novo dentro de um heredoc, e esse heredoc
    contém o marcador e a assinatura de `up()` escritos por extenso.
    """

    def test_a_migration_declares_it(self):
        assert declares_migration(migration(add_sql(CREATE))) is True

    @pytest.mark.parametrize(
        "clause",
        [
            "extends AbstractMigration",
            "extends \\Doctrine\\Migrations\\AbstractMigration",
            "extends Doctrine\\Migrations\\AbstractMigration",
            "extends   AbstractMigration",
        ],
    )
    def test_the_namespace_may_be_written_out(self, clause):
        source = migration(add_sql(CREATE)).replace("extends AbstractMigration", clause)
        assert declares_migration(source) is True

    def test_the_generator_of_migrations_is_not_one(self):
        assert declares_migration(GENERATOR) is False

    def test_a_substring_marker_would_have_read_the_generator_as_a_migration(self):
        """O par que prova de onde vem a exclusão.

        Sem ele, `test_the_generator_of_migrations_is_not_one` não distingue
        "o marcador roda sobre o texto apagado" de "o gerador não tem o
        marcador". Ele tem — duas vezes, e a assinatura de `up()` também.
        """
        assert "extends AbstractMigration" in GENERATOR
        assert "AbstractMigration" in GENERATOR
        assert "function up(" in GENERATOR

    def test_the_parser_has_nothing_to_say_about_the_generator(self):
        # O outro lado: o marcador não é mais estreito que o parser. Se ele
        # deixasse o gerador passar, o parser abriria o `up()` do template e
        # devolveria lista vazia — que a jusante se lê como "nada a reportar".
        assert classify_migration(GENERATOR) == []

    def test_a_migration_named_in_a_string_is_not_a_migration(self):
        source = (
            "<?php\n"
            "$template = 'final class Version1 extends AbstractMigration { "
            "public function up() { } }';\n"
        )
        assert declares_migration(source) is False

    def test_a_migration_named_in_a_comment_is_not_a_migration(self):
        for comment in (
            "// final class V extends AbstractMigration\n",
            "# final class V extends AbstractMigration\n",
            "/* final class V extends AbstractMigration */\n",
        ):
            assert declares_migration(f"<?php\n{comment}") is False, comment

    def test_a_php_attribute_is_not_read_as_a_comment(self):
        # `#[` abre atributo no PHP 8, e não comentário. Lido como comentário, o
        # resto da linha seria apagado — e com o atributo na mesma linha da
        # declaração da classe, o marcador desapareceria.
        source = migration(add_sql(CREATE)).replace(
            "final class Version20250101120000 extends AbstractMigration",
            "#[\\Attribute] final class Version20250101120000 extends AbstractMigration",
        )
        assert declares_migration(source) is True
        assert severities(source) == [Severity.SAFE]

    def test_a_php_file_that_is_not_a_migration_does_not_declare_one(self):
        assert declares_migration("<?php\nclass Helper { }\n") is False
        assert declares_migration("<?php\nreturn ['databases' => []];\n") is False


# ---------------------------------------------------------------------------
# O glob de migration_paths
# ---------------------------------------------------------------------------

ACTION_DIR = Path(__file__).resolve().parent.parent
CORPUS = Path(__file__).parent / "fixtures" / "corpus"


def _glob_to_regex(glob: str) -> str:
    """A conversão glob→regex do step `Collect`, em Python.

    Mesma ordem de substituições do `sed`: ponto literal, `**` protegido por
    placeholder, `*` simples, placeholder restaurado. É a conversão que decide
    quais arquivos chegam ao classificador, e ela só entende `*`, `**` e ponto.
    """
    regex = glob.replace(".", r"\.").replace("**", "DSTAR")
    regex = regex.replace("*", "[^/]*").replace("DSTAR", ".*")
    return regex


def _doctrine_glob() -> str:
    action = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
    line = next(
        l for l in action.splitlines()
        if l.strip().startswith("default:") and "*.sql" in l
    )
    globs = [g.strip() for g in line.split("'")[1].split(",")]
    php = [g for g in globs if g.endswith(".php")]
    assert len(php) == 1, php
    return php[0]


class TestTheMigrationPathsGlob:
    """O glob do `action.yml`, passado pela conversão de verdade.

    A conversão do step `Collect` só entende `*`, `**` e ponto literal —
    qualquer outro metacaractere faz o step falhar por decisão. Um glob que
    sobrevivesse à conversão e não casasse nada publicaria `has_files=false`, e
    o job terminaria verde dizendo "sem alteração de banco" num PR que dropou
    coluna.
    """

    def test_the_glob_uses_no_metacharacter_the_conversion_rejects(self):
        assert not re.search(r"[\[\]{}?]", _doctrine_glob())

    def test_the_converted_regex_matches_every_doctrine_fixture(self):
        pattern = re.compile(_glob_to_regex(_doctrine_glob()))
        fixtures = sorted((CORPUS / "doctrine").rglob("*.php"))
        assert fixtures  # senão o laço abaixo não afirma nada
        for path in fixtures:
            relative = path.relative_to(CORPUS).as_posix()
            assert pattern.search(relative), relative

    def test_both_database_directories_are_covered(self):
        # O motivo de existir do cartão: o consumidor tem duas bases, e um glob
        # que alcançasse só uma deixaria a outra sem alerta nenhum.
        pattern = re.compile(_glob_to_regex(_doctrine_glob()))
        matched = {
            path.parent.name
            for path in sorted((CORPUS / "doctrine").rglob("*.php"))
            if pattern.search(path.relative_to(CORPUS).as_posix())
        }
        assert matched == {"mysql", "pgsql"}

    @pytest.mark.parametrize(
        "path",
        [
            # O runner do consumidor. `Migrations/*/Version*.php` casaria — é o
            # `2` depois de `Version` que o deixa de fora.
            "Migrations/Migrator/VersionHelper.php",
            # O gerador, que traz o marcador escrito dentro de um heredoc.
            "Migrations/Migrator/Command/MakeCommand.php",
            # A configuração das duas conexões.
            "Migrations/migrations.php",
            # Um `Version.php` de biblioteca vendorizada, fora de `Migrations/`.
            "plugin/vendor/src/Service/Version.php",
        ],
    )
    def test_the_glob_leaves_out_what_is_not_a_migration(self, path):
        pattern = re.compile(_glob_to_regex(_doctrine_glob()))
        assert not pattern.search(path), path

    def test_the_root_relative_form_of_the_regex_also_matches(self):
        # O step publica duas alternativas por glob: a convertida e a mesma sem
        # o `.*/` do começo, para o arquivo que está na raiz do repositório. É a
        # segunda que casa os arquivos do consumidor, cujo caminho começa em
        # `Migrations/`.
        regex = _glob_to_regex(_doctrine_glob())
        root = re.sub(r"^\.\*/", "", regex)
        assert re.search(root, "Migrations/mysql/Version20260101120000.php")
        assert re.search(root, "Migrations/pgsql/Version20260101120000.php")
        assert not re.search(root, "Migrations/Migrator/VersionHelper.php")


# ---------------------------------------------------------------------------
# As bordas do scanner
# ---------------------------------------------------------------------------


class TestScannerEdges:
    """Arquivo truncado e sintaxe que não é a que parece.

    Cada caso aqui é um lugar onde o scanner pode sair do trilho e devolver
    lista vazia, que a jusante se lê como "nada a reportar". Nenhum deles pode
    sair em silêncio: ou o `up()` continua legível, ou a resposta é `unknown`.
    """

    def test_an_unterminated_single_quoted_string_does_not_hang_or_go_silent(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            "        $this->addSql('ALTER TABLE freight_note DROP COLUMN settled\n"
        )
        # A string engole o resto do arquivo, então o corpo de `up()` não fecha.
        assert only(source).reason == _GATES["unterminated_up"].reason

    def test_an_unterminated_double_quoted_string_does_not_hang_or_go_silent(self):
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema): void\n"
            "    {\n"
            '        $this->addSql("ALTER TABLE freight_note DROP COLUMN settled\n'
        )
        assert only(source).reason == _GATES["unterminated_up"].reason

    def test_a_file_that_ends_right_after_the_name_of_up_is_not_a_definition(self):
        # Sem `(` depois do nome não há definição a ler. Lista vazia é a resposta
        # certa aqui, e o portão do dispatch é quem a transforma em `unknown`
        # sobre um arquivo que declara operações.
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up"
        )
        assert classify_migration(source) == []

    def test_an_escape_in_a_double_quoted_string_does_not_end_it(self):
        # `\\"` não fecha a string. Fechando ali, o resto do arquivo entraria
        # como código e o corpo de `up()` deixaria de fechar.
        source = migration(
            '$this->addSql("SELECT \\"x\\"");\n' + add_sql(DROP_COLUMN)
        )
        assert severities(source) == [Severity.UNKNOWN, Severity.BREAKING]

    def test_a_triple_angle_that_does_not_open_a_heredoc_is_not_a_literal(self):
        # Rótulo que não é identificador válido: não é heredoc, então não há
        # literal naquele índice e a resposta é o portão de SQL não literal.
        source = migration("$this->addSql(<<<123\nDROP TABLE freight_note\n123);")
        assert only(source).reason == _GATES["dynamic_sql"].reason

    def test_a_nested_parenthesis_in_the_parameter_list_is_matched(self):
        # `new Schema()` na lista de parâmetros põe um `)` que não fecha a lista.
        # Fechando ali, a chave do corpo seria procurada do lugar errado.
        source = (
            f"{HEAD}\n"
            "final class Version20250101120000 extends AbstractMigration\n"
            "{\n"
            "    public function up(Schema $schema = new Schema()): void\n"
            "    {\n"
            f"        {add_sql(DROP_COLUMN)}\n"
            "    }\n"
            "}\n"
        )
        assert severities(source) == [Severity.BREAKING]

    def test_a_static_call_to_add_sql_is_not_the_doctrine_api(self):
        # `AbstractMigration::addSql()` é método de instância, e o que o Doctrine
        # executa é `$this->addSql()`. Um `Helper::addSql()` é outra função, com
        # outro comportamento, e o parser não pode afirmar o que ela faz.
        source = migration("SqlHelper::addSql('DROP TABLE freight_note');")
        assert classify_migration(source) == []

    def test_the_instance_call_right_next_to_it_is_read(self):
        # Par do teste acima: é o `->` que decide, e não a palavra `addSql`.
        source = migration("$this->addSql('DROP TABLE freight_note');")
        assert severities(source) == [Severity.BREAKING]

    def test_a_call_broken_across_lines_is_read(self):
        # `->` numa linha e `addSql(` na outra é PHP válido e o corpus escreve
        # chamada multilinha o tempo todo.
        source = migration(
            "$this\n    ->addSql(\n        'DROP TABLE freight_note'\n    );"
        )
        assert severities(source) == [Severity.BREAKING]
