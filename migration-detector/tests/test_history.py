"""Testes da reconstrução do estado anterior de um campo.

O `AlterField` do Django carrega só o estado final. Quem diz se a alteração
quebra é a definição anterior do mesmo par (modelo, campo), que está no
diretório `migrations/` do app — os três workflows fazem `checkout` com
`fetch-depth: 0`, então o diretório inteiro está em disco quando a action roda.

As migrações aqui são escritas à mão com a forma dos arquivos reais do
consumidor Django (591 arquivos, 248 `AlterField`). Nenhum arquivo do corpus é
copiado: a suíte de fixtures é da QQ-2161.
"""

import ast
import inspect
import re
import string
import textwrap

import pytest

from detect._reading import call_name
from detect.django import _OPERATIONS, classify_migration
from detect.history import (
    AppState,
    _COMPARISONS,
    _HANDLERS,
    _OPAQUE,
    _SCHEMA_ARGUMENTS,
    _UNIQUE_TOGETHER_COMPARISONS,
    _WITHOUT_DDL,
    _argument_of,
    _compare,
    _FieldState,
    _field_state,
    prior_state,
)
from detect.severity import DUPLICATE, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP = "subjects"


def migration_source(operations: str, dependencies: list[str], app: str = APP) -> str:
    """Um arquivo de migração com `dependencies` e `operations` literais."""
    depends = ", ".join(f"('{app}', '{stem}')" for stem in dependencies)
    return (
        "from django.db import migrations, models\n"
        "\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = [{depends}]\n"
        "    operations = [\n"
        f"{textwrap.indent(textwrap.dedent(operations).strip(), ' ' * 8)}\n"
        "    ]\n"
    )


def write_app(tmp_path, migrations: dict[str, str], dependencies=None, app: str = APP):
    """Escreve `app/migrations/*.py` e devolve o diretório.

    Sem `dependencies` explícito, cada migração depende da anterior na ordem de
    inserção — a cadeia linear que o Django gera sozinho.
    """
    directory = tmp_path / app / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    stems = list(migrations)
    for index, stem in enumerate(stems):
        parents = (dependencies or {}).get(stem, stems[index - 1 : index])
        (directory / f"{stem}.py").write_text(
            migration_source(migrations[stem], parents, app), encoding="utf-8"
        )
    return directory


def classify_last(tmp_path, migrations: dict[str, str], dependencies=None, app: str = APP):
    """Classifica a última migração escrita, com o estado anterior reconstruído."""
    directory = write_app(tmp_path, migrations, dependencies, app)
    path = directory / f"{list(migrations)[-1]}.py"
    return classify_migration(path.read_text(encoding="utf-8"), prior=prior_state(path))


def only(findings):
    assert len(findings) == 1, findings
    return findings[0]


def alter(field: str, model: str = "subject", name: str = "code") -> str:
    return (
        f"migrations.AlterField(model_name='{model}', name='{name}', field={field}),"
    )


def add(field: str, model: str = "subject", name: str = "code") -> str:
    return f"migrations.AddField(model_name='{model}', name='{name}', field={field}),"


def field_state(source: str):
    return _field_state(ast.parse(source, mode="eval").body)


# ---------------------------------------------------------------------------
# Onde o estado anterior é encontrado
# ---------------------------------------------------------------------------


class TestWhereThePreviousStateComesFrom:
    def test_the_add_field_that_created_the_column(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10, null=True)"),
                    "0002_narrow": alter("models.CharField(max_length=10)"),
                },
            )
        )
        assert finding.severity is Severity.BREAKING
        assert "passou a NOT NULL" in finding.reason

    def test_the_last_alter_field_before_this_one(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_widen": alter("models.CharField(max_length=60)"),
                    "0003_narrow": alter("models.CharField(max_length=30)"),
                },
            )
        )
        # Contra 0002 (60) é redução; contra 0001 (10) seria aumento.
        assert finding.severity is Severity.BREAKING
        assert "`60` para `30`" in finding.reason

    def test_the_create_model_that_declared_the_field(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": (
                        "migrations.CreateModel(name='Subject', fields=["
                        "('code', models.CharField(max_length=10, null=True))]),"
                    ),
                    "0002_not_null": alter("models.CharField(max_length=10)"),
                },
            )
        )
        assert finding.severity is Severity.BREAKING
        assert "passou a NOT NULL" in finding.reason

    def test_a_field_added_earlier_in_the_same_migration(self, tmp_path):
        # `schedule/0007_schedule_integration_uuid`: AddField, RunPython e
        # AlterField no mesmo arquivo. As operações do Django valem em ordem,
        # então o AddField acima é o estado anterior do AlterField abaixo.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                "0002_uuid": (
                    add("models.UUIDField(null=True)", name="uuid")
                    + "\nmigrations.RunPython(fill, migrations.RunPython.noop),\n"
                    + alter("models.UUIDField(unique=True)", name="uuid")
                ),
            },
        )
        assert findings[-1].severity is Severity.BREAKING
        assert "passou a NOT NULL" in findings[-1].reason

    def test_a_rename_field_earlier_in_the_same_migration(self, tmp_path):
        # `sync_gateway/0003_auto_20260115_0900`: RenameField seguido de
        # AlterField sobre o nome novo. O estado anterior é o do nome antigo.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add("models.PositiveIntegerField()", name="attempt"),
                "0002_rename": (
                    "migrations.RenameField(model_name='subject', old_name='attempt',"
                    " new_name='revision'),\n"
                    + alter(
                        "models.PositiveIntegerField(verbose_name='Revisão')",
                        name="revision",
                    )
                ),
            },
        )
        assert findings[-1].severity is Severity.NONE

    def test_a_nested_alter_field_reads_the_state_from_before_the_operation(self, tmp_path):
        # `bookings/0034` e `subjects/0027`: o AlterField mora em
        # `state_operations` e o DDL real é um RunPython que monta o ALTER TABLE
        # com f-string — que o `ast` não alcança. O estado anterior é a única
        # pista, e ela basta.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add(
                    "util.tsid_fields.TSIDField(blank=True, editable=False, null=True)",
                    name="external_id",
                ),
                "0002_not_null": (
                    "migrations.SeparateDatabaseAndState(\n"
                    "    state_operations=[\n"
                    "        " + alter(
                        "util.tsid_fields.TSIDField(blank=True, editable=False)",
                        name="external_id",
                    ) + "\n"
                    "    ],\n"
                    "    database_operations=[\n"
                    "        migrations.RunPython(forward, backward),\n"
                    "    ],\n"
                    "),"
                ),
            },
        )
        severities = [finding.severity for finding in findings]
        assert Severity.BREAKING in severities
        breaking = [f for f in findings if f.severity is Severity.BREAKING]
        assert only(breaking).operation == "AlterField"
        assert "passou a NOT NULL" in only(breaking).reason


# ---------------------------------------------------------------------------
# Ordenação
# ---------------------------------------------------------------------------


class TestSeparateDatabaseAndState:
    def test_the_state_branch_feeds_the_next_migration(self, tmp_path):
        # `bookings/0035` lê o `external_id` que `bookings/0034` declarou dentro de
        # `state_operations`. É esse ramo que o Django aplica ao estado; o de
        # banco executa DDL sem mexer nele.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_separate": (
                        "migrations.SeparateDatabaseAndState(\n"
                        "    state_operations=[\n"
                        "        " + alter("models.CharField(max_length=90)") + "\n"
                        "    ],\n"
                        "    database_operations=[\n"
                        "        migrations.RunPython(forward, backward),\n"
                        "    ],\n"
                        "),"
                    ),
                    "0003_narrow": alter("models.CharField(max_length=50)"),
                },
            )
        )
        # Contra 0002 (90) é redução; contra 0001 (10) seria aumento.
        assert finding.severity is Severity.BREAKING
        assert "`90` para `50`" in finding.reason


class TestOrderingInsideTheStateBranch:
    def test_two_operations_in_the_state_branch_are_read_in_order(self, tmp_path):
        # O ramo de estado é uma lista de operações do Django como outra
        # qualquer: valem em ordem. Com uma operação só no ramo, a diferença
        # entre percorrer em ordem e aplicar o bloco todo depois não aparece.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                "0002_separate": (
                    "migrations.SeparateDatabaseAndState(\n"
                    "    state_operations=[\n"
                    "        " + add("models.CharField(max_length=10, null=True)") + "\n"
                    "        " + alter("models.CharField(max_length=10)") + "\n"
                    "    ],\n"
                    "    database_operations=[\n"
                    "        migrations.RunPython(forward, backward),\n"
                    "    ],\n"
                    "),"
                ),
            },
        )
        altered = [finding for finding in findings if finding.operation == "AlterField"]
        assert only(altered).severity is Severity.BREAKING
        assert "passou a NOT NULL" in only(altered).reason

    def test_a_branch_that_is_simply_absent_does_not_give_up_the_state(self, tmp_path):
        # `bookings/0034` e `subjects/0027` só têm `database_operations`, e a
        # maioria das migrações de índice do corpus também. Ramo ausente não é
        # ramo ilegível: tratar os dois igual apagaria o estado do app inteiro
        # a partir da primeira migração assim.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_only_database": (
                        "migrations.SeparateDatabaseAndState(\n"
                        "    database_operations=[\n"
                        "        migrations.RunSQL(sql='CREATE INDEX ix ON t (c);'),\n"
                        "    ],\n"
                        "),"
                    ),
                    "0003_widen": alter("models.CharField(max_length=50)"),
                },
            )
        )
        assert finding.severity is Severity.CONTROLLED
        assert "`10` para `50`" in finding.reason

    def test_a_separate_database_and_state_built_from_a_variable_gives_up(self, tmp_path):
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_unpacked": (
                    "migrations.SeparateDatabaseAndState(**SPEC),\n"
                    + alter("models.CharField(max_length=50)")
                ),
            },
        )
        assert findings[-1].severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in findings[-1].reason

    def test_an_ancestor_with_an_unreadable_state_branch_gives_up_the_state(self, tmp_path):
        # A releitura dos ancestrais não passa por `_Walker`: quem desconfia
        # nesse caminho é o handler do `AppState`, e ele tem o seu próprio ramo
        # de "não deu para ler".
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_separate": (
                        "migrations.SeparateDatabaseAndState(state_operations=OPS),"
                    ),
                    "0003_widen": alter("models.CharField(max_length=50)"),
                },
            )
        )
        assert finding.severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in finding.reason

    def test_the_state_branch_is_applied_once_and_the_block_never_again(self, tmp_path):
        # O expansor percorre o ramo operação a operação; o laço de fora não
        # pode reaplicar o bloco inteiro em cima disso. Nenhum handler de hoje
        # se importa com aplicar duas vezes, e é justamente por isso que só um
        # espião vê a diferença antes de algum passar a se importar.
        applied = []

        class Spy:
            def field_change(self, model, name, definition):
                return None

            def unique_together_change(self, model, pairs):
                return None

            def apply(self, node):
                applied.append(call_name(node))

            def claim(self):
                pass

            def distrust(self):
                pass

        source = migration_source(
            "migrations.SeparateDatabaseAndState(\n"
            "    state_operations=[\n"
            "        " + add("models.CharField(max_length=10, null=True)") + "\n"
            "        " + alter("models.CharField(max_length=10)") + "\n"
            "    ],\n"
            "),",
            ["0001_initial"],
        )
        classify_migration(source, prior=Spy())
        assert applied == ["AddField", "AlterField"]

    def test_a_state_branch_that_cannot_be_read_gives_up_the_state(self, tmp_path):
        # As operações escondidas no ramo são justamente as que mudariam o
        # campo. Seguir com o que já estava na memória compararia contra a
        # definição de antes delas: 10 para 50 sairia como aumento.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_separate": (
                    "migrations.SeparateDatabaseAndState(state_operations=OPS),\n"
                    + alter("models.CharField(max_length=50)")
                ),
            },
        )
        assert findings[-1].operation == "AlterField"
        assert findings[-1].severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in findings[-1].reason


class TestOrdering:
    def test_the_dependency_chain_beats_the_numeric_prefix(self, tmp_path):
        # Prefixo menor, aplicada depois: quem manda é a cadeia.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0003_widen": alter("models.CharField(max_length=90)"),
                    "0002_narrow_later": alter("models.CharField(max_length=40)"),
                },
                dependencies={
                    "0003_widen": ["0001_initial"],
                    "0002_narrow_later": ["0003_widen"],
                },
            )
        )
        assert finding.severity is Severity.BREAKING
        assert "`90` para `40`" in finding.reason

    def test_the_numeric_prefix_breaks_the_tie_between_two_parents(self, tmp_path):
        # Merge: dois pais sem ordem entre si. `ledger/0005_merge_20171114_2226`
        # é essa forma. O desempate é o prefixo, então 0002 aplica antes de 0003.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_branch_a": alter("models.CharField(max_length=20)"),
                    "0003_branch_b": alter("models.CharField(max_length=30)"),
                    "0004_merge": alter("models.CharField(max_length=25)"),
                },
                dependencies={
                    "0002_branch_a": ["0001_initial"],
                    "0003_branch_b": ["0001_initial"],
                    "0004_merge": ["0002_branch_a", "0003_branch_b"],
                },
            )
        )
        # Último a aplicar é 0003 (30); 25 é redução.
        assert finding.severity is Severity.BREAKING
        assert "`30` para `25`" in finding.reason

    def test_the_tie_break_reads_the_prefix_as_a_number_not_as_text(self, tmp_path):
        # Nome sem zeros à esquerda: em texto `10_late` vem antes de `2_early`,
        # em número não. Os dois são pais do merge, então só o desempate decide
        # qual aplica por último.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "2_early": alter("models.CharField(max_length=20)"),
                    "10_late": alter("models.CharField(max_length=90)"),
                    "0011_merge": alter("models.CharField(max_length=50)"),
                },
                dependencies={
                    "2_early": ["0001_initial"],
                    "10_late": ["0001_initial"],
                    "0011_merge": ["2_early", "10_late"],
                },
            )
        )
        # Último a aplicar é `10_late` (90); 50 é redução.
        assert finding.severity is Severity.BREAKING
        assert "`90` para `50`" in finding.reason

    def test_a_branch_the_chain_does_not_reach_is_not_read(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_other_branch": alter("models.CharField(max_length=99)"),
                    "0003_here": alter("models.CharField(max_length=50)"),
                },
                dependencies={
                    "0002_other_branch": ["0001_initial"],
                    "0003_here": ["0001_initial"],
                },
            )
        )
        # Contra 0001 (10) é aumento; se 0002 (99) tivesse sido lida, seria redução.
        assert finding.severity is Severity.CONTROLLED
        assert "`10` para `50`" in finding.reason

    def test_a_missing_ancestor_leaves_the_state_unknown(self, tmp_path):
        # 0001 continua em disco e sozinho daria uma resposta confiante — 10
        # para 20 é aumento. Só que a definição que valia era a de 0002, que
        # sumiu; pular o buraco devolveria o estado de antes dele.
        directory = write_app(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_widen": alter("models.CharField(max_length=40)"),
                "0003_narrow": alter("models.CharField(max_length=20)"),
            },
        )
        (directory / "0002_widen.py").unlink()
        path = directory / "0003_narrow.py"
        path.write_text(
            migration_source(
                alter("models.CharField(max_length=20)"), ["0002_widen", "0001_initial"]
            ),
            encoding="utf-8",
        )
        finding = only(
            classify_migration(path.read_text(encoding="utf-8"), prior=prior_state(path))
        )
        assert finding.severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in finding.reason

    def test_a_cycle_among_the_ancestors_leaves_the_state_unknown(self, tmp_path):
        # Reaplicar parte de uma cadeia com ciclo daria um estado anterior
        # *errado* — a definição de uma migração que talvez nem tenha rodado
        # antes. Errado é pior que ausente, então a resposta é `unknown`.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_a": alter("models.CharField(max_length=20)"),
                    "0003_b": alter("models.CharField(max_length=30)"),
                    "0004_here": alter("models.CharField(max_length=5)"),
                },
                dependencies={
                    "0002_a": ["0003_b"],
                    "0003_b": ["0002_a"],
                    # 0001 é legível e sozinho daria uma resposta confiante:
                    # contra `max_length=10`, 5 é redução. A cadeia com ciclo
                    # não pode ser reaplicada pela metade.
                    "0004_here": ["0003_b", "0001_initial"],
                },
            )
        )
        assert finding.severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in finding.reason


class TestTheStateBelongsToOneMigration:
    """O `AppState` avança enquanto o arquivo é lido; reusá-lo mistura arquivos.

    Memoizar `prior_state` por app e passar o mesmo objeto para vários arquivos
    da PR faria as operações do primeiro entrarem no estado anterior do segundo
    — exatamente a severidade confiante e errada que este módulo existe para
    não produzir. Quem quiser economizar memoiza os ancestrais lidos.
    """

    def two_migrations(self, tmp_path):
        directory = write_app(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_widen": alter("models.CharField(max_length=90)"),
                "0003_narrow": alter("models.CharField(max_length=50)"),
            },
        )
        return directory / "0002_widen.py", directory / "0003_narrow.py"

    def test_reusing_the_same_state_for_a_second_migration_raises(self, tmp_path):
        first, second = self.two_migrations(tmp_path)
        state = prior_state(first)
        classify_migration(first.read_text(encoding="utf-8"), prior=state)
        with pytest.raises(RuntimeError) as raised:
            classify_migration(second.read_text(encoding="utf-8"), prior=state)
        assert "prior_state" in str(raised.value)

    def test_a_fresh_state_per_migration_is_what_works(self, tmp_path):
        first, second = self.two_migrations(tmp_path)
        assert (
            only(
                classify_migration(
                    first.read_text(encoding="utf-8"), prior=prior_state(first)
                )
            ).severity
            is Severity.CONTROLLED
        )
        # 0003 compara contra 0002 (90), não contra o que 0002 leu.
        finding = only(
            classify_migration(second.read_text(encoding="utf-8"), prior=prior_state(second))
        )
        assert finding.severity is Severity.BREAKING
        assert "`90` para `50`" in finding.reason


class TestAnAncestorThatCannotBeReadEntirely:
    """Meia cadeia daria um estado anterior desatualizado, não ausente.

    Uma operação que o classificador não lê é justamente uma que pode ter
    mudado o campo. Reaplicar só o resto devolveria a definição de antes dela —
    e uma severidade confiante calculada em cima da definição errada é o defeito
    que este classificador existe para corrigir.
    """

    def unknown(self, tmp_path, ancestor: str):
        # 0001 é legível e daria uma resposta confiante — contra `max_length=10`
        # o novo valor 20 seria aumento. Quem decide é 0002, que não dá para ler.
        directory = tmp_path / APP / "migrations"
        directory.mkdir(parents=True)
        (directory / "0001_initial.py").write_text(
            migration_source(add("models.CharField(max_length=10)"), []), encoding="utf-8"
        )
        (directory / "0002_opaque.py").write_text(ancestor, encoding="utf-8")
        path = directory / "0003_alter.py"
        path.write_text(
            migration_source(
                alter("models.CharField(max_length=20)"), ["0002_opaque", "0001_initial"]
            ),
            encoding="utf-8",
        )
        finding = only(
            classify_migration(path.read_text(encoding="utf-8"), prior=prior_state(path))
        )
        assert finding.severity is Severity.UNKNOWN, finding
        assert "não conhece a definição anterior" in finding.reason

    def test_an_ancestor_whose_operations_list_is_not_a_literal(self, tmp_path):
        self.unknown(
            tmp_path,
            "from django.db import migrations, models\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('subjects', '0001_initial')]\n"
            "    operations = BASE + EXTRA\n",
        )

    def test_an_ancestor_that_appends_to_operations(self, tmp_path):
        self.unknown(
            tmp_path,
            "from django.db import migrations, models\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('subjects', '0001_initial')]\n"
            "    operations = []\n"
            "    for column in COLUMNS:\n"
            "        operations.append(migrations.RemoveField('subject', column))\n",
        )

    def test_an_ancestor_whose_migration_class_is_conditional(self, tmp_path):
        self.unknown(
            tmp_path,
            "from django.db import migrations, models\n"
            "if LEGACY:\n"
            "    class Migration(migrations.Migration):\n"
            "        dependencies = [('subjects', '0001_initial')]\n"
            "        operations = []\n",
        )

    def test_an_ancestor_that_is_not_valid_python(self, tmp_path):
        self.unknown(tmp_path, "class Migration(migrations.Migration:\n")

    # Ancestral legível, operação legível, nome que não é literal. O handler
    # não sabe qual campo ou modelo a operação mexeu, e pular a operação deixa
    # no estado a definição que ela pode ter substituído. `FIELD = "code"` é o
    # caso normal: 0002 mergeia num PR, 0003 vem no seguinte, e só o 0003 é
    # classificado.
    NON_LITERAL_NAMES = {
        "add_field_name": "migrations.AddField(model_name='subject', name=FIELD, "
        "field=models.CharField(max_length=40)),",
        "add_field_model": "migrations.AddField(model_name=MODEL, name='code', "
        "field=models.CharField(max_length=40)),",
        "alter_field_name": "migrations.AlterField(model_name='subject', name=FIELD, "
        "field=models.CharField(max_length=40)),",
        "remove_field_name": "migrations.RemoveField(model_name='subject', name=FIELD),",
        "rename_field_old": "migrations.RenameField(model_name='subject', old_name=OLD, "
        "new_name='code'),",
        "rename_field_new": "migrations.RenameField(model_name='subject', old_name='code', "
        "new_name=NEW),",
        "rename_field_model": "migrations.RenameField(model_name=MODEL, old_name='code', "
        "new_name='legacy_code'),",
        "rename_model_old": "migrations.RenameModel(old_name=OLD, new_name='Subject'),",
        "rename_model_new": "migrations.RenameModel(old_name='Subject', new_name=NEW),",
        "delete_model_name": "migrations.DeleteModel(name=MODEL),",
        "create_model_name": "migrations.CreateModel(name=MODEL, fields=[]),",
        "alter_unique_together_name": "migrations.AlterUniqueTogether(name=MODEL, "
        "unique_together={('code', 'id')}),",
    }

    @pytest.mark.parametrize("shape", sorted(NON_LITERAL_NAMES))
    def test_an_ancestor_operation_whose_name_is_not_a_literal(self, tmp_path, shape):
        self.unknown(
            tmp_path, migration_source(self.NON_LITERAL_NAMES[shape], ["0001_initial"])
        )

    def test_the_names_cover_every_handler_that_reads_a_name(self):
        # `SeparateDatabaseAndState` não lê nome e tem os próprios testes. Um
        # handler novo sem caso aqui é um `return` que ninguém viu desistir.
        called = {
            call_name(ast.parse(source.rstrip(","), mode="eval").body)
            for source in self.NON_LITERAL_NAMES.values()
        }
        assert called == set(_HANDLERS) - {"SeparateDatabaseAndState"}


# ---------------------------------------------------------------------------
# Sem estado anterior: unknown, nunca um palpite
# ---------------------------------------------------------------------------


class TestWithoutPreviousState:
    """Cada caminho que não acha o estado anterior devolve `unknown`."""

    def unknown(self, tmp_path, migrations, dependencies=None):
        finding = only(classify_last(tmp_path, migrations, dependencies))
        assert finding.severity is Severity.UNKNOWN, finding
        assert "não conhece a definição anterior" in finding.reason
        return finding

    def test_a_model_that_lives_in_another_app(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                "0002_alter": alter("models.CharField(max_length=10)", model="supplier"),
            },
        )

    def test_a_field_inherited_from_the_base_state(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": (
                    "migrations.CreateModel(name='Subject',"
                    " fields=[('id', models.AutoField(primary_key=True))]),"
                ),
                "0002_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_a_field_that_came_from_an_abstract_model(self, tmp_path):
        # O `CreateModel` do app não lista o campo herdado do modelo abstrato.
        self.unknown(
            tmp_path,
            {
                "0001_initial": (
                    "migrations.CreateModel(name='Subject', fields=[],"
                    " bases=('core.TimestampedModel',)),"
                ),
                "0002_alter": alter("models.DateTimeField(auto_now=True)", name="_updated_at"),
            },
        )

    def test_a_field_removed_before_this_migration(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_remove": "migrations.RemoveField(model_name='subject', name='code'),",
                "0003_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_a_model_deleted_before_this_migration(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_delete": "migrations.DeleteModel(name='Subject'),",
                "0003_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_the_old_name_after_a_rename_field(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_rename": (
                    "migrations.RenameField(model_name='subject', old_name='code',"
                    " new_name='tax_id'),"
                ),
                "0003_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_the_old_name_after_a_rename_model(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_rename": (
                    "migrations.RenameModel(old_name='Subject', new_name='Person'),"
                ),
                "0003_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_a_previous_definition_that_is_not_a_readable_call(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("CODE_FIELD"),
                "0002_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_a_previous_definition_built_from_unpacked_arguments(self, tmp_path):
        self.unknown(
            tmp_path,
            {
                "0001_initial": add("models.CharField(**CODE)"),
                "0002_alter": alter("models.CharField(max_length=10)"),
            },
        )

    def test_a_migration_without_a_prior_state_map_stays_unknown(self):
        # `classify_migration` sem `prior` continua respondendo como antes: quem
        # chama pode não ter o diretório `migrations/` em mãos.
        source = migration_source(alter("models.CharField(max_length=10)"), ["0001_initial"])
        finding = only(classify_migration(source))
        assert finding.severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in finding.reason

    def test_the_new_definition_that_is_not_a_readable_call(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_alter": alter("CODE_FIELD"),
                },
            )
        )
        assert finding.severity is Severity.UNKNOWN
        assert "definição" in finding.reason


# ---------------------------------------------------------------------------
# A tabela de comparação
# ---------------------------------------------------------------------------
#
# Uma entrada por linha da tabela: a definição anterior, a nova, a severidade e
# a frase renderizada inteira. A frase escrita à mão é o que pega transposição
# de nomes — `"code" in "subject_code"` passa por acidente, e dois `in`
# separados não veem a troca de lugar entre modelo e campo.

COMPARISONS = {
    "classe do campo mudou": (
        "models.IntegerField()",
        "models.BigIntegerField()",
        Severity.BREAKING,
        "Campo `code` de `subject` mudou de `IntegerField` para `BigIntegerField`",
    ),
    "campo passou a NOT NULL": (
        "models.CharField(max_length=10, null=True)",
        "models.CharField(max_length=10)",
        Severity.BREAKING,
        "Campo `code` de `subject` passou a NOT NULL",
    ),
    "campo passou a UNIQUE": (
        "models.CharField(max_length=10)",
        "models.CharField(max_length=10, unique=True)",
        Severity.BREAKING,
        "Campo `code` de `subject` passou a UNIQUE",
    ),
    "max_length reduzido": (
        "models.CharField(max_length=30)",
        "models.CharField(max_length=10)",
        Severity.BREAKING,
        "Campo `code` de `subject` encurtou de `30` para `10`",
    ),
    "max_length aumentado": (
        "models.CharField(max_length=10)",
        "models.CharField(max_length=30)",
        Severity.CONTROLLED,
        "Campo `code` de `subject` alongou de `10` para `30`",
    ),
    "campo passou a aceitar nulo": (
        "models.CharField(max_length=10)",
        "models.CharField(max_length=10, null=True)",
        Severity.CONTROLLED,
        "Campo `code` de `subject` passou a aceitar nulo",
    ),
    "unique removido": (
        "models.CharField(max_length=10, unique=True)",
        "models.CharField(max_length=10)",
        Severity.CONTROLLED,
        "Campo `code` de `subject` deixou de ser UNIQUE",
    ),
    "db_index alterado": (
        "models.CharField(max_length=10)",
        "models.CharField(max_length=10, db_index=True)",
        Severity.CONTROLLED,
        "Índice do campo `code` de `subject` mudou",
    ),
    "precisão numérica alterada": (
        "models.DecimalField(max_digits=6, decimal_places=2)",
        "models.DecimalField(max_digits=10, decimal_places=2)",
        Severity.CONTROLLED,
        "Precisão numérica do campo `code` de `subject` mudou",
    ),
    "argumento de schema não literal": (
        "models.CharField(max_length=10)",
        "models.CharField(max_length=LIMIT)",
        Severity.UNKNOWN,
        "Campo `code` de `subject` tem argumento de schema que não é literal",
    ),
    "max_length passou a ser declarado": (
        "models.FileField()",
        "models.FileField(max_length=255)",
        Severity.UNKNOWN,
        "Campo `code` de `subject` passou a declarar `max_length` `255`",
    ),
    "max_length deixou de ser declarado": (
        "models.FileField(max_length=255)",
        "models.FileField()",
        Severity.UNKNOWN,
        "Campo `code` de `subject` deixou de declarar `max_length` `255`",
    ),
    "argumento fora da tabela": (
        "models.ForeignKey(to='subjects.Chart', on_delete=models.CASCADE)",
        "models.ForeignKey(to='subjects.Visit', on_delete=models.CASCADE)",
        Severity.UNKNOWN,
        "Campo `code` de `subject` mudou `to`",
    ),
    "sem alteração de schema": (
        "models.CharField(max_length=10, verbose_name='código')",
        "models.CharField(max_length=10, verbose_name='código do paciente',"
        " help_text='ajuda', choices=[('a', 'A')], default='a', blank=True)",
        Severity.NONE,
        "Campo `code` de `subject` alterado sem DDL",
    ),
}


class TestComparisonTable:
    @pytest.mark.parametrize("row", sorted(COMPARISONS))
    def test_the_row_gives_the_severity_and_the_sentence_it_promises(self, row, tmp_path):
        before, after, severity, sentence = COMPARISONS[row]
        finding = only(
            classify_last(
                tmp_path, {"0001_initial": add(before), "0002_alter": alter(after)}
            )
        )
        assert finding.severity is severity, finding
        assert sentence in finding.reason, finding.reason

    def test_every_row_of_the_table_is_covered(self):
        assert {comparison.name for comparison in _COMPARISONS} == set(COMPARISONS)

    def test_the_first_matching_row_wins(self, tmp_path):
        # Encurtar e passar a NOT NULL ao mesmo tempo: a linha mais grave é a
        # que responde, porque a tabela está ordenada da mais grave para a mais
        # branda.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=30, null=True)"),
                    "0002_alter": alter("models.CharField(max_length=10)"),
                },
            )
        )
        assert finding.severity is Severity.BREAKING
        assert "passou a NOT NULL" in finding.reason

    def test_an_argument_written_positionally_is_the_same_as_by_keyword(self, tmp_path):
        # `subjects/0028_add_public_api_origin_choice` escreve o verbose_name
        # como primeiro posicional. Não é DDL nos dois casos.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_alter": alter("models.CharField('origem', max_length=10)"),
                },
            )
        )
        assert finding.severity is Severity.NONE

    def test_a_model_reference_is_compared_without_case(self, tmp_path):
        # `journal/0020` reescreve `to='ml_gateway.PromptRun'` como
        # `to='ml_gateway.promptrun'`; o Django normaliza, o alvo é o mesmo.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add(
                        "models.ForeignKey(to='subjects.Chart', on_delete=models.CASCADE)"
                    ),
                    "0002_alter": alter(
                        "models.ForeignKey(to='subjects.chart',"
                        " on_delete=models.CASCADE, null=True)"
                    ),
                },
            )
        )
        assert finding.severity is Severity.CONTROLLED
        assert "passou a aceitar nulo" in finding.reason

    def test_an_absent_boolean_reads_as_the_django_default(self, tmp_path):
        # `null` ausente é `null=False`; sem isso `null=True` -> ausente não
        # seria lido como NOT NULL, que é justamente `ledger/0042`.
        state = field_state("models.CharField(max_length=10)")
        assert "null" not in state.arguments
        assert _argument_of(state, "null") is False
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10, null=False)"),
                    "0002_alter": alter("models.CharField(max_length=10)"),
                },
            )
        )
        assert finding.severity is Severity.NONE


class TestTheComparisonTableIsATable:
    """As invariantes que a QQ-2162 vai precisar quando o time de dados editar."""

    @pytest.mark.parametrize("index", range(len(_COMPARISONS)))
    def test_every_reason_is_a_portuguese_sentence(self, index):
        reason = _COMPARISONS[index].reason
        assert reason[0].isupper(), reason
        assert reason.endswith("."), reason

    @pytest.mark.parametrize("index", range(len(_COMPARISONS)))
    def test_every_reason_only_cites_names_the_row_supplies(self, index):
        comparison = _COMPARISONS[index]
        cited = {
            field for _, field, _, _ in string.Formatter().parse(comparison.reason) if field
        }
        assert cited <= {"model", "target", "before", "after", "arguments"}, comparison.name
        if cited & {"before", "after", "arguments"}:
            assert comparison.detail is not None, comparison.name
        if comparison.detail is None:
            assert not cited - {"model", "target"}, comparison.name

    def test_every_row_name_is_unique(self):
        names = [comparison.name for comparison in _COMPARISONS]
        assert len(names) == len(set(names))

    def test_the_table_is_ordered_by_precedence(self):
        # `_compare` devolve a primeira linha que casa, então a ordem é a
        # precedência — o específico antes do geral. A tabela não é ordenada por
        # severidade: as duas linhas de `max_length` implícito são `unknown` e
        # ficam abaixo de linhas `controlled`, de propósito.
        names = [comparison.name for comparison in _COMPARISONS]

        # A guarda de leitura vem antes de toda linha que lê argumento de schema.
        assert names[0] == "argumento de schema não literal"

        # Quebra antes de controlada: encurtar e passar a NOT NULL ao mesmo
        # tempo tem que responder pela quebra.
        breaking = [i for i, c in enumerate(_COMPARISONS) if c.severity is Severity.BREAKING]
        controlled = [i for i, c in enumerate(_COMPARISONS) if c.severity is Severity.CONTROLLED]
        assert max(breaking) < min(controlled)

        # Comprimento implícito só depois das duas que comparam comprimentos
        # escritos, que é o que sobra para elas.
        for written in ("max_length reduzido", "max_length aumentado"):
            assert names.index(written) < names.index("max_length passou a ser declarado")
            assert names.index(written) < names.index("max_length deixou de ser declarado")

        # E só a última casa com tudo.
        assert names[-1] == "sem alteração de schema"
        assert _COMPARISONS[-1].severity is Severity.NONE

    def test_only_the_last_row_matches_two_identical_definitions(self):
        # Uma linha acima da última que casasse com "nada mudou" engoliria todas
        # as diferenças abaixo dela. É o mesmo defeito que a linha de
        # `max_length` tinha: nome específico, predicado geral.
        same = field_state("models.CharField(max_length=10, null=True, db_index=True)")
        for comparison in _COMPARISONS[:-1]:
            assert not comparison.applies(same, same), comparison.name
        assert _COMPARISONS[-1].applies(same, same)

    def test_a_value_with_a_brace_survives_the_second_format(self):
        # A razão sai daqui com `{model}` e `{target}` por preencher e leva um
        # segundo `format` em `detect/django.py`. Uma chave vinda de um valor
        # viraria campo de formatação lá e derrubaria o arquivo inteiro.
        severity, reason = _compare(_FieldState("Char{Field}", {}), _FieldState("Text", {}))
        assert severity is Severity.BREAKING
        rendered = reason.format(model="`subject`", target="`code`")
        assert "`Char{Field}`" in rendered

    def test_no_argument_is_both_without_ddl_and_a_schema_argument(self):
        assert not _WITHOUT_DDL & set(_SCHEMA_ARGUMENTS)


class TestTheHandlersAgreeWithTheOperationTable:
    """A segunda ponta do acoplamento com `detect/django.py`.

    Os handlers leem `fields`, `options`, `old_name`, `new_name`,
    `unique_together` e `state_operations` por posição, usando a `signature`
    declarada na tabela de lá. Encurtar uma dessas tuplas não quebra nenhum
    teste do outro módulo e faz a reconstrução do estado parar de enxergar o
    argumento em silêncio — o que sai depois é uma severidade confiante
    calculada sobre um estado incompleto.
    """

    def test_every_handler_belongs_to_an_operation(self):
        # Handler sob uma chave que a tabela não tem não conseguiria ler
        # parâmetro nenhum por posição.
        assert set(_HANDLERS) <= set(_OPERATIONS)

    @pytest.mark.parametrize("name", sorted(_HANDLERS))
    def test_the_parameters_each_handler_reads_are_in_the_signature(self, name):
        read = set(
            re.findall(
                r'_argument\(operation, call, "(\w+)"\)',
                inspect.getsource(_HANDLERS[name]),
            )
        )
        assert read, name
        signature = set(_OPERATIONS[name].signature)
        assert read <= signature, (name, sorted(read - signature))

    def test_a_handler_without_a_row_in_the_table_does_not_crash(self, monkeypatch):
        # `test_every_handler_belongs_to_an_operation` é o que impede isto de
        # existir; a guarda é o que faz o dia em que existir sair como `unknown`
        # em vez de derrubar a classificação do arquivo com `KeyError`.
        monkeypatch.setitem(_HANDLERS, "Frobnicate", AppState._delete_model)
        state = AppState()
        # Posicional de propósito: é a leitura por posição que depende da
        # `signature` da linha, e é ela que estoura sem a linha.
        state.apply(ast.parse("migrations.Frobnicate('Subject')", mode="eval").body)

    def test_an_operation_outside_the_table_does_not_crash_the_walk(self, tmp_path):
        # `_OPERATIONS[name]` direto seria `KeyError` no meio da classificação,
        # que derruba o arquivo inteiro em vez de devolver um `unknown`.
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_third_party": (
                    "AddDefaultValue(model_name='subject', name='code', value='x'),\n"
                    + alter("models.CharField(max_length=50)")
                ),
            },
        )
        assert [finding.severity for finding in findings] == [
            Severity.UNKNOWN,
            Severity.CONTROLLED,
        ]


# ---------------------------------------------------------------------------
# AlterUniqueTogether
# ---------------------------------------------------------------------------


def unique_together(pairs: str, model: str = "subject") -> str:
    return f"migrations.AlterUniqueTogether(name='{model}', unique_together={pairs}),"


UNIQUE_TOGETHER_COMPARISONS = {
    "tabela criada nesta migração": (
        "migrations.CreateModel(name='Subject', fields=[]),\n"
        + unique_together("{('clinic', 'code')}"),
        Severity.SAFE,
        "Meta `unique_together` de `subject` definida junto com a tabela",
    ),
    "combinação única nova": (
        unique_together("{('clinic', 'code'), ('clinic', 'cpf')}"),
        Severity.BREAKING,
        "Meta `unique_together` de `subject` passou a exigir combinação única",
    ),
    "combinação única removida": (
        unique_together("set()"),
        Severity.CONTROLLED,
        "Meta `unique_together` de `subject` deixou de exigir",
    ),
    "combinação única inalterada": (
        unique_together("{('clinic', 'code')}"),
        Severity.NONE,
        "Meta `unique_together` de `subject` reescrita sem mudança",
    ),
}


class TestAlterUniqueTogether:
    def test_a_pair_added_to_a_table_that_already_exists_is_breaking(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                    "0002_unique": unique_together("{('clinic', 'code')}"),
                },
            )
        )
        assert finding.severity is Severity.BREAKING

    def test_a_pair_added_to_a_table_created_here_is_safe(self, tmp_path):
        # 14 das 20 ocorrências do corpus são desta forma: CreateModel e
        # AlterUniqueTogether no mesmo arquivo, sobre tabela vazia.
        findings = classify_last(
            tmp_path,
            {
                "0001_other": "migrations.CreateModel(name='Chart', fields=[]),",
                "0002_create": UNIQUE_TOGETHER_COMPARISONS["tabela criada nesta migração"][0],
            },
        )
        assert findings[-1].severity is Severity.SAFE

    def test_dropping_one_of_two_pairs_is_controlled(self, tmp_path):
        # É o caso que a heurística sem estado anterior não sabia distinguir:
        # sobrar uma combinação não é exigir uma nova.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                    "0002_two": unique_together("{('clinic', 'code'), ('clinic', 'cpf')}"),
                    "0003_one": unique_together("{('clinic', 'code')}"),
                },
            )
        )
        assert finding.severity is Severity.CONTROLLED
        assert "deixou de exigir" in finding.reason

    def test_reordering_the_same_pairs_is_none(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": "migrations.CreateModel(name='Subject', fields=[]),",
                    "0002_two": unique_together("{('clinic', 'code'), ('clinic', 'cpf')}"),
                    "0003_same": unique_together("{('clinic', 'cpf'), ('clinic', 'code')}"),
                },
            )
        )
        assert finding.severity is Severity.NONE

    def test_the_pairs_declared_by_create_model_options_count(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": (
                        "migrations.CreateModel(name='Subject', fields=[],"
                        " options={'unique_together': {('clinic', 'code')}}),"
                    ),
                    "0002_same": unique_together("{('clinic', 'code')}"),
                },
            )
        )
        assert finding.severity is Severity.NONE

    def test_a_model_without_previous_state_keeps_the_heuristic(self, tmp_path):
        # Sem estado anterior a resposta é a da QQ-2155: exigir combinação
        # única é tratado como quebra.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": "migrations.CreateModel(name='Chart', fields=[]),",
                    "0002_unique": unique_together("{('clinic', 'code')}"),
                },
            )
        )
        assert finding.severity is Severity.BREAKING
        assert finding.reason.endswith(DUPLICATE)

    def test_a_model_seen_only_through_a_field_keeps_the_heuristic(self, tmp_path):
        # `AddField` não estabelece `unique_together`: o modelo pode ter sido
        # criado por um `CreateModel` que este app não escreveu, com pares que o
        # classificador nunca viu. Tratar isso como conjunto vazio faria um
        # `unique_together=set()` sair como "nada mudou" em vez de "o índice
        # único some".
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_empty": unique_together("set()"),
                },
            )
        )
        assert finding.severity is Severity.CONTROLLED
        assert "esvaziada" in finding.reason

    @pytest.mark.parametrize("row", sorted(UNIQUE_TOGETHER_COMPARISONS))
    def test_the_row_gives_the_severity_and_the_sentence_it_promises(self, row, tmp_path):
        operations, severity, sentence = UNIQUE_TOGETHER_COMPARISONS[row]
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": (
                    "migrations.CreateModel(name='Subject', fields=[],"
                    " options={'unique_together': {('clinic', 'code')}}),"
                ),
                "0002_alter": operations,
            },
        )
        assert findings[-1].severity is severity, findings
        assert sentence in findings[-1].reason, findings[-1].reason

    def test_every_row_of_the_table_is_covered(self):
        assert {row.name for row in _UNIQUE_TOGETHER_COMPARISONS} == set(
            UNIQUE_TOGETHER_COMPARISONS
        )

    @pytest.mark.parametrize("index", range(len(_UNIQUE_TOGETHER_COMPARISONS)))
    def test_every_reason_is_a_sentence_that_only_cites_the_model(self, index):
        comparison = _UNIQUE_TOGETHER_COMPARISONS[index]
        assert comparison.reason[0].isupper(), comparison.reason
        assert comparison.reason.endswith("."), comparison.reason
        cited = {
            field for _, field, _, _ in string.Formatter().parse(comparison.reason) if field
        }
        assert cited <= {"model"}, comparison.name

    def test_the_table_is_ordered_by_precedence(self):
        # A linha de tabela nova vem antes da de par novo de propósito: as duas
        # casam quando a tabela é criada aqui, e a que responde tem que ser a
        # que sabe que a tabela ainda está vazia.
        names = [row.name for row in _UNIQUE_TOGETHER_COMPARISONS]
        assert names.index("tabela criada nesta migração") < names.index(
            "combinação única nova"
        )
        assert names[-1] == "combinação única inalterada"
        assert _UNIQUE_TOGETHER_COMPARISONS[-1].severity is Severity.NONE

    def test_only_the_last_row_matches_an_unchanged_set(self):
        same = frozenset({("clinic", "code")})
        for row in _UNIQUE_TOGETHER_COMPARISONS[:-1]:
            assert not row.applies(same, same, False), row.name
        assert _UNIQUE_TOGETHER_COMPARISONS[-1].applies(same, same, False)



# ---------------------------------------------------------------------------
# O que o estado anterior não muda
# ---------------------------------------------------------------------------


class TestTheRestOfTheClassifierIsUntouched:
    def test_the_operation_of_an_alter_field_finding_is_still_alter_field(self, tmp_path):
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_alter": alter("models.CharField(max_length=30)"),
                },
            )
        )
        assert finding.operation == "AlterField"

    def test_one_finding_per_operation_in_order(self, tmp_path):
        findings = classify_last(
            tmp_path,
            {
                "0001_initial": add("models.CharField(max_length=10)"),
                "0002_many": (
                    alter("models.CharField(max_length=30)")
                    + "\nmigrations.RemoveField(model_name='subject', name='code'),"
                ),
            },
        )
        assert [finding.operation for finding in findings] == ["AlterField", "RemoveField"]

    def test_every_reason_is_a_portuguese_sentence(self, tmp_path):
        for before, after, _, _ in COMPARISONS.values():
            findings = classify_last(
                tmp_path, {"0001_initial": add(before), "0002_alter": alter(after)}
            )
            for finding in findings:
                assert finding.reason[0].isupper(), finding.reason
                assert finding.reason.endswith("."), finding.reason

    def test_no_reason_echoes_a_literal_from_the_field(self, tmp_path):
        # `choices` e `default` carregam dado de negócio; a razão cita nome de
        # coluna, nunca valor.
        finding = only(
            classify_last(
                tmp_path,
                {
                    "0001_initial": add("models.CharField(max_length=10)"),
                    "0002_alter": alter(
                        "models.CharField(max_length=10, default='Fulano',"
                        " choices=[('x', 'Fulano')])"
                    ),
                },
            )
        )
        assert "Fulano" not in finding.reason


# ---------------------------------------------------------------------------
# Leitura de um campo
# ---------------------------------------------------------------------------


class TestFieldState:
    def test_a_field_that_is_not_a_call_is_unreadable(self):
        assert field_state("CODE_FIELD") is None

    def test_a_field_with_unpacked_keywords_is_unreadable(self):
        assert field_state("models.CharField(**CODE)") is None

    def test_the_class_is_the_last_name_of_the_dotted_path(self):
        assert field_state("util.tsid_fields.TSIDField()").cls == "TSIDField"

    def test_a_value_that_is_not_literal_reads_as_opaque(self):
        # `is not None` passaria para `""`, `0` e qualquer outro valor de
        # fracasso — inclusive um que quebrasse a igualdade consigo mesmo e
        # fizesse "continua não literal" virar "mudou".
        state = field_state("models.CharField(max_length=LIMIT)")
        assert state.arguments["max_length"] is _OPAQUE
        assert _OPAQUE == _OPAQUE

    def test_the_first_positional_of_a_foreign_key_is_the_target(self):
        state = field_state("models.ForeignKey('subjects.Chart', models.CASCADE)")
        assert state.arguments["to"] == "subjects.chart"
