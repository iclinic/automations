"""O dispatch: qual parser lê qual arquivo, e o que acontece quando nenhum lê.

Errar o dispatch não estoura. Entregue a fonte de um stack ao parser do outro e
os dois devolvem lista vazia, que a jusante se lê como "nada a reportar" — o
verde silencioso que esta entrega existe para matar. Por isso a maior parte
deste módulo não é sobre acertar o parser: é sobre provar que **nenhum caminho
devolve silêncio** quando o classificador não sabe o que fazer.
"""

import ast
import pathlib

import pytest

import detect
from detect import STACKS, classify_file, read_source, stack_for
from detect.severity import MANUAL, Severity, max_severity

ACTION_DIR = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# As fontes mínimas de cada stack
# ---------------------------------------------------------------------------

DJANGO = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.RemoveField(model_name="order", name="qty"),
    ]
"""

ALEMBIC = """\
from alembic import op

revision = "abc123"
down_revision = "def456"


def upgrade():
    op.drop_column("orders", "qty")
"""

SQL = "ALTER TABLE orders DROP COLUMN qty;"

TYPEORM = """\
export class DropQty1700000000000 implements MigrationInterface {
  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`ALTER TABLE orders DROP COLUMN qty`);
  }
}
"""

SOURCES = {"django": DJANGO, "alembic": ALEMBIC, "sql": SQL, "typeorm": TYPEORM}

SUFFIX = {"django": ".py", "alembic": ".py", "sql": ".sql", "typeorm": ".ts"}


def write(tmp_path, name: str, source: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# A tabela
# ---------------------------------------------------------------------------


def _by_suffix() -> dict[str, list]:
    grouped: dict[str, list] = {}
    for stack in STACKS:
        grouped.setdefault(stack.suffix, []).append(stack)
    return grouped


class TestDispatchTable:
    def test_every_stack_is_reachable(self, tmp_path):
        # Toda linha da tabela tem que ser alcançável por algum arquivo. Uma
        # linha inalcançável é um parser que nunca roda.
        reached = {
            stack_for(write(tmp_path, f"m{SUFFIX[name]}", source), source)
            for name, source in SOURCES.items()
        }
        assert reached == {stack.name for stack in STACKS}

    def test_the_names_are_unique(self):
        names = [stack.name for stack in STACKS]
        assert len(names) == len(set(names))

    def test_a_suffix_shared_by_two_rows_forces_both_to_have_a_marker(self):
        # É o invariante que faz `.py` funcionar. Uma linha nova sem marcador
        # dividindo `.py` com outra tornaria a escolha dependente da ordem da
        # tabela, e a ordem da tabela não é um critério de classificação.
        for suffix, stacks in _by_suffix().items():
            if len(stacks) > 1:
                assert all(s.marker for s in stacks), suffix

    def test_some_suffix_really_is_shared(self):
        # Pareado: sem isto o teste acima passa com o corpo do laço nunca
        # executado — bastaria alguém remover a linha do Alembic.
        assert any(len(stacks) > 1 for stacks in _by_suffix().values())

    def test_a_row_without_a_marker_is_alone_on_its_suffix(self):
        # A contrapositiva, que é a que tem dente: um parser sem marcador só
        # pode existir onde a extensão já decide sozinha.
        for suffix, stacks in _by_suffix().items():
            if any(s.marker is None for s in stacks):
                assert len(stacks) == 1, suffix

    def test_only_sql_has_no_marker(self):
        # `.sql` dispensa marcador porque `sql.py` classifica todo statement e
        # devolve `unknown` para o verbo que não reconhece — um `.sql` de
        # dialeto estranho não sai em silêncio, sai como `unknown` do parser.
        # Os outros três precisam, e este teste é o que obriga uma linha nova a
        # justificar a ausência em vez de herdá-la.
        assert {s.name for s in STACKS if s.marker is None} == {"sql"}

    def test_every_suffix_in_the_default_migration_paths_has_a_stack(self):
        # Amarra a tabela ao `action.yml`: um glob no default de
        # `migration_paths` que não tenha parser coleta arquivo para ninguém
        # ler. Lido do YAML, e não de uma lista escrita à mão aqui.
        action = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
        line = next(
            l for l in action.splitlines()
            if l.strip().startswith("default:") and "*.sql" in l
        )
        globs = line.split("'")[1].split(",")
        assert len(globs) >= 6, globs
        suffixes = {pathlib.PurePath(g.strip()).suffix.lower() for g in globs}
        assert suffixes <= {stack.suffix for stack in STACKS}, suffixes


# ---------------------------------------------------------------------------
# Django ou Alembic
# ---------------------------------------------------------------------------


class TestDjangoVersusAlembic:
    @pytest.mark.parametrize("name", ("django", "alembic"))
    def test_the_content_decides(self, tmp_path, name):
        source = SOURCES[name]
        assert stack_for(write(tmp_path, "0001_x.py", source), source) == name

    @pytest.mark.parametrize(
        "directory,name",
        (
            # O caminho que "parece" do outro stack. O sinal é o conteúdo, então
            # o diretório não muda a escolha — e é isso que protege um projeto
            # Alembic com `script_location = migrations`, que casa
            # `**/migrations/*.py` e cairia no parser do Django.
            ("alembic/versions", "django"),
            ("migrations", "alembic"),
            ("migrations", "django"),
            ("alembic/versions", "alembic"),
        ),
    )
    def test_the_directory_does_not_override_the_content(self, tmp_path, directory, name):
        folder = tmp_path / directory
        folder.mkdir(parents=True)
        source = SOURCES[name]
        path = folder / "0001_x.py"
        path.write_text(source, encoding="utf-8")
        assert stack_for(path, source) == name

    @pytest.mark.parametrize("name", ("django", "alembic"))
    def test_the_parser_it_chose_actually_has_something_to_say(self, tmp_path, name):
        # O invariante que fecha o buraco: escolher o parser errado não estoura,
        # devolve lista vazia. Este teste prova que o escolhido produz finding e
        # — pareado, na linha de baixo — que o outro não produziria nada, ou
        # seja, que a escolha importa e não é sorte.
        source = SOURCES[name]
        path = write(tmp_path, "0001_x.py", source)
        chosen = classify_file(path)
        assert chosen, name

        other = "alembic" if name == "django" else "django"
        other_parser = next(s for s in STACKS if s.name == other)
        assert other_parser.classify(source, path) == []

    def test_a_file_with_both_markers_is_unknown_and_not_a_coin_flip(self, tmp_path):
        source = DJANGO + "\n\ndef upgrade():\n    pass\n"
        path = write(tmp_path, "0001_both.py", source)
        assert stack_for(path, source) is None
        findings = classify_file(path)
        assert [f.severity for f in findings] == [Severity.UNKNOWN]
        assert "Django" in findings[0].reason and "Alembic" in findings[0].reason

    def test_a_migration_class_inside_an_if_still_reaches_the_django_parser(self, tmp_path):
        # O marcador varre a árvore inteira de propósito. `django.py` tem um
        # `unknown` declarado para "a classe está dentro de um `if` e eu não sei
        # qual vale"; um marcador que olhasse só `tree.body` não veria essa
        # classe, o arquivo não casaria stack nenhum e sairia `none` — trocando
        # um `unknown` honesto por um verde.
        source = (
            "from django.db import migrations\n"
            "if True:\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = []\n"
        )
        path = write(tmp_path, "0001_cond.py", source)
        assert stack_for(path, source) == "django"
        assert [f.severity for f in classify_file(path)] == [Severity.UNKNOWN]

    def test_an_upgrade_inside_a_try_still_reaches_the_alembic_parser(self, tmp_path):
        source = (
            "from alembic import op\n"
            "try:\n"
            "    def upgrade():\n"
            "        op.drop_column('orders', 'qty')\n"
            "except ImportError:\n"
            "    pass\n"
        )
        path = write(tmp_path, "0001_cond.py", source)
        assert stack_for(path, source) == "alembic"
        assert [f.severity for f in classify_file(path)] == [Severity.UNKNOWN]

    def test_a_python_file_that_is_neither_is_not_a_migration(self, tmp_path):
        # `__init__.py` de um pacote `migrations/`, módulo auxiliar. Django e
        # Alembic também o ignorariam: zero findings, e não um `unknown`.
        for source in ("", "# vazio\n", "HELPERS = {'a': 1}\n"):
            path = write(tmp_path, "__init__.py", source)
            assert stack_for(path, source) is None
            assert classify_file(path) == []


# ---------------------------------------------------------------------------
# Nada some em silêncio
# ---------------------------------------------------------------------------


class TestNothingIsSilentlyDropped:
    @pytest.mark.parametrize("suffix", (".rb", ".txt", ".go", ""))
    def test_a_suffix_with_no_parser_is_unknown(self, tmp_path, suffix):
        path = write(tmp_path, f"0001_migrate{suffix}", "qualquer coisa")
        findings = classify_file(path)
        assert [f.severity for f in findings] == [Severity.UNKNOWN]
        assert findings[0].reason.endswith(MANUAL)

    def test_the_unknown_says_which_extension_it_could_not_read(self, tmp_path):
        path = write(tmp_path, "0001_migrate.rb", "puts 'oi'")
        # Fragmento renderizado: dois `in` soltos não distinguiriam a extensão
        # certa de qualquer outra frase que contivesse `.rb`.
        assert "Extensão `.rb` não tem classificador" in classify_file(path)[0].reason

    def test_python_that_does_not_parse_is_unknown(self, tmp_path):
        path = write(tmp_path, "0001_broken.py", "class Migration(  :::")
        findings = classify_file(path)
        assert [f.severity for f in findings] == [Severity.UNKNOWN]
        assert "não é Python válido" in findings[0].reason

    @pytest.mark.parametrize(
        "name,source",
        (
            ("0001_x.rb", "puts 'oi'"),
            ("0001_broken.py", "class Migration(  :::"),
            ("0001_both.py", DJANGO + "\n\ndef upgrade():\n    pass\n"),
        ),
    )
    def test_no_dispatch_failure_returns_an_empty_list(self, tmp_path, name, source):
        # O invariante do módulo. Lista vazia é "li e não achei nada"; falha de
        # dispatch é outra coisa e não pode se disfarçar dela.
        assert classify_file(write(tmp_path, name, source)) != []

    # -- migração de um framework que este pacote não conhece --------------
    #
    # Casou `migration_paths`, logo alguém a considera migração; nenhum marcador
    # bate, logo nenhum parser sabe lê-la. Devolver lista vazia aqui fazia um PR
    # que dropa coluna terminar verde com `has_db_change=false`.

    UNKNOWN_DIALECTS = {
        "yoyo": (
            "0001_drop.py",
            'from yoyo import step\n'
            'steps = [step("ALTER TABLE subjects DROP COLUMN cpf")]\n',
        ),
        "south": (
            "0001_drop.py",
            "from south.db import db\n"
            "class Migration:\n"
            "    def forwards(self, orm):\n"
            "        db.delete_column"
            "('subjects', 'cpf')\n",
        ),
        "knex": (
            "0001_drop.ts",
            "export async function up(knex) {\n"
            "  await knex.raw('ALTER TABLE subjects DROP COLUMN cpf');\n"
            "}\n",
        ),
        "migration_class_nested_in_a_class": (
            "0001_drop.py",
            "from django.db import migrations\n"
            "class Wrapper:\n"
            "    class Migration(migrations.Migration):\n"
            "        operations = [\n"
            "            migrations.RemoveField(model_name='subject', name='cpf'),\n"
            "        ]\n",
        ),
    }

    @pytest.mark.parametrize("dialect", sorted(UNKNOWN_DIALECTS))
    def test_an_unknown_migration_dialect_is_unknown_and_not_silence(
        self, tmp_path, dialect
    ):
        name, source = self.UNKNOWN_DIALECTS[dialect]
        findings = classify_file(write(tmp_path, name, source))
        assert [f.severity for f in findings] == [Severity.UNKNOWN], dialect

    @pytest.mark.parametrize("dialect", sorted(UNKNOWN_DIALECTS))
    def test_an_unknown_dialect_never_ends_up_reported_as_no_change(
        self, tmp_path, dialect
    ):
        # O fecho de verdade: pelo caminho de produção, e em severidade agregada
        # — que é o que vira `has_db_change` e a cor do alerta.
        from classify import analyze

        name, source = self.UNKNOWN_DIALECTS[dialect]
        result = analyze([str(write(tmp_path, name, source))])
        assert result["highest_severity"] == "unknown", dialect
        assert result["has_db_change"] is True, dialect

    def test_the_migration_class_nested_in_a_class_is_not_sent_to_django(self, tmp_path):
        # O marcador do Django é a união exata do que o portão de confiança de
        # `_reading.py` enxerga. `ast.walk` sozinho via esta classe e mandava o
        # arquivo para o parser do Django, que não a vê e devolve lista vazia —
        # um `RemoveField` reportado como "nada a reportar".
        name, source = self.UNKNOWN_DIALECTS["migration_class_nested_in_a_class"]
        path = write(tmp_path, name, source)
        assert stack_for(path, source) is None
        # Pareado: o parser do Django realmente não teria o que dizer.
        django_row = next(s for s in STACKS if s.name == "django")
        assert django_row.classify(source, path) == []

    @pytest.mark.parametrize(
        "name,source",
        (
            ("__init__.py", ""),
            ("__init__.py", "# vazio\n"),
            ("helpers.py", "HELPERS = {'a': 1}\n"),
            ("index.ts", "export * from './conference';\n"),
        ),
    )
    def test_a_file_that_declares_no_operation_is_not_a_migration(
        self, tmp_path, name, source
    ):
        # O outro lado do mesmo corte, e o que impede o guarda acima de virar
        # "todo arquivo que eu não classifico é unknown": um arquivo sem
        # chamada nenhuma não declara operação nenhuma. Todos os 53
        # `__init__.py` sob `migrations/` do consumidor Django são vazios.
        assert classify_file(write(tmp_path, name, source)) == []

    @pytest.mark.parametrize(
        "source",
        (
            "-- ALTER TABLE orders ADD COLUMN note VARCHAR(50) NULL;\n",
            "/* ALTER TABLE orders ADD COLUMN note VARCHAR(50); */\n",
            "-- nada aqui\n",
            "",
        ),
    )
    def test_a_commented_out_sql_migration_is_not_a_false_unknown(self, tmp_path, source):
        # Comentar a migração inteira é artefato de review corriqueiro. O portão
        # do parser mudo não vale para `.sql`: `_declares_operations` não sabe
        # ler SQL — tenta `ast.parse` num texto que não é Python e cai no teste
        # de parêntese —, então `VARCHAR(50)` dentro de um comentário virava
        # `unknown` vermelho.
        assert classify_file(write(tmp_path, "0001_drop.sql", source)) == []

    def test_sql_that_the_parser_cannot_read_is_still_unknown(self, tmp_path):
        # Pareado, e é o que justifica dispensar o portão em `.sql`: quem
        # levanta a mão ali é o próprio `sql.py`, statement a statement. Sem
        # isto, o teste acima seria satisfeito por um `.sql` que nunca reporta
        # nada.
        findings = classify_file(write(tmp_path, "0002_odd.sql", "FROBNICATE orders(qty);"))
        assert [f.severity for f in findings] == [Severity.UNKNOWN]

    def test_the_silent_parser_gate_still_holds_where_there_is_a_marker(self, tmp_path):
        # A dispensa é só para `.sql`. Um `.py` cujo parser volta vazio sobre
        # arquivo que declara operações continua sendo `unknown`.
        source = (
            "from south.db import db\n"
            "class Migration:\n"
            "    def forwards(self, orm):\n"
            "        db.delete_column('subjects', 'cpf')\n"
        )
        findings = classify_file(write(tmp_path, "0003_south.py", source))
        assert [f.severity for f in findings] == [Severity.UNKNOWN]

    def test_the_dispatch_findings_carry_no_file_content(self, tmp_path):
        # Mesma disciplina dos parsers: o `operation` de um finding do dispatch
        # não sai do conteúdo do arquivo — o dispatch não chegou a ler operação
        # nenhuma.
        source = "INSERT INTO subjects (cpf) VALUES ('12345678900');"
        for name in ("0001_x.rb", "0001_x.py"):
            findings = classify_file(write(tmp_path, name, source))
            assert findings, name  # senão o laço abaixo não afirma nada
            for finding in findings:
                assert "12345678900" not in finding.operation
                assert "12345678900" not in finding.reason
                assert finding.operation == detect.DISPATCH


# ---------------------------------------------------------------------------
# read_source
# ---------------------------------------------------------------------------


class TestReadSource:
    def test_reads_the_whole_file(self, tmp_path):
        # O `MAX_FILE_BYTES = 6000` que existia no `classify.py` era orçamento
        # de prompt. 22 das 591 migrações do consumidor Django passam disso, e um
        # arquivo cortado ou não é Python válido — virando um `unknown` falso —
        # ou, pior, ainda parseia sem o `DROP COLUMN` que estava depois do
        # corte.
        source = "# " + "x" * 20000 + "\n"
        path = write(tmp_path, "grande.py", source)
        assert read_source(path) == source

    def test_a_long_migration_is_classified_past_the_old_cut(self, tmp_path):
        # O caso concreto: o `DROP COLUMN` mora depois do byte 6000.
        padding = "-- " + "x" * 7000 + "\n"
        path = write(tmp_path, "0001_drop.sql", padding + SQL)
        findings = classify_file(path)
        assert max_severity(f.severity for f in findings) is Severity.BREAKING

    def test_a_read_error_is_not_swallowed(self, tmp_path):
        with pytest.raises(OSError):
            read_source(tmp_path / "sumiu.sql")

    def test_undecodable_bytes_do_not_bring_the_reader_down(self, tmp_path):
        path = tmp_path / "0001_x.sql"
        path.write_bytes(b"ALTER TABLE orders DROP COLUMN qty; -- \xff\xfe")
        assert "DROP COLUMN" in read_source(path)


# ---------------------------------------------------------------------------
# O estado anterior do Django, ligado
# ---------------------------------------------------------------------------


def _app(tmp_path, *files: tuple[str, str]) -> pathlib.Path:
    """Monta um app Django com `migrations/` e devolve o diretório."""
    folder = tmp_path / "app" / "subjects" / "migrations"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("")
    for name, source in files:
        (folder / name).write_text(source, encoding="utf-8")
    return folder


INITIAL = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Subject",
            fields=[("name", models.CharField(max_length=200))],
        ),
    ]
"""

SHORTEN = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subjects", "0001_initial")]
    operations = [
        migrations.AlterField(
            model_name="subject",
            name="name",
            field=models.CharField(max_length=50),
        ),
    ]
"""


WIDEN = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subjects", "0002_shorten")]
    operations = [
        migrations.AlterField(
            model_name="subject",
            name="name",
            field=models.CharField(max_length=500),
        ),
    ]
"""


class TestPriorStateIsWired:
    def test_alter_field_is_classified_with_the_prior_state(self, tmp_path):
        # O ganho todo desta subtask. Sem estado anterior, `AlterField` carrega
        # só o estado final e sai `unknown`; com ele, encurtar de 200 para 50 é
        # uma quebra que o classificador sabe nomear.
        folder = _app(tmp_path, ("0001_initial.py", INITIAL), ("0002_shorten.py", SHORTEN))
        findings = classify_file(folder / "0002_shorten.py")
        assert max_severity(f.severity for f in findings) is Severity.BREAKING

    def test_without_the_ancestor_on_disk_it_stays_unknown(self, tmp_path):
        # Mutante pareado. Sem ele o teste acima não distingue "o estado
        # anterior foi lido" de "o parser adivinhou certo": o mesmo arquivo, sem
        # o ancestral ao lado, tem que voltar a ser `unknown`.
        folder = _app(tmp_path, ("0002_shorten.py", SHORTEN))
        findings = classify_file(folder / "0002_shorten.py")
        assert max_severity(f.severity for f in findings) is Severity.UNKNOWN

    def test_two_migrations_of_the_same_app_each_get_their_own_app_state(self, tmp_path):
        # `AppState` é consumido por exatamente uma chamada de
        # `classify_migration` e `claim()` levanta `RuntimeError` no reuso. Se o
        # dispatch memoizasse o objeto, o segundo arquivo do mesmo app
        # estouraria — e um PR que mexe em duas migrações do mesmo app é o caso
        # comum, não o raro.
        #
        # Dois arquivos **diferentes** do mesmo app, e não o mesmo duas vezes:
        # com o mesmo arquivo o teste passava mesmo com um cache por caminho,
        # que é justamente o que não pode existir.
        folder = _app(
            tmp_path,
            ("0001_initial.py", INITIAL),
            ("0002_shorten.py", SHORTEN),
            ("0003_widen.py", WIDEN),
        )
        shorten = classify_file(folder / "0002_shorten.py")
        widen = classify_file(folder / "0003_widen.py")

        assert max_severity(f.severity for f in shorten) is Severity.BREAKING
        # O estado anterior do terceiro arquivo é o do segundo, não o do
        # primeiro: alargar de 50 para 500 não é quebra.
        assert max_severity(f.severity for f in widen) is not Severity.BREAKING

    def test_classifying_together_matches_classifying_one_by_one(self, tmp_path):
        # O que o cache ausente compra: nenhuma dependência de ordem nem de
        # quantos arquivos vieram no mesmo PR.
        from classify import analyze

        folder = _app(
            tmp_path,
            ("0001_initial.py", INITIAL),
            ("0002_shorten.py", SHORTEN),
            ("0003_widen.py", WIDEN),
        )
        paths = [str(folder / n) for n in ("0002_shorten.py", "0003_widen.py")]
        together = analyze(paths)["items"]
        apart = [item for p in paths for item in analyze([p])["items"]]
        assert together == apart

    def test_the_django_row_is_the_only_one_that_reads_the_disk(self):
        # `prior_state` custa ~10–25 ms por arquivo e só faz sentido para o
        # Django. Pela AST, para não depender de o nome aparecer num comentário.
        source = (ACTION_DIR / "detect" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "prior_state"
        }
        assert callers == {"_classify_django"}


# ---------------------------------------------------------------------------
# O pacote é biblioteca padrão
# ---------------------------------------------------------------------------


class TestDetectStaysStandardLibrary:
    def test_no_third_party_import_under_detect(self):
        allowed = {
            "ast", "dataclasses", "pathlib", "typing", "enum", "re", "sys",
            "collections", "heapq", "functools", "itertools", "__future__",
        }
        offenders = []
        for path in sorted((ACTION_DIR / "detect").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:  # import relativo, dentro do pacote
                        continue
                    names = [node.module.split(".")[0]] if node.module else []
                else:
                    continue
                offenders += [f"{path.name}:{n}" for n in names if n not in allowed]
        assert offenders == []
