"""Testes do parser de migrações do Django.

As migrações usadas aqui foram derivadas do corpus real do consumidor
Django/MySQL — 591 arquivos, cujas 17 operações distintas o classificador tem
que cobrir. Onde o
corpus não cobre o caso exigido pelo aceite (SQL montado dinamicamente, lista
`operations` condicional), a migração é inventada, mas com a forma que um
arquivo real teria.

Nenhum arquivo do corpus é copiado para cá: a suíte de fixtures é da QQ-2161.
"""

import string
import textwrap

import pytest

from detect.django import (
    _EXPANDERS,
    _OPERATIONS,
    _REFINERS,
    classify_migration,
)
from detect.severity import DUPLICATE, Severity, max_severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def migration(operations: str, preamble: str = "") -> str:
    """Um arquivo de migração com `operations` no corpo da classe."""
    return (
        "from django.db import migrations, models\n"
        f"{preamble}\n"
        "class Migration(migrations.Migration):\n"
        "    dependencies = [('app', '0001_initial')]\n"
        "    operations = [\n"
        f"{textwrap.indent(textwrap.dedent(operations).strip(), ' ' * 8)}\n"
        "    ]\n"
    )


def severities(source: str) -> list[Severity]:
    return [finding.severity for finding in classify_migration(source)]


def only(source: str):
    """O único finding da migração — falha se houver mais de um."""
    findings = classify_migration(source)
    assert len(findings) == 1, findings
    return findings[0]


def severity_of(operations: str) -> Severity:
    return only(migration(operations)).severity


# ---------------------------------------------------------------------------
# Leitura da lista operations
# ---------------------------------------------------------------------------


class TestOperationsList:
    def test_returns_one_finding_per_operation_in_order(self):
        source = migration(
            """
            migrations.CreateModel(name='Tab', fields=[]),
            migrations.RemoveField(model_name='tab', name='code'),
            migrations.RunPython(forward, backward),
            """
        )
        assert severities(source) == [Severity.SAFE, Severity.BREAKING, Severity.CONTROLLED]

    def test_a_tuple_of_operations_is_read_like_a_list(self):
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = (migrations.DeleteModel(name='Tab'),)\n"
        )
        assert severities(source) == [Severity.BREAKING]

    def test_empty_operations_yields_no_findings(self):
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('app', '0001_initial')]\n"
            "    operations = []\n"
        )
        assert classify_migration(source) == []

    def test_a_file_without_a_migration_class_yields_no_findings(self):
        # `__init__.py` e módulos auxiliares dentro de `migrations/` caem aqui.
        source = "SQL_APPLY = 'ALTER TABLE t DROP COLUMN c'\n\ndef helper():\n    return 1\n"
        assert classify_migration(source) == []

    def test_a_migration_class_without_operations_yields_no_findings(self):
        # Migração de merge: só junta dependências, não faz nada no banco.
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('app', '0009_a'), ('app', '0009_b')]\n"
        )
        assert classify_migration(source) == []

    def test_the_last_operations_assignment_wins(self):
        # Semântica de Python, e a única leitura defensável: é a lista que a
        # classe termina tendo.
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.CreateModel(name='A', fields=[])]\n"
            "    operations = [migrations.DeleteModel(name='A')]\n"
        )
        assert severities(source) == [Severity.BREAKING]

    def test_the_last_migration_class_wins(self):
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.CreateModel(name='A', fields=[])]\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.DeleteModel(name='A')]\n"
        )
        assert severities(source) == [Severity.BREAKING]


class TestOperationsListThatCannotBeRead:
    """A lista tem que ser um literal. Fora disso, `unknown`, nunca palpite."""

    def test_operations_built_from_a_variable_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "OPS = [migrations.DeleteModel(name='Tab')]\n"
            "class Migration(migrations.Migration):\n"
            "    operations = OPS\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_operations_built_by_a_comprehension_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.DeleteModel(name=n) for n in ('a', 'b')]\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_operations_built_by_concatenation_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "EXTRA = []\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.DeleteModel(name='Tab')] + EXTRA\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_operations_assigned_inside_a_conditional_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "import os\n"
            "class Migration(migrations.Migration):\n"
            "    if os.environ.get('LEGACY'):\n"
            "        operations = [migrations.DeleteModel(name='Tab')]\n"
            "    else:\n"
            "        operations = []\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_operations_extended_with_augmented_assignment_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "EXTRA = []\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.CreateModel(name='A', fields=[])]\n"
            "    operations += EXTRA\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    @pytest.mark.parametrize(
        "mutation",
        [
            "append(migrations.DeleteModel(name='B'))",
            "extend([migrations.DeleteModel(name='B')])",
            "insert(0, migrations.DeleteModel(name='B'))",
            "remove(operations[0])",
            "pop()",
            "clear()",
        ],
    )
    def test_operations_mutated_by_a_method_call_is_unknown(self, mutation):
        # `operations.append(...)` deixa `operations` em contexto de leitura: o
        # literal continua legível e o arquivo sairia com zero findings, que a
        # jusante lê como "nada a reportar". Vale para todo método que muda a
        # lista no lugar, não só os que acrescentam.
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.CreateModel(name='A', fields=[])]\n"
            f"    operations.{mutation}\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_operations_built_by_a_loop_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = []\n"
            "    for name in ('a', 'b'):\n"
            "        operations.append(migrations.DeleteModel(name=name))\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_migration_class_defined_inside_a_conditional_is_unknown(self):
        # O Django carrega `module.Migration`, então a definição vale; qual das
        # definições vale depende de algo que o classificador não executa.
        source = (
            "from django.db import migrations\n"
            "import os\n"
            "if os.environ.get('LEGACY'):\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = [migrations.DeleteModel(name='Tab')]\n"
            "else:\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = []\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_migration_class_defined_inside_a_try_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "try:\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = [migrations.DeleteModel(name='Tab')]\n"
            "except ImportError:\n"
            "    pass\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_class_named_migration_next_to_the_real_one_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "import os\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.CreateModel(name='A', fields=[])]\n"
            "if os.environ.get('LEGACY'):\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = [migrations.DeleteModel(name='A')]\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_file_that_is_not_valid_python_is_unknown(self):
        assert severities("class Migration(migrations.Migration:\n") == [Severity.UNKNOWN]

    def test_an_unreadable_migration_never_reports_a_severity_below_unknown(self):
        # É o contrato que sustenta o ADR: falhar em ler não pode sair verde.
        for source in [
            "class Migration(migrations.Migration:\n",
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = OPS\n",
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = []\n"
            "    operations.append(migrations.RunSQL(sql='DROP TABLE orders;'))\n",
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.SeparateDatabaseAndState(**SPEC)]\n",
        ]:
            assert max_severity(severities(source)) is Severity.UNKNOWN


# ---------------------------------------------------------------------------
# Operações destrutivas
# ---------------------------------------------------------------------------


class TestDestructiveOperationsAreBreaking:
    """Aceite: RemoveField, RenameField, DeleteModel e RenameModel são breaking."""

    def test_remove_field(self):
        assert (
            severity_of("migrations.RemoveField(model_name='subject', name='code'),")
            is Severity.BREAKING
        )

    def test_rename_field(self):
        assert (
            severity_of(
                "migrations.RenameField(model_name='subject', old_name='code',"
                " new_name='subject_code'),"
            )
            is Severity.BREAKING
        )

    def test_delete_model(self):
        assert severity_of("migrations.DeleteModel(name='TissVersion'),") is Severity.BREAKING

    def test_rename_model(self):
        assert (
            severity_of("migrations.RenameModel(old_name='Tab', new_name='Section'),")
            is Severity.BREAKING
        )

    def test_remove_field_reason_names_the_model_and_the_field(self):
        finding = only(migration("migrations.RemoveField(model_name='subject', name='code'),"))
        assert "subject" in finding.reason
        assert "code" in finding.reason

    def test_rename_field_reason_names_the_old_name_not_the_new_one(self):
        # Quem quebra é quem lê o nome antigo; é esse que o Slack precisa mostrar.
        # Os dois nomes não podem ser um substring do outro, senão o assert passa
        # com a razão citando o nome errado.
        finding = only(
            migration(
                "migrations.RenameField(model_name='subject', old_name='cpf',"
                " new_name='tax_id'),"
            )
        )
        assert "cpf" in finding.reason
        assert "tax_id" not in finding.reason

    def test_positional_arguments_are_read_like_keywords(self):
        finding = only(migration("migrations.RemoveField('subject', 'code'),"))
        assert finding.severity is Severity.BREAKING
        assert "subject" in finding.reason
        assert "code" in finding.reason


# ---------------------------------------------------------------------------
# AddField
# ---------------------------------------------------------------------------


class TestAddField:
    """Aceite: `null=True` ou `default` é safe; sem os dois, controlled."""

    def test_nullable_field_is_safe(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='nickname',"
                " field=models.CharField(max_length=50, null=True)),"
            )
            is Severity.SAFE
        )

    def test_field_with_a_default_is_safe(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='tab', name='new_docs_tab',"
                " field=models.BooleanField(default=False)),"
            )
            is Severity.SAFE
        )

    def test_field_with_both_null_and_default_is_safe(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='nickname',"
                " field=models.CharField(default=None, max_length=50, null=True)),"
            )
            is Severity.SAFE
        )

    def test_field_without_null_and_without_default_is_controlled(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='clinic',"
                " field=models.ForeignKey(on_delete=models.CASCADE, to='clinics.Clinic')),"
            )
            is Severity.CONTROLLED
        )

    def test_null_false_does_not_count_as_nullable(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='code',"
                " field=models.CharField(max_length=10, null=False)),"
            )
            is Severity.CONTROLLED
        )

    def test_blank_true_does_not_count_as_nullable(self):
        # `blank` é validação de formulário, não nulidade de coluna.
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='code',"
                " field=models.CharField(blank=True, max_length=10)),"
            )
            is Severity.CONTROLLED
        )

    def test_null_from_a_truthy_non_boolean_does_not_count_as_nullable(self):
        # Só o literal `True`. `1` é truthy em Python mas não é o que o Django
        # escreve, e aceitar qualquer truthy afrouxa a regra sem ganho nenhum.
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='code',"
                " field=models.CharField(max_length=10, null=1)),"
            )
            is Severity.CONTROLLED
        )

    def test_null_from_a_variable_does_not_count_as_nullable(self):
        # Só o literal `True` conta; um nome o classificador não resolve.
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='code',"
                " field=models.CharField(max_length=10, null=NULLABLE)),"
            )
            is Severity.CONTROLLED
        )

    def test_default_from_a_callable_still_counts_as_a_default(self):
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='created',"
                " field=models.DateTimeField(default=django.utils.timezone.now)),"
            )
            is Severity.SAFE
        )

    def test_preserve_default_false_does_not_change_the_verdict(self):
        # Padrão do Django para NOT NULL com default de uma vez só.
        assert (
            severity_of(
                "migrations.AddField(model_name='subject', name='code',"
                " field=models.CharField(default='x', max_length=10),"
                " preserve_default=False),"
            )
            is Severity.SAFE
        )

    def test_a_field_that_is_not_a_literal_call_is_unknown(self):
        assert (
            severity_of("migrations.AddField(model_name='subject', name='code', field=FIELD),")
            is Severity.UNKNOWN
        )

    def test_a_missing_field_argument_is_unknown(self):
        assert (
            severity_of("migrations.AddField(model_name='subject', name='code'),")
            is Severity.UNKNOWN
        )

    def test_field_passed_positionally_is_read(self):
        assert (
            severity_of(
                "migrations.AddField('subject', 'nickname',"
                " models.CharField(max_length=50, null=True)),"
            )
            is Severity.SAFE
        )

    def test_controlled_reason_explains_the_not_null_without_default(self):
        finding = only(
            migration(
                "migrations.AddField(model_name='subject', name='clinic',"
                " field=models.IntegerField()),"
            )
        )
        assert "NOT NULL" in finding.reason
        assert "clinic" in finding.reason
        assert "subject" in finding.reason


# ---------------------------------------------------------------------------
# AlterField — a costura da QQ-2156
# ---------------------------------------------------------------------------


class TestAlterField:
    """`AlterField` sai `unknown` nesta subtask.

    Decidir se a alteração quebra exige o estado anterior do campo, que só a
    reconstrução do grafo de migrações (QQ-2156) traz. Enquanto isso não existe,
    `unknown` — e não um palpite a partir da definição nova, que sozinha não diz
    nada sobre o que mudou.
    """

    def test_alter_field_is_unknown(self):
        assert (
            severity_of(
                "migrations.AlterField(model_name='supplier', name='external_id',"
                " field=util.tsid_fields.TSIDField(blank=True, editable=False)),"
            )
            is Severity.UNKNOWN
        )

    def test_alter_field_is_unknown_even_when_the_new_field_is_nullable(self):
        # A definição nova ser nullable não diz se a antiga também era.
        assert (
            severity_of(
                "migrations.AlterField(model_name='subject', name='code',"
                " field=models.CharField(max_length=10, null=True)),"
            )
            is Severity.UNKNOWN
        )

    def test_alter_field_reason_names_the_model_and_the_field(self):
        finding = only(
            migration(
                "migrations.AlterField(model_name='supplier', name='external_id',"
                " field=models.BigIntegerField()),"
            )
        )
        assert "supplier" in finding.reason
        assert "external_id" in finding.reason
        assert "revisão manual" in finding.reason


# ---------------------------------------------------------------------------
# SeparateDatabaseAndState
# ---------------------------------------------------------------------------


class TestSeparateDatabaseAndState:
    """Aceite: percorre `database_operations` e `state_operations`."""

    def test_walks_both_branches(self):
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    migrations.RunSQL(sql='ALTER TABLE t DROP COLUMN c;'),
                ],
                state_operations=[
                    migrations.RemoveField(model_name='t', name='c'),
                ],
            ),
            """
        )
        assert severities(source) == [Severity.BREAKING, Severity.BREAKING]

    def test_database_operations_come_before_state_operations(self):
        # O DDL real mora em `database_operations`; é o que o time de dados lê
        # primeiro no Slack, mesmo quando o arquivo declara `state` antes.
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                state_operations=[
                    migrations.AlterField(model_name='t', name='c',
                                          field=models.BigIntegerField()),
                ],
                database_operations=[
                    migrations.RunPython(forward, backward),
                ],
            ),
            """
        )
        assert severities(source) == [Severity.CONTROLLED, Severity.UNKNOWN]

    def test_a_branch_with_only_database_operations_is_read(self):
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    migrations.RunSQL(sql='DROP TABLE legacy;'),
                ],
            ),
            """
        )
        assert severities(source) == [Severity.BREAKING]

    def test_positional_branches_are_read(self):
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                [migrations.RunSQL(sql='DROP TABLE legacy;')],
                [migrations.DeleteModel(name='Legacy')],
            ),
            """
        )
        assert severities(source) == [Severity.BREAKING, Severity.BREAKING]

    def test_nested_separate_database_and_state_recurses(self):
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    migrations.SeparateDatabaseAndState(
                        database_operations=[
                            migrations.RunSQL(sql='ALTER TABLE t DROP COLUMN c;'),
                        ],
                    ),
                ],
            ),
            """
        )
        assert severities(source) == [Severity.BREAKING]

    def test_a_branch_that_is_not_a_literal_list_is_unknown(self):
        source = migration(
            "migrations.SeparateDatabaseAndState(database_operations=DB_OPS),",
        )
        finding = only(source)
        assert finding.severity is Severity.UNKNOWN
        assert "database_operations" in finding.reason

    def test_keyword_unpacking_is_unknown_not_a_no_op(self):
        # `**kwargs` chega na AST com `arg=None`: os dois ramos parecem ausentes
        # e o caso vazio responderia `none`, afirmando que a migração não mexe no
        # banco. É exatamente o palpite confiante que este classificador existe
        # para não dar.
        finding = only(migration("migrations.SeparateDatabaseAndState(**SPEC),"))
        assert finding.severity is Severity.UNKNOWN
        assert "revisão manual" in finding.reason

    def test_positional_unpacking_is_unknown_and_says_why(self):
        # `*ARGS` já saía `unknown` por outro caminho — o nó desempacotado cai na
        # posição de `database_operations` e não é lista literal. Mas a razão
        # culpava um ramo que a chamada nem nomeia. A severidade estava certa
        # pelo motivo errado, e é a razão que o time de dados lê no Slack.
        finding = only(migration("migrations.SeparateDatabaseAndState(*ARGS),"))
        assert finding.severity is Severity.UNKNOWN
        assert "desempacotados" in finding.reason
        assert "database_operations" not in finding.reason

    def test_unpacking_mixed_with_a_readable_branch_is_still_unknown(self):
        # O ramo legível não prova que o desempacotado é inofensivo.
        finding = only(
            migration(
                "migrations.SeparateDatabaseAndState(database_operations=[], **SPEC),"
            )
        )
        assert finding.severity is Severity.UNKNOWN

    def test_without_any_branch_it_does_not_touch_the_schema(self):
        assert severity_of("migrations.SeparateDatabaseAndState(),") is Severity.NONE

    def test_empty_branches_do_not_touch_the_schema(self):
        assert (
            severity_of(
                "migrations.SeparateDatabaseAndState(database_operations=[],"
                " state_operations=[]),"
            )
            is Severity.NONE
        )

    def test_index_added_online_reads_as_the_index_it_creates(self):
        # payments/0044: o par canônico do corpus — RunSQL com o DDL real em
        # `database_operations` e o AddIndex equivalente em `state_operations`.
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                database_operations=[
                    migrations.RunSQL(
                        sql=(
                            "ALTER TABLE `remittance_plansubscription` "
                            "ALGORITHM=INPLACE, LOCK=NONE, "
                            "ADD INDEX `ix_plansub_dt_created` (`dt_created`);"
                        ),
                        reverse_sql=(
                            "ALTER TABLE `remittance_plansubscription` "
                            "DROP INDEX `ix_plansub_dt_created`;"
                        ),
                    ),
                ],
                state_operations=[
                    migrations.AddIndex(
                        model_name='plansubscription',
                        index=models.Index(fields=['dt_created'],
                                           name='ix_plansub_dt_created'),
                    ),
                ],
            ),
            """
        )
        assert severities(source) == [Severity.SAFE, Severity.SAFE]

    def test_not_null_through_run_sql_reaches_breaking(self):
        # ledger/0042 e 0043: a forma que o classificador determinístico
        # precisa alcançar sem IA — o MODIFY ... NOT NULL está enterrado numa
        # lista de statements dentro de `database_operations`.
        source = migration(
            """
            migrations.SeparateDatabaseAndState(
                state_operations=[
                    migrations.AlterField(
                        model_name='supplier',
                        name='external_id',
                        field=util.tsid_fields.TSIDField(blank=True, editable=False),
                    ),
                ],
                database_operations=[
                    migrations.RunSQL(
                        sql=[
                            "SET @orig_sql_mode = @@SESSION.sql_mode;",
                            "SET SESSION lock_wait_timeout = 60;",
                            "ALTER TABLE ledger_supplier "
                            "MODIFY external_id BIGINT NOT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                            "SET SESSION sql_mode = @orig_sql_mode;",
                        ],
                        reverse_sql=[
                            "ALTER TABLE ledger_supplier "
                            "MODIFY external_id BIGINT NULL, ALGORITHM=INPLACE, LOCK=NONE;",
                        ],
                    ),
                ],
            ),
            """
        )
        findings = classify_migration(source)
        assert max_severity(f.severity for f in findings) is Severity.BREAKING
        breaking = next(f for f in findings if f.severity is Severity.BREAKING)
        assert "ledger_supplier" in breaking.reason
        assert "external_id" in breaking.reason


# ---------------------------------------------------------------------------
# RunSQL
# ---------------------------------------------------------------------------


class TestRunSQL:
    """Aceite: delega para `detect/sql.py`; lista avalia todos e usa a pior."""

    def test_a_single_statement_is_delegated_to_the_sql_classifier(self):
        finding = only(
            migration("migrations.RunSQL(sql='ALTER TABLE subjects_subject DROP COLUMN code;'),")
        )
        assert finding.severity is Severity.BREAKING
        assert finding.operation == "DROP COLUMN"

    def test_several_statements_in_one_string_use_the_worst(self):
        finding = only(
            migration(
                "migrations.RunSQL(sql='SET SESSION lock_wait_timeout = 60;"
                " ALTER TABLE t DROP COLUMN c;'),"
            )
        )
        assert finding.severity is Severity.BREAKING

    def test_a_list_of_statements_uses_the_worst(self):
        finding = only(
            migration(
                """
                migrations.RunSQL(sql=[
                    "SET SESSION lock_wait_timeout = 60;",
                    "ALTER TABLE t ADD INDEX ix_t_c (c);",
                    "ALTER TABLE t DROP COLUMN c;",
                    "SET SESSION lock_wait_timeout = @orig;",
                ]),
                """
            )
        )
        assert finding.severity is Severity.BREAKING
        assert finding.operation == "DROP COLUMN"

    def test_a_list_of_harmless_statements_stays_harmless(self):
        assert (
            severity_of(
                """
                migrations.RunSQL(sql=[
                    "SET SESSION lock_wait_timeout = 60;",
                    "SET SESSION sql_mode = @orig_sql_mode;",
                ]),
                """
            )
            is Severity.NONE
        )

    def test_sql_passed_positionally_is_read(self):
        assert (
            severity_of("migrations.RunSQL('DROP TABLE claim_version;', 'SELECT 1;'),")
            is Severity.BREAKING
        )

    def test_implicit_string_concatenation_is_read(self):
        assert (
            severity_of(
                "migrations.RunSQL(sql=('ALTER TABLE t ' 'DROP COLUMN c;')),",
            )
            is Severity.BREAKING
        )

    def test_reverse_sql_is_not_classified(self):
        # O reverse só roda em rollback; classificá-lo pintaria de vermelho toda
        # migração que sabe se desfazer.
        assert (
            severity_of(
                "migrations.RunSQL(sql='ALTER TABLE t ADD COLUMN c int NULL;',"
                " reverse_sql='ALTER TABLE t DROP COLUMN c;'),"
            )
            is Severity.SAFE
        )

    def test_empty_sql_does_not_touch_the_schema(self):
        assert severity_of("migrations.RunSQL(sql=''),") is Severity.NONE


class TestRunSQLThatCannotBeRead:
    """SQL que não é literal devolve `unknown` — nunca um palpite."""

    def test_sql_from_a_module_level_variable_is_unknown(self):
        # payments/0033: `migrations.RunSQL(SQL_APPLY, SQL_REVERSE)`.
        source = (
            "from django.db import migrations\n"
            "SQL_APPLY = 'ALTER TABLE t MODIFY c CHAR(32) NOT NULL;'\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.RunSQL(SQL_APPLY)]\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_sql_built_by_an_fstring_is_unknown(self):
        assert (
            severity_of(
                "migrations.RunSQL(sql=f'ALTER TABLE {TABLE} DROP COLUMN c;'),",
            )
            is Severity.UNKNOWN
        )

    def test_sql_built_by_percent_formatting_is_unknown(self):
        assert (
            severity_of("migrations.RunSQL(sql='ALTER TABLE %s DROP COLUMN c;' % TABLE),")
            is Severity.UNKNOWN
        )

    def test_sql_built_by_str_format_is_unknown(self):
        assert (
            severity_of("migrations.RunSQL(sql='DROP TABLE {}'.format(TABLE)),")
            is Severity.UNKNOWN
        )

    def test_sql_built_by_join_is_unknown(self):
        assert severity_of("migrations.RunSQL(sql=';'.join(STATEMENTS)),") is Severity.UNKNOWN

    def test_a_list_with_one_non_literal_statement_is_unknown(self):
        assert (
            severity_of(
                "migrations.RunSQL(sql=['SET SESSION lock_wait_timeout = 60;', DYNAMIC]),"
            )
            is Severity.UNKNOWN
        )

    def test_a_statement_with_bound_parameters_is_unknown(self):
        # Forma `[(sql, params)]` do Django: o classificador não lê os parâmetros.
        assert (
            severity_of("migrations.RunSQL(sql=[('UPDATE t SET c = %s', [1])]),")
            is Severity.UNKNOWN
        )

    def test_a_missing_sql_argument_is_unknown(self):
        assert severity_of("migrations.RunSQL(reverse_sql='SELECT 1;'),") is Severity.UNKNOWN

    def test_the_unknown_reason_says_the_sql_is_not_literal(self):
        finding = only(migration("migrations.RunSQL(sql=SQL_APPLY),"))
        assert finding.operation == "RunSQL"
        assert "revisão manual" in finding.reason


# ---------------------------------------------------------------------------
# RunPython
# ---------------------------------------------------------------------------


class TestRunPython:
    """Aceite: `RunPython` é controlled — a confirmar com o time de dados."""

    def test_run_python_is_controlled(self):
        assert severity_of("migrations.RunPython(forward, backward),") is Severity.CONTROLLED

    def test_run_python_with_noop_reverse_is_controlled(self):
        assert (
            severity_of("migrations.RunPython(forward, migrations.RunPython.noop),")
            is Severity.CONTROLLED
        )

    def test_run_python_with_keyword_arguments_is_controlled(self):
        assert (
            severity_of("migrations.RunPython(code=forward, reverse_code=backward),")
            is Severity.CONTROLLED
        )

    def test_the_reason_says_the_classifier_does_not_read_the_function(self):
        finding = only(migration("migrations.RunPython(forward, backward),"))
        assert finding.operation == "RunPython"
        assert "Python" in finding.reason


class TestRunPythonBodyIsScannedForDDL:
    """`RunPython` não é severidade fixa (ADR 0001).

    `controlled` é o piso das migrações que são de dados de verdade, não a
    resposta para todas: o corpo é varrido por strings com cara de DDL —
    literais e f-strings — e o que sair vai para o `detect/sql.py`. Os dois
    arquivos da Evidência 1 da ADR montam `ALTER TABLE ... MODIFY ... NOT NULL`
    com f-string dentro do corpo, e sem esta varredura eles dependem de via
    única: a reconstrução de estado do `AlterField`.
    """

    def test_ddl_literal_in_the_body_decides_the_severity(self):
        source = migration(
            "migrations.RunPython(drop_it, migrations.RunPython.noop),",
            preamble=(
                "def drop_it(apps, schema_editor):\n"
                "    with schema_editor.connection.cursor() as cursor:\n"
                "        cursor.execute('ALTER TABLE app_thing DROP COLUMN legacy_code')\n"
            ),
        )
        assert only(source).severity is Severity.BREAKING

    def test_ddl_built_with_an_f_string_decides_the_severity(self):
        source = migration(
            "migrations.RunPython(tighten, migrations.RunPython.noop),",
            preamble=(
                "TABLE = 'app_thing'\n"
                "def tighten(apps, schema_editor):\n"
                "    schema_editor.execute(\n"
                "        f'ALTER TABLE {TABLE} MODIFY external_id BIGINT NOT NULL'\n"
                "    )\n"
            ),
        )
        assert only(source).severity is Severity.BREAKING

    def test_a_data_migration_without_ddl_stays_on_the_floor(self):
        source = migration(
            "migrations.RunPython(backfill, migrations.RunPython.noop),",
            preamble=(
                "def backfill(apps, schema_editor):\n"
                "    Thing = apps.get_model('app', 'Thing')\n"
                "    Thing.objects.filter(title='').update(title='sem titulo')\n"
            ),
        )
        assert only(source).severity is Severity.CONTROLLED

    def test_a_prose_string_in_the_body_is_not_read_as_sql(self):
        source = migration(
            "migrations.RunPython(backfill, migrations.RunPython.noop),",
            preamble=(
                "def backfill(apps, schema_editor):\n"
                "    raise RuntimeError('DROP everything is not what this does')\n"
            ),
        )
        assert only(source).severity is Severity.CONTROLLED

    def test_ddl_the_sql_classifier_does_not_recognise_is_unknown(self):
        source = migration(
            "migrations.RunPython(reorganise, migrations.RunPython.noop),",
            preamble=(
                "def reorganise(apps, schema_editor):\n"
                "    schema_editor.execute('ALTER TABLE app_thing CLUSTER ON idx_thing')\n"
            ),
        )
        assert only(source).severity is Severity.UNKNOWN

    def test_safe_ddl_in_the_body_does_not_drop_below_the_floor(self):
        source = migration(
            "migrations.RunPython(add_index, migrations.RunPython.noop),",
            preamble=(
                "def add_index(apps, schema_editor):\n"
                "    schema_editor.execute('CREATE INDEX idx_thing ON app_thing (title)')\n"
            ),
        )
        assert only(source).severity is Severity.CONTROLLED

    def test_the_reason_of_a_ddl_body_comes_from_the_sql_classifier(self):
        source = migration(
            "migrations.RunPython(drop_it, migrations.RunPython.noop),",
            preamble=(
                "def drop_it(apps, schema_editor):\n"
                "    schema_editor.execute('ALTER TABLE app_thing DROP COLUMN legacy_code')\n"
            ),
        )
        finding = only(source)
        assert finding.operation == "DROP COLUMN"
        assert "legacy_code" in finding.reason

    def test_a_lambda_body_is_scanned_too(self):
        source = migration(
            "migrations.RunPython("
            "lambda apps, se: se.execute('DROP TABLE app_thing'), "
            "migrations.RunPython.noop),"
        )
        assert only(source).severity is Severity.BREAKING

    def test_ddl_in_a_helper_the_function_calls_is_found(self):
        """A forma da Evidência 1 da ADR: o `RunPython` aponta para uma função
        que delega o `cursor.execute` a um helper do mesmo arquivo. Varrer só o
        corpo de primeiro nível não acha o `ALTER TABLE`."""
        source = migration(
            "migrations.RunPython(set_not_null, migrations.RunPython.noop),",
            preamble=(
                "def _modify_column(cursor, null_clause):\n"
                "    cursor.execute(\n"
                "        f'ALTER TABLE app_thing MODIFY external_id BIGINT {null_clause}'\n"
                "    )\n"
                "def set_not_null(apps, schema_editor):\n"
                "    with schema_editor.connection.cursor() as cursor:\n"
                "        _modify_column(cursor, 'NOT NULL')\n"
            ),
        )
        assert only(source).severity is Severity.BREAKING

    def test_a_helper_that_calls_itself_does_not_hang_the_scan(self):
        source = migration(
            "migrations.RunPython(walk, migrations.RunPython.noop),",
            preamble=(
                "def walk(apps, schema_editor):\n"
                "    walk(apps, schema_editor)\n"
                "    schema_editor.execute('DROP TABLE app_thing')\n"
            ),
        )
        assert only(source).severity is Severity.BREAKING

    def test_a_function_the_module_does_not_define_stays_on_the_floor(self):
        source = migration(
            "migrations.RunPython(imported_helper, migrations.RunPython.noop),",
            preamble="from app.helpers import imported_helper\n",
        )
        finding = only(source)
        assert finding.severity is Severity.CONTROLLED
        assert "corpo" in finding.reason


# ---------------------------------------------------------------------------
# As demais operações do corpus
# ---------------------------------------------------------------------------


class TestRemainingCorpusOperations:
    @pytest.mark.parametrize(
        "operation, expected",
        [
            ("migrations.CreateModel(name='SplitTest', fields=[])", Severity.SAFE),
            (
                "migrations.AddIndex(model_name='subject',"
                " index=models.Index(fields=['code'], name='idx_subject_code'))",
                Severity.SAFE,
            ),
            (
                "migrations.RemoveIndex(model_name='subject', name='idx_subject_code')",
                Severity.CONTROLLED,
            ),
            (
                "migrations.AlterModelOptions(name='supplier',"
                " options={'ordering': ('id',), 'verbose_name': 'médico'})",
                Severity.NONE,
            ),
            (
                "migrations.RemoveConstraint(model_name='subject',"
                " name='unique_subject_unique_ref_code')",
                Severity.CONTROLLED,
            ),
            (
                "migrations.AlterOrderWithRespectTo(name='promptrecipe',"
                " order_with_respect_to='prompt_config')",
                Severity.CONTROLLED,
            ),
        ],
    )
    def test_severity(self, operation, expected):
        assert severity_of(operation + ",") is expected


class TestAddConstraint:
    """Constraint UNIQUE quebra pelo mesmo motivo que em `detect/sql.py`."""

    def test_a_unique_constraint_is_breaking(self):
        assert (
            severity_of(
                "migrations.AddConstraint(model_name='supplier',"
                " constraint=models.UniqueConstraint(fields=('external_id',),"
                " name='unique_supplier_external_id')),"
            )
            is Severity.BREAKING
        )

    def test_a_check_constraint_is_controlled(self):
        assert (
            severity_of(
                "migrations.AddConstraint(model_name='bill',"
                " constraint=models.CheckConstraint(check=models.Q(amount__gte=0),"
                " name='bill_amount_positive')),"
            )
            is Severity.CONTROLLED
        )

    def test_a_constraint_that_is_not_a_literal_call_is_unknown(self):
        assert (
            severity_of("migrations.AddConstraint(model_name='bill', constraint=CONSTRAINT),")
            is Severity.UNKNOWN
        )


class TestAlterUniqueTogether:
    """Sem o estado anterior, exigir combinação única é tratado como quebra.

    É a mesma leitura que `detect/sql.py` faz de `ADD UNIQUE`: linha duplicada
    já existente impede a migração. Esvaziar o `unique_together` só derruba o
    índice, que é controlled.
    """

    def test_setting_a_unique_pair_is_breaking(self):
        assert (
            severity_of(
                "migrations.AlterUniqueTogether(name='userprofiletenant',"
                " unique_together=set([('profile', 'clinic')])),"
            )
            is Severity.BREAKING
        )

    def test_a_set_literal_is_read_like_the_set_call(self):
        assert (
            severity_of(
                "migrations.AlterUniqueTogether(name='invoice',"
                " unique_together={('tender_type', 'external_id')}),"
            )
            is Severity.BREAKING
        )

    def test_emptying_it_with_set_of_empty_list_is_controlled(self):
        assert (
            severity_of(
                "migrations.AlterUniqueTogether(name='partnerbooking',"
                " unique_together=set([])),"
            )
            is Severity.CONTROLLED
        )

    def test_emptying_it_with_a_bare_set_call_is_controlled(self):
        assert (
            severity_of(
                "migrations.AlterUniqueTogether(name='messagetemplateslot',"
                " unique_together=set()),"
            )
            is Severity.CONTROLLED
        )

    def test_a_value_that_is_not_a_literal_is_unknown(self):
        assert (
            severity_of("migrations.AlterUniqueTogether(name='invoice', unique_together=PAIRS),")
            is Severity.UNKNOWN
        )


# ---------------------------------------------------------------------------
# Operação desconhecida
# ---------------------------------------------------------------------------


class TestUnknownOperationsReturnUnknown:
    """O aceite que sustenta o ADR: o classificador nunca chuta.

    O modo de falha que esta subtask substitui é um classificador que devolvia
    `controlled` com confiança 0.0 para tudo. Aqui, o que não está na tabela sai
    `unknown` e aparece no Slack como tal.
    """

    def test_a_third_party_operation_is_unknown(self):
        # journal/0013: `django_add_default_value.AddDefaultValue`, a única
        # operação do corpus fora do vocabulário do Django.
        finding = only(
            migration(
                "AddDefaultValue(model_name='tab', name='new_docs_tab', value=False),",
                preamble="from django_add_default_value import AddDefaultValue\n",
            )
        )
        assert finding.severity is Severity.UNKNOWN
        assert finding.operation == "AddDefaultValue"
        assert "revisão manual" in finding.reason

    def test_an_unmapped_django_operation_is_unknown(self):
        finding = only(migration("migrations.AlterModelTable(name='tab', table='new_tab'),"))
        assert finding.severity is Severity.UNKNOWN
        assert finding.operation == "AlterModelTable"

    def test_an_operation_referenced_by_a_variable_is_unknown(self):
        source = (
            "from django.db import migrations\n"
            "DROP = migrations.DeleteModel(name='Tab')\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [DROP]\n"
        )
        assert severities(source) == [Severity.UNKNOWN]

    def test_a_call_on_an_unresolvable_callee_does_not_claim_it_is_not_a_call(self):
        # `OPS['drop']()` é uma chamada; o que falta é resolver o nome. Dizer
        # "não é uma chamada de operação" afirmaria o contrário do arquivo.
        finding = only(migration("OPS['drop'](),"))
        assert finding.severity is Severity.UNKNOWN
        assert "não é uma chamada" not in finding.reason
        assert "expressão" in finding.reason

    def test_an_item_that_is_not_a_call_says_exactly_that(self):
        finding = only(migration("lambda apps, schema_editor: None,"))
        assert finding.severity is Severity.UNKNOWN
        assert "não é uma chamada de operação" in finding.reason

    def test_a_conditional_expression_in_the_list_is_unknown(self):
        assert (
            severity_of("migrations.DeleteModel(name='Tab') if LEGACY else None,")
            is Severity.UNKNOWN
        )

    def test_a_starred_unpacking_in_the_list_is_unknown(self):
        assert severity_of("*EXTRA_OPERATIONS,") is Severity.UNKNOWN

    def test_a_lambda_in_the_list_is_unknown(self):
        assert severity_of("lambda apps, schema_editor: None,") is Severity.UNKNOWN

    def test_the_unknown_operation_does_not_hide_a_known_breaking_one(self):
        source = migration(
            """
            AddDefaultValue(model_name='tab', name='c', value=False),
            migrations.DeleteModel(name='Tab'),
            """
        )
        assert max_severity(severities(source)) is Severity.BREAKING

    def test_an_unknown_operation_outranks_a_controlled_one(self):
        source = migration(
            """
            migrations.RunPython(forward, backward),
            AddDefaultValue(model_name='tab', name='c', value=False),
            """
        )
        assert max_severity(severities(source)) is Severity.UNKNOWN


# ---------------------------------------------------------------------------
# A tabela de regras não pode sair de sincronia
# ---------------------------------------------------------------------------

# As 17 operações que os 591 arquivos do consumidor Django usam. Operação que sai
# desta lista sai da cobertura do aceite.
CORPUS_OPERATIONS = [
    "AddField",
    "CreateModel",
    "AlterField",
    "RunPython",
    "RunSQL",
    "SeparateDatabaseAndState",
    "AddIndex",
    "AlterModelOptions",
    "RemoveField",
    "AlterUniqueTogether",
    "AddConstraint",
    "DeleteModel",
    "RenameField",
    "RemoveIndex",
    "RenameModel",
    "RemoveConstraint",
    "AlterOrderWithRespectTo",
]


class TestSharedWithTheSqlClassifier:
    def test_the_duplicate_row_sentence_is_the_same_in_both_classifiers(self):
        # Índice único criado pelo ORM e índice único escrito à mão são o mesmo
        # risco, e agora a frase tem uma fonte só, em `detect/severity.py`. O
        # teste deixou de guardar duas cópias e passou a guardar o que importa:
        # as duas razões renderizadas dizem a mesma coisa.
        from detect.sql import classify_sql

        orm = only(
            migration(
                "migrations.AddConstraint(model_name='p',"
                " constraint=models.UniqueConstraint(fields=('c',), name='uniq_p_c')),"
            )
        )
        handwritten = classify_sql("ALTER TABLE p ADD CONSTRAINT uniq_p_c UNIQUE (c);")[0]
        assert orm.severity is handwritten.severity
        assert orm.reason.endswith(DUPLICATE)
        assert handwritten.reason.endswith(DUPLICATE)


class TestRuleTableCoverage:
    @pytest.mark.parametrize("name", CORPUS_OPERATIONS)
    def test_every_corpus_operation_has_a_rule(self, name):
        assert name in _OPERATIONS

    def test_every_refiner_belongs_to_an_operation(self):
        # Refinador sob chave digitada errada é código morto silencioso: a
        # operação continua classificando, só que sempre pela regra crua.
        assert set(_REFINERS) <= set(_OPERATIONS)

    def test_every_expander_belongs_to_an_operation(self):
        assert set(_EXPANDERS) <= set(_OPERATIONS)

    def test_no_operation_has_both_a_refiner_and_an_expander(self):
        # O expansor tem prioridade; um refinador na mesma chave nunca rodaria.
        assert not set(_REFINERS) & set(_EXPANDERS)

    @pytest.mark.parametrize("name", sorted(_OPERATIONS))
    def test_every_reason_only_cites_names_the_rule_knows_how_to_find(self, name):
        operation = _OPERATIONS[name]
        cited = {
            field for _, field, _, _ in string.Formatter().parse(operation.reason) if field
        }
        assert cited <= {"model", "target"}, name
        if name in _EXPANDERS:
            # O expansor monta os nomes que a razão cita; a linha da tabela não
            # tem onde lê-los. `TestSeparateDatabaseAndState` cobre o resultado.
            return
        if "model" in cited:
            assert operation.model, name
        if "target" in cited:
            assert operation.target, name

    @pytest.mark.parametrize("name", sorted(_OPERATIONS))
    def test_the_parameters_the_rule_reads_are_in_the_signature(self, name):
        # Parâmetro fora da assinatura só é achado por keyword; passado
        # posicionalmente, sumiria em silêncio.
        operation = _OPERATIONS[name]
        for parameter in (operation.model, operation.target):
            if parameter:
                assert parameter in operation.signature, (name, parameter)


# ---------------------------------------------------------------------------
# Cada nome no seu lugar dentro da frase
# ---------------------------------------------------------------------------
#
# `"subject" in reason` e `"code" in reason` passam os dois com a frase
# transposta — "Campo `subject` removido de `code`". Só o fragmento renderizado
# vê a ordem, então cada linha que declara `model` e `target` traz aqui a frase
# que ela tem que produzir, escrita à mão. Nomes que não são substring um do
# outro, senão o assert volta a passar por acidente.

TWO_NAMED_SENTENCES = {
    "AddField": (
        "migrations.AddField(model_name='subject', name='nickname',"
        " field=models.CharField(max_length=50, null=True)),",
        "Campo `nickname` adicionado em `subject`.",
    ),
    "AlterField": (
        "migrations.AlterField(model_name='subject', name='nickname',"
        " field=models.CharField(max_length=50)),",
        "Campo `nickname` de `subject` alterado",
    ),
    "RemoveField": (
        "migrations.RemoveField(model_name='subject', name='nickname'),",
        "Campo `nickname` removido de `subject`",
    ),
    "RenameField": (
        "migrations.RenameField(model_name='subject', old_name='cpf',"
        " new_name='tax_id'),",
        "Campo `cpf` de `subject` renomeado",
    ),
    "RemoveIndex": (
        "migrations.RemoveIndex(model_name='subject', name='idx_updated'),",
        "Índice `idx_updated` removido de `subject`",
    ),
    "RemoveConstraint": (
        "migrations.RemoveConstraint(model_name='subject', name='uniq_external'),",
        "Constraint `uniq_external` removida de `subject`",
    ),
    "AlterOrderWithRespectTo": (
        "migrations.AlterOrderWithRespectTo(name='promptrecipe',"
        " order_with_respect_to='prompt_config'),",
        "Ordenação de `promptrecipe` passou a depender de `prompt_config`",
    ),
}


class TestEachNameLandsInItsOwnSlot:
    @pytest.mark.parametrize("name", sorted(TWO_NAMED_SENTENCES))
    def test_the_rendered_sentence_is_the_one_the_row_promises(self, name):
        operation, expected = TWO_NAMED_SENTENCES[name]
        assert expected in only(migration(operation)).reason

    def test_every_row_with_two_names_is_covered(self):
        # Linha nova com `model` e `target` entra sem frase escrita à mão e a
        # transposição volta a ser invisível.
        two_named = {
            name
            for name, operation in _OPERATIONS.items()
            if operation.model and operation.target
        }
        assert two_named == set(TWO_NAMED_SENTENCES)


# ---------------------------------------------------------------------------
# Forma do Finding
# ---------------------------------------------------------------------------

# Uma fonte por operação da tabela, mais um caso por saída de refinador e por
# fallback de expansor. `test_every_operation_in_the_table_has_a_shape_case`
# é o que impede a lista de ficar para trás quando a tabela cresce — foi a
# ausência de `AlterUniqueTogether` e `AddConstraint` aqui que deixou três
# razões que o Slack mostra começarem em minúscula sem ninguém ver.

EVERY_KIND_OF_MIGRATION = [
    migration("migrations.CreateModel(name='SplitTest', fields=[]),"),
    migration("migrations.DeleteModel(name='Tab'),"),
    migration("migrations.RenameModel(old_name='Tab', new_name='Section'),"),
    migration("migrations.AlterModelOptions(name='p', options={'ordering': ('id',)}),"),
    migration("migrations.AlterOrderWithRespectTo(name='p', order_with_respect_to='c'),"),
    migration("migrations.RemoveField(model_name='subject', name='code'),"),
    migration("migrations.RenameField(model_name='p', old_name='a', new_name='b'),"),
    # AddField: as três saídas do refinador
    migration("migrations.AddField(model_name='p', name='c', field=models.IntegerField()),"),
    migration(
        "migrations.AddField(model_name='p', name='c',"
        " field=models.IntegerField(null=True)),"
    ),
    migration("migrations.AddField(model_name='p', name='c', field=FIELD),"),
    migration("migrations.AlterField(model_name='p', name='c', field=models.IntegerField()),"),
    migration(
        "migrations.AddIndex(model_name='p',"
        " index=models.Index(fields=['c'], name='ix_p_c')),"
    ),
    migration("migrations.RemoveIndex(model_name='p', name='ix_p_c'),"),
    # AddConstraint: as três saídas do refinador
    migration(
        "migrations.AddConstraint(model_name='p',"
        " constraint=models.UniqueConstraint(fields=('c',), name='uniq_p_c')),"
    ),
    migration(
        "migrations.AddConstraint(model_name='p',"
        " constraint=models.CheckConstraint(check=Q(), name='ck_p_c')),"
    ),
    migration("migrations.AddConstraint(model_name='p', constraint=CONSTRAINT),"),
    migration("migrations.RemoveConstraint(model_name='p', name='uniq_p_c'),"),
    # AlterUniqueTogether: as três saídas do refinador
    migration("migrations.AlterUniqueTogether(name='p', unique_together={('a', 'b')}),"),
    migration("migrations.AlterUniqueTogether(name='p', unique_together=set()),"),
    migration("migrations.AlterUniqueTogether(name='p', unique_together=PAIRS),"),
    migration("migrations.RunSQL(sql='ALTER TABLE t DROP COLUMN c;'),"),
    migration("migrations.RunSQL(sql=SQL_APPLY),"),
    migration("migrations.RunPython(forward, backward),"),
    migration("migrations.SeparateDatabaseAndState(),"),
    migration("migrations.SeparateDatabaseAndState(database_operations=OPS),"),
    migration("migrations.SeparateDatabaseAndState(**SPEC),"),
    migration("AddDefaultValue(model_name='tab', name='c', value=False),"),
    migration("*EXTRA,"),
    migration("OPS['drop'](),"),
    "class Migration(migrations.Migration:\n",
]


class TestFindingShape:
    @pytest.mark.parametrize("name", sorted(_OPERATIONS))
    def test_every_reason_template_in_the_table_is_a_sentence(self, name):
        # A invariante direto na tabela, sem depender de alguém lembrar de
        # escrever uma fonte que exercite a linha.
        reason = _OPERATIONS[name].reason
        assert reason[0].isupper(), (name, reason)
        assert reason.endswith("."), (name, reason)

    def test_every_operation_in_the_table_has_a_shape_case(self):
        for name in _OPERATIONS:
            assert any(name in source for source in EVERY_KIND_OF_MIGRATION), name

    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_every_finding_has_an_operation_and_a_reason(self, source):
        findings = classify_migration(source)
        assert findings, source
        for finding in findings:
            assert finding.operation, source
            assert finding.reason, source

    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_every_reason_is_a_portuguese_sentence(self, source):
        # Pega também as razões que saem dos refinadores e dos expansores, que
        # não estão na tabela.
        for finding in classify_migration(source):
            assert finding.reason[0].isupper(), finding.reason
            assert finding.reason.endswith("."), finding.reason

    @pytest.mark.parametrize("source", EVERY_KIND_OF_MIGRATION)
    def test_no_reason_leaks_an_ast_node_type(self, source):
        # `(`Lambda`)`, `(`IfExp`)`: vocabulário do parser, não do time de dados.
        for finding in classify_migration(source):
            for node_type in ("Lambda", "IfExp", "Starred", "Subscript", "Call", "Name"):
                assert f"`{node_type}`" not in finding.reason, finding.reason

    def test_a_missing_name_does_not_leave_an_empty_hole_in_the_reason(self):
        finding = only(migration("migrations.RemoveField(**spec),"))
        assert "``" not in finding.reason
        assert "  " not in finding.reason

    def test_the_operation_is_the_django_operation_name(self):
        assert only(migration("migrations.DeleteModel(name='Tab'),")).operation == "DeleteModel"

    def test_a_run_sql_finding_keeps_the_sql_verb_as_the_operation(self):
        # A QQ-2158 agrupa por `operation`: um DROP COLUMN escrito à mão e um
        # DROP COLUMN dentro de RunSQL são a mesma operação e têm que cair no
        # mesmo balde.
        assert (
            only(migration("migrations.RunSQL(sql='ALTER TABLE t DROP COLUMN c;'),")).operation
            == "DROP COLUMN"
        )

    def test_reasons_never_echo_a_sql_literal(self):
        # `detect/sql.py` já corta antes do primeiro literal; o RunSQL não pode
        # reintroduzir o valor por outro caminho. Migração de dados no
        # consumidor Django/MySQL carrega dado de paciente.
        finding = only(
            migration(
                "migrations.RunSQL(sql=\"UPDATE subjects_subject SET name = 'Fulano';\"),"
            )
        )
        assert "Fulano" not in finding.reason
