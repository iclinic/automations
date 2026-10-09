"""O corpus versionado, e o portão que impede a taxa de determinismo de cair.

Os outros módulos de teste alimentam cada parser com fontes escritas à mão. Este
alimenta o **dispatch** com arquivos em disco, do jeito que a action os recebe:
`detect.classify_file(caminho)`, ida e volta completa, incluindo o
`history.prior_state` que só existe quando a migração tem ancestrais no
diretório ao lado.

Por que o corpus é sintético
---------------------------

O aceite da QQ-2161 pede que o teste rode no CI do `automations` sem checkout dos
repositórios consumidores, e a leitura óbvia disso era vendorizar as 638
migrações dos três consumidores.

`iclinic/automations` é um repositório **público**. Vendorizar aquele corpus
publicaria o schema de produção inteiro — toda tabela, toda coluna, todo índice
dos três serviços —, além de nomes de parceiros comerciais, SKUs de produto e a
lista de fornecedores farmacêuticos que três migrações de dados carregam. Não é
o vazamento de dado de paciente que `tests/test_pii.py` guarda, mas é o mesmo
tipo de erro e é irreversível depois do push.

Então o corpus daqui é escrito, não copiado. Cada arquivo existe por um motivo
declarado: uma linha de uma tabela de regras, um caso de resíduo, um dos
arquivos citados na ADR. Os quatro casos da ADR reproduzem, statement por
statement e frase por frase, o que o classificador devolve para os arquivos
reais — foram conferidos contra o clone do consumidor Django na escrita, e
`TestTheRealCorpus` reconfere quando o checkout está presente.

Os identificadores das fixtures também são sintéticos: app, tabela, coluna e
nome de arquivo. O que amarra cada fixture da ADR ao arquivo real que ela
reproduz é `tests/fixtures/.identifier-map.json`, que o `.gitignore` mantém
fora de qualquer commit — ver `IDENTIFIER_MAP` mais abaixo.

O que o portão prende
---------------------

Uma "taxa de determinismo" sobre um corpus que eu mesmo desenhei não é uma
medida: eu escolho quantos `unknown` ele tem. O que substitui a taxa aqui é uma
**partição exata** — `RESIDUE` lista, por nome, todo arquivo que pode voltar
`unknown`, e o teste falha nas duas direções: um arquivo fora da lista que
passe a devolver `unknown` (o classificador regrediu) e um arquivo da lista que
deixe de devolver (o `unknown` honesto virou palpite). Isso é mais forte que um
piso percentual, e não depende de eu ter desenhado a distribuição certa.

A taxa real continua registrada: `REAL_CORPUS` guarda a medição dos três
consumidores, e `TestTheRealCorpus` a reconfere quando os checkouts existem ao
lado. No CI ele pula; o que roda lá é a partição exata e o manifesto.
"""

import json
import re
from pathlib import Path

import pytest

import detect
from detect import alembic, django, doctrine, history, sql, typeorm
from detect.severity import Severity

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
MANIFEST = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))


# O que `rglob` traz e não é fixture. `__pycache__` aparece dentro do corpus
# assim que qualquer coisa importa ou compila os arquivos daqui — um
# `compileall`, uma IDE, um import perdido —, e sem este filtro os `.pyc`
# entravam no corpus: cinco testes vermelhos, dois deles do portão de
# determinismo, mais um `UnicodeDecodeError`. Falha que parece regressão do
# classificador e não é.
_NOT_A_FIXTURE = ("manifest.json",)


def corpus_files() -> list[Path]:
    return sorted(
        p
        for p in CORPUS.rglob("*")
        if p.is_file()
        and p.name not in _NOT_A_FIXTURE
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
    )


def rel(path: Path) -> str:
    return path.relative_to(CORPUS).as_posix()


# Todo arquivo de texto **versionado** do pacote. `tests/test_identifier_leakage`
# varre isto contra o vocabulário dos clones, então a exclusão do mapa é
# obrigatória: ele carrega os identificadores reais de propósito, e incluí-lo
# faria a varredura acusar o corpus inteiro.
_NOT_OURS = (".venv", "__pycache__", ".pytest_cache", ".git", "node_modules")
PACKAGE = Path(__file__).resolve().parents[1]


def package_files() -> list[tuple[Path, str]]:
    out = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file() or path == IDENTIFIER_MAP_PATH:
            continue
        if any(part in _NOT_OURS or part.startswith(".") for part in path.parts):
            continue
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return out


def findings_of(name: str) -> list:
    return detect.classify_file(CORPUS / name)


def shape(findings) -> list[dict]:
    return [
        {"severity": f.severity.value, "operation": f.operation, "reason": f.reason}
        for f in findings
    ]


# ---------------------------------------------------------------------------
# A partição
# ---------------------------------------------------------------------------

# Todo arquivo do corpus que pode devolver `unknown`, e por quê. A lista é
# escrita à mão: é ela, e não uma contagem, que o portão de regressão compara.
RESIDUE: dict[str, str] = {
    # --- os quatro casos que a QQ-2161 nomeia -----------------------------
    "django/residue/migrations/0001_alter_field_without_prior_state.py":
        "AlterField sem estado anterior — o arquivo carrega só a definição nova",
    "django/residue/migrations/0002_alter_unique_together_opaque.py":
        "AlterUniqueTogether com valor que não é literal",
    "django/residue/migrations/0003_run_sql_dynamic.py":
        "RunSQL montado com f-string — o SQL que roda não está escrito",
    "django/residue/migrations/0004_separate_database_and_state_non_literal.py":
        "ramo de SeparateDatabaseAndState montado fora da chamada",
    "sql/0006_alter_type_rename.sql":
        "ALTER TYPE que não é ADD VALUE",
    # --- o mesmo resíduo nos outros stacks --------------------------------
    "alembic/versions/0008_execute_dynamic_sql.py":
        "op.execute com f-string",
    "alembic/versions/0009_alter_column_undecidable.py":
        "op.alter_column que não escreve nem tipo nem nulidade",
    "typeorm/migrations/1700000000004-EnumRewrite.ts":
        "ALTER TYPE ... RENAME no meio da reescrita de enum",
    "typeorm/migrations/1700000000006-DynamicSql.ts":
        "template com interpolação",
    "typeorm/migrations/1700000000007-UnreadSql.ts":
        "SQL vindo de arquivo que o classificador não abre",
    "typeorm/migrations/1700000000009-AmbiguousUp.ts":
        "duas definições de up()",
    "typeorm/migrations/1700000000010-UnterminatedUp.ts":
        "corpo de up() que não fecha",
    "sql/0008_unrecognised_verb.sql":
        "verbo fora da tabela de statements",
    "sql/0009_alter_column_action_unrecognised.sql":
        "ação de ALTER COLUMN fora da tabela",
    # --- o resíduo do Doctrine, nas duas bases ----------------------------
    #
    # A migração de dados com parâmetros nomeados, que é metade do corpus real
    # deste consumidor, não está aqui: `INSERT`, `UPDATE` e `DELETE` têm linha
    # `controlled` na tabela de statements do `sql.py`. O que sobra é SQL que o
    # arquivo não escreve por inteiro.
    "doctrine/Migrations/mysql/Version20250101120400.php":
        "heredoc interpolado — o SQL que roda pode não ser o que está escrito",
    "doctrine/Migrations/mysql/Version20250101120500.php":
        "SQL montado por concatenação",
    "doctrine/Migrations/pgsql/Version20250201120600.php":
        "duas definições de up(), em duas classes do mesmo arquivo",
    "doctrine/Migrations/pgsql/Version20250201120700.php":
        "corpo de up() que não fecha",
    "doctrine/Migrations/pgsql/Version20250201120800.php":
        "migração escrita com o schema builder — chamadas fora de addSql()",
    # --- os `unknown` que o próprio dispatch produz ------------------------
    "dispatch/activerecord_style_migration.rb":
        "extensão sem classificador",
    "dispatch/ambiguous_django_and_alembic.py":
        "declara os marcadores de Django e Alembic ao mesmo tempo",
    "dispatch/broken_syntax_migration.py":
        "não é Python válido",
    "dispatch/south_style_migration.py":
        "parser certo, zero findings, e o arquivo declara operações",
    "dispatch/yoyo_style_migration.py":
        "dialeto de migração que o pacote não conhece",
    "dispatch/doctrine_generator.php":
        "o gerador que escreve migrações, e que traz o marcador do Doctrine "
        "escrito por extenso dentro de um heredoc",
}


# A medição dos três repositórios reais, feita pelo mesmo `classify_file` que
# este módulo chama. É o número que o portão existe para proteger; o corpus
# sintético não o reproduz porque foi desenhado para concentrar os casos duros.
# O nome do diretório de cada consumidor não está aqui: ele é identificador de
# produção e mora no mapa fora do repositório. O que fica versionado é a
# medição, que não identifica serviço nenhum.
REAL_CORPUS = {
    "django": {
        "glob": "django/app/*/migrations/*.py",
        "files": 591,
        "unknown_files": 9,
    },
    "alembic": {
        "glob": "**/versions/*.py",
        "files": 13,
        "unknown_files": 0,
    },
    "typeorm": {
        "glob": "migrations/*.ts",
        "files": 34,
        # Nenhum dos 34 arquivos sai inteiro como `unknown`: os dois contados
        # aqui trazem seis statements classificados cada e um `ALTER TYPE ...
        # RENAME` que o classificador não decide. Ver `REAL_TYPEORM_STATEMENTS`.
        "unknown_files": 2,
    },
    "doctrine": {
        # O `*` do meio é o diretório do banco, e é ele que faz as **duas** bases
        # entrarem na medição — a do financeiro e a do resto do sistema. O `2`
        # depois de `Version` deixa fora o `VersionHelper.php` do runner, que não
        # é migração; é o mesmo recorte do glob de `migration_paths`.
        "glob": "Migrations/*/Version2*.php",
        "files": 19,
        # Eram 10 de 18 enquanto `INSERT`, `UPDATE` e `DELETE` ficavam fora da
        # tabela de statements do `sql.py`: metade das migrações deste consumidor
        # é migração de dados em SQL cru. Com o DML valendo `controlled`, como o
        # `RunPython` de dados do Django, nenhum arquivo sobra sem decisão.
        "unknown_files": 0,
    },
}

# `typeorm.py` devolve um finding por statement, então a medição do repositório
# de vídeo tem um segundo número que os outros dois não têm.
REAL_TYPEORM_STATEMENTS = {"total": 133, "unknown": 2}

# `doctrine.py` também devolve um finding por statement, e a medição dele tem um
# terceiro número que nenhum outro stack tem: a quebra por **banco**. O cartão da
# SHS-606 existe por causa dela — um alerta que cobrisse só o PostgreSQL deixaria
# a base do financeiro sem proteção —, então a contagem por diretório de banco é
# afirmada e não deduzida.
REAL_DOCTRINE = {
    "statements": {"total": 38, "unknown": 0},
    # Os verbos de DML que o corpus real usa, todos com linha própria na tabela.
    # Igualdade, e não contenção: um verbo de dados novo no consumidor muda o
    # conjunto e pede que alguém confira a severidade dele.
    "data_operations": {"INSERT", "UPDATE", "DELETE"},
    "databases": {"mysql": 7, "pgsql": 12},
}


# ---------------------------------------------------------------------------
# O manifesto
# ---------------------------------------------------------------------------


class TestTheManifestCoversTheCorpus:
    """O manifesto e o diretório têm que ter exatamente os mesmos arquivos.

    É o que impede uma fixture nova de entrar sem expectativa — e sem
    expectativa ela não prova nada — e uma fixture apagada de deixar uma
    expectativa órfã que ninguém mais exercita.
    """

    def test_every_file_on_disk_has_a_manifest_entry(self):
        assert {rel(p) for p in corpus_files()} == set(MANIFEST)


@pytest.mark.parametrize("name", sorted(MANIFEST), ids=sorted(MANIFEST))
def test_the_fixture_classifies_exactly_as_the_manifest_says(name):
    """Severidade, operação e frase renderizada, na ordem, para cada arquivo.

    O manifesto guarda a razão inteira e não um fragmento: é a frase renderizada
    que distingue "a regra certa disparou" de "alguma regra disparou".
    """
    assert shape(findings_of(name)) == MANIFEST[name], name


# ---------------------------------------------------------------------------
# O portão de regressão
# ---------------------------------------------------------------------------


class TestTheDeterminismGate:
    """A partição exata entre o que o classificador resolve e o que ele não.

    Falha nas duas direções, e as duas importam: um `unknown` novo é regressão
    de cobertura, e um `unknown` que sumiu é um palpite ocupando o lugar de uma
    resposta honesta.
    """

    @staticmethod
    def _unknown_files() -> set[str]:
        return {
            rel(p)
            for p in corpus_files()
            if any(f.severity is Severity.UNKNOWN for f in detect.classify_file(p))
        }

    def test_no_file_outside_the_residue_list_comes_back_unknown(self):
        assert self._unknown_files() - set(RESIDUE) == set()

    def test_every_file_in_the_residue_list_still_comes_back_unknown(self):
        assert set(RESIDUE) - self._unknown_files() == set()

    def test_the_residue_list_names_only_files_that_exist(self):
        assert set(RESIDUE) <= {rel(p) for p in corpus_files()}

    def test_the_rest_of_the_corpus_is_fully_deterministic(self):
        """A taxa que o aceite pede, sobre a parte do corpus que não é resíduo.

        Sobre um corpus desenhado a taxa só tem sentido com o denominador
        declarado, e é isso que a lista `RESIDUE` faz: fora dela a taxa é 1.0,
        não um piso.
        """
        decided = [p for p in corpus_files() if rel(p) not in RESIDUE]
        undecided = [
            rel(p)
            for p in decided
            if any(f.severity is Severity.UNKNOWN for f in detect.classify_file(p))
        ]
        assert undecided == []


# ---------------------------------------------------------------------------
# Os casos de resíduo que o cartão nomeia
# ---------------------------------------------------------------------------


class TestTheNamedResidueCases:
    """Os quatro casos citados na QQ-2161, um a um, com a frase que devolvem.

    Eles não são falha do classificador: são a resposta honesta quando a fonte
    não carrega o que decidiria a severidade. O teste existe para que continuem
    devolvendo `unknown` — e para que a frase continue dizendo por quê, porque
    um `unknown` sem motivo não serve para quem lê o Slack.
    """

    @pytest.mark.parametrize(
        "name, fragment",
        [
            (
                "django/residue/migrations/0002_alter_unique_together_opaque.py",
                "Meta `unique_together` de `orphan` definida por um valor que o "
                "classificador não consegue ler",
            ),
            (
                "django/residue/migrations/0001_alter_field_without_prior_state.py",
                "Campo `code` de `orphan` alterado — o classificador não conhece a "
                "definição anterior",
            ),
            (
                "django/residue/migrations/0003_run_sql_dynamic.py",
                "SQL montado dinamicamente — o classificador só lê SQL literal",
            ),
            (
                "sql/0006_alter_type_rename.sql",
                "ALTER TYPE em `ledger_kind` que não é ADD VALUE",
            ),
        ],
    )
    def test_the_case_returns_unknown_and_says_why(self, name, fragment):
        findings = findings_of(name)
        assert len(findings) == 1, shape(findings)
        assert findings[0].severity is Severity.UNKNOWN
        assert fragment in findings[0].reason
        assert findings[0].reason.endswith("precisa de revisão manual.")

    def test_alter_field_becomes_decidable_once_the_prior_state_is_there(self):
        """O mesmo `AlterField` que sai `unknown` órfão sai `breaking` com ancestral.

        É o par que prova que o `unknown` do caso acima é falta de estado
        anterior, e não uma operação que o classificador não conhece.
        """
        orphan = findings_of(
            "django/residue/migrations/0001_alter_field_without_prior_state.py"
        )
        with_prior = findings_of(
            "django/catalog/migrations/0011_alter_field_shortens_column.py"
        )
        assert orphan[0].severity is Severity.UNKNOWN
        assert [f.operation for f in with_prior] == ["AlterField"]
        assert with_prior[0].severity is Severity.BREAKING
        assert with_prior[0].reason == (
            "Campo `sku` de `item` encurtou de `64` para `16` — "
            "valor mais longo que o novo limite impede a migração."
        )


# ---------------------------------------------------------------------------
# Os arquivos da ADR
# ---------------------------------------------------------------------------


ADR_FILES = (
    "django/ledger/migrations/0042_modify_supplier_external_id_to_non_null.py",
    "django/ledger/migrations/0043_modify_supplierservice_external_id_to_non_null.py",
)

# O outro par da Evidência 1 da ADR: a operação de banco é um `RunPython` que
# monta o `ALTER TABLE ... MODIFY ... NOT NULL` dentro do corpo, e o NOT NULL
# também está no `AlterField` do bloco de estado. As duas vias são
# independentes — o `sql.py` lendo o DDL do corpo e o `history.py` comparando
# com o estado que os ancestrais reconstroem — e é disso que a ADR fala em
# "nenhum dos oito depende de via única". Enquanto o `RunPython` foi severidade
# fixa, estes dois dependiam: `test_without_the_ancestors_...` mostra que sem
# os ancestrais o `AlterField` sozinho cai em `unknown`.
RUN_PYTHON_DDL = (
    "django/bookings/migrations/0034_modify_external_id_to_non_null.py",
    "django/subjects/migrations/0027_modify_external_id_to_non_null.py",
)


class TestTheAdrCases:
    """Os arquivos que a ADR cita, e o par que prova o `history.prior_state`."""

    @pytest.mark.parametrize("name", ADR_FILES)
    def test_the_adr_file_is_among_the_fixtures(self, name):
        assert (CORPUS / name).is_file()

    @pytest.mark.parametrize("name", ADR_FILES)
    def test_the_adr_file_is_breaking_by_two_independent_paths(self, name):
        """O `RunSQL` e o `AlterField` chegam ao mesmo veredito por caminhos
        diferentes — o primeiro lendo o DDL, o segundo comparando com o estado
        que os ancestrais reconstroem."""
        findings = findings_of(name)
        assert [f.operation for f in findings] == ["MODIFY ... NOT NULL", "AlterField"]
        assert all(f.severity is Severity.BREAKING for f in findings)
        assert "passou a NOT NULL" in findings[0].reason
        assert "passou a NOT NULL" in findings[1].reason

    @pytest.mark.parametrize("name", RUN_PYTHON_DDL)
    def test_the_ddl_inside_run_python_is_breaking_on_its_own(self, name):
        """O DDL do corpo do `RunPython` chega ao mesmo veredito do `AlterField`.

        Com `RunPython` valendo `controlled` fixo, o primeiro finding não
        dependia do arquivo: o `ALTER TABLE ... MODIFY ... NOT NULL` que está
        literalmente no corpo da função nunca era lido, e o `breaking` vinha só
        da reconstrução de estado. A ADR mede que 2% dos `AlterField` do corpus
        não têm estado anterior no grafo; quando isso coincidir com um
        `RunPython`, a via única não responde.
        """
        findings = findings_of(name)
        # `MODIFY` e não `MODIFY ... NOT NULL`: a cláusula de nulidade é o
        # argumento interpolado da f-string, e o classificador não executa nada
        # para saber qual das duas chamadas está em jogo. O que sobra na
        # string é a mudança de tipo, que já é `breaking` — e a razão diz isso,
        # em vez de afirmar um NOT NULL que ela não leu.
        assert [f.operation for f in findings] == ["MODIFY", "AlterField"]
        assert all(f.severity is Severity.BREAKING for f in findings)
        assert findings[0].reason.startswith("Tipo da coluna `external_id` de ")
        assert findings[1].reason.startswith("Campo `external_id` de ")
        assert "passou a NOT NULL" in findings[1].reason

    @pytest.mark.parametrize("name", RUN_PYTHON_DDL)
    def test_without_the_ancestors_the_same_file_would_be_unknown(self, name, tmp_path):
        """A mesma fonte, sozinha num diretório, perde o `breaking`.

        É o teste que distingue "a máquina de estado anterior está ligada" de
        "a fonte já dizia tudo". Sem ele, `test_the_not_null_is_reachable_...`
        passaria mesmo com `prior_state` desligado.
        """
        orphan = tmp_path / "migrations" / Path(name).name
        orphan.parent.mkdir(parents=True)
        orphan.write_text((CORPUS / name).read_text(encoding="utf-8"), encoding="utf-8")

        findings = detect.classify_file(orphan)
        alter = [f for f in findings if f.operation == "AlterField"]
        assert len(alter) == 1
        assert alter[0].severity is Severity.UNKNOWN
        assert "não conhece a definição anterior" in alter[0].reason


# ---------------------------------------------------------------------------
# Cobertura das tabelas de regra pelo corpus
# ---------------------------------------------------------------------------
#
# Cada parser já tem a sua invariante de tabela sobre fontes escritas à mão. O
# que estas classes acrescentam é a mesma exigência do outro lado: a linha tem
# que ser alcançada **através do dispatch, por um arquivo versionado**. Nas duas
# tabelas de `sql.py` isso não existia — `add_key` e `drop_key` tinham a
# severidade afirmada em lugar nenhum, e trocá-las por `SAFE` não quebrava a
# suíte.


def corpus_findings() -> list:
    out = []
    for p in corpus_files():
        out.extend(detect.classify_file(p))
    return out


def corpus_pairs() -> set[tuple[str, str]]:
    return {(f.operation, f.severity.value) for f in corpus_findings()}


# A operação e a severidade que cada linha de `django._OPERATIONS` devolve no
# corpus. Onde a linha tem refinamento — `AlterField` e `AlterUniqueTogether`
# comparados com o estado anterior, `AddField` sem default, `AddConstraint`
# UNIQUE, `RunSQL` literal — o par abaixo é o da **linha crua**, o que a tabela
# promete quando nada a refina.
DJANGO_ROWS: dict[str, tuple[str, Severity]] = {
    "CreateModel":              ("CreateModel",              Severity.SAFE),
    "DeleteModel":              ("DeleteModel",              Severity.BREAKING),
    "RenameModel":              ("RenameModel",              Severity.BREAKING),
    "AlterModelOptions":        ("AlterModelOptions",        Severity.NONE),
    "AlterOrderWithRespectTo":  ("AlterOrderWithRespectTo",  Severity.CONTROLLED),
    "AddField":                 ("AddField",                 Severity.SAFE),
    "RemoveField":              ("RemoveField",              Severity.BREAKING),
    "AlterField":               ("AlterField",               Severity.UNKNOWN),
    "RenameField":              ("RenameField",              Severity.BREAKING),
    "AddIndex":                 ("AddIndex",                 Severity.SAFE),
    "RemoveIndex":              ("RemoveIndex",              Severity.CONTROLLED),
    "AddConstraint":            ("AddConstraint",            Severity.CONTROLLED),
    "RemoveConstraint":         ("RemoveConstraint",         Severity.CONTROLLED),
    "AlterUniqueTogether":      ("AlterUniqueTogether",      Severity.BREAKING),
    "RunPython":                ("RunPython",                Severity.CONTROLLED),
    "RunSQL":                   ("RunSQL",                   Severity.UNKNOWN),
    "SeparateDatabaseAndState": ("SeparateDatabaseAndState", Severity.UNKNOWN),
}

ALEMBIC_ROWS: dict[str, tuple[str, Severity]] = {
    "create_table":             ("op.create_table",             Severity.SAFE),
    "drop_table":               ("op.drop_table",               Severity.BREAKING),
    "rename_table":             ("op.rename_table",             Severity.BREAKING),
    "add_column":               ("op.add_column",               Severity.SAFE),
    "drop_column":              ("op.drop_column",              Severity.BREAKING),
    "alter_column":             ("op.alter_column",             Severity.UNKNOWN),
    "create_index":             ("op.create_index",             Severity.SAFE),
    "drop_index":               ("op.drop_index",               Severity.CONTROLLED),
    "create_unique_constraint": ("op.create_unique_constraint", Severity.BREAKING),
    "create_primary_key":       ("op.create_primary_key",       Severity.BREAKING),
    "drop_constraint":          ("op.drop_constraint",          Severity.CONTROLLED),
    "create_foreign_key":       ("op.create_foreign_key",       Severity.CONTROLLED),
    "execute":                  ("op.execute",                  Severity.UNKNOWN),
    "f":                        ("op.f",                        Severity.NONE),
}


class TestTheCorpusReachesEveryDjangoOperation:
    """A linha tem que ser **classificada** pelo corpus, não só escrita nele.

    A versão anterior deste teste procurava `migrations.{name}(` no texto das
    fixtures, e por isso passava com a única chamada de verdade apagada e a
    string sobrevivendo dentro de um comentário. É a mesma forma que a revisão
    da QQ-2161 achou em `sql.py` — asserção que não distingue "a regra
    funcionou" de "a regra sumiu".
    """

    def test_the_row_map_has_the_same_keys_as_the_table(self):
        assert set(DJANGO_ROWS) == set(django._OPERATIONS)

    @pytest.mark.parametrize("row", sorted(DJANGO_ROWS))
    def test_the_row_reaches_the_corpus_with_the_severity_it_promises(self, row):
        operation, severity = DJANGO_ROWS[row]
        assert (operation, severity.value) in corpus_pairs(), row

    def test_the_declared_severity_is_the_one_the_table_declares(self):
        """O mapa acima copia a tabela, então ele tem que continuar copiando.

        Sem isto, alguém que troque a severidade de uma linha em `django.py`
        conserta o teste editando o mapa e o corpus continua verde.
        """
        drift = {
            row: (severity.value, django._OPERATIONS[row].severity.value)
            for row, (_, severity) in DJANGO_ROWS.items()
            if severity is not django._OPERATIONS[row].severity
        }
        assert drift == {}


class TestTheCorpusReachesEveryAlembicOperation:
    def test_the_row_map_has_the_same_keys_as_the_table(self):
        assert set(ALEMBIC_ROWS) == set(alembic._OPERATIONS)

    @pytest.mark.parametrize("row", sorted(ALEMBIC_ROWS))
    def test_the_row_reaches_the_corpus_with_the_severity_it_promises(self, row):
        operation, severity = ALEMBIC_ROWS[row]
        assert (operation, severity.value) in corpus_pairs(), row

    def test_the_declared_severity_is_the_one_the_table_declares(self):
        drift = {
            row: (severity.value, alembic._OPERATIONS[row].severity.value)
            for row, (_, severity) in ALEMBIC_ROWS.items()
            if severity is not alembic._OPERATIONS[row].severity
        }
        assert drift == {}


# A operação e a severidade que cada linha de `_ALTER_TABLE_RULES` produz no
# corpus. A chave é o nome do grupo da regex de ações, igual à chave da tabela.
ALTER_TABLE_ROWS: dict[str, tuple[str, Severity]] = {
    "add_constraint":  ("ADD CONSTRAINT",     Severity.CONTROLLED),
    "add_unique_index": ("ADD UNIQUE INDEX",  Severity.BREAKING),
    "add_key":         ("ADD FOREIGN KEY",    Severity.CONTROLLED),
    "add_index":       ("ADD INDEX",          Severity.SAFE),
    "add_column":      ("ADD COLUMN",         Severity.SAFE),
    "drop_constraint": ("DROP CONSTRAINT",    Severity.CONTROLLED),
    "drop_key":        ("DROP KEY",           Severity.CONTROLLED),
    "drop_column":     ("DROP COLUMN",        Severity.BREAKING),
    "alter_column":    ("ALTER COLUMN",       Severity.UNKNOWN),
    "modify":          ("MODIFY",             Severity.BREAKING),
    "change":          ("CHANGE",             Severity.BREAKING),
    "rename_column":   ("RENAME COLUMN",      Severity.BREAKING),
    "rename_table":    ("RENAME TO",          Severity.BREAKING),
}

ALTER_COLUMN_ROWS: dict[str, tuple[str, Severity]] = {
    "SET NOT NULL":  ("ALTER COLUMN ... SET NOT NULL",  Severity.BREAKING),
    "DROP NOT NULL": ("ALTER COLUMN ... DROP NOT NULL", Severity.CONTROLLED),
    "TYPE":          ("ALTER COLUMN ... TYPE",          Severity.BREAKING),
    "SET DEFAULT":   ("ALTER COLUMN ... SET DEFAULT",   Severity.CONTROLLED),
    "DROP DEFAULT":  ("ALTER COLUMN ... DROP DEFAULT",  Severity.CONTROLLED),
}

# Keyed pela `head` de cada `_Statement`, que é a chave da linha.
STATEMENT_ROWS: dict[str, tuple[str, Severity]] = {
    r"(?:START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK)\b": ("BEGIN", Severity.NONE),
    r"SET\b":                                    ("SET",             Severity.NONE),
    r"COMMENT\s+ON\b":                           ("COMMENT ON",      Severity.NONE),
    r"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\b":   ("CREATE TABLE",    Severity.SAFE),
    r"DROP\s+TABLE\b":                           ("DROP TABLE",      Severity.BREAKING),
    r"TRUNCATE\b":                               ("TRUNCATE",        Severity.BREAKING),
    r"INSERT\b":                                 ("INSERT",          Severity.CONTROLLED),
    r"UPDATE\b":                                 ("UPDATE",          Severity.CONTROLLED),
    r"DELETE\b":                                 ("DELETE",          Severity.CONTROLLED),
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\b":           ("CREATE INDEX",    Severity.SAFE),
    r"DROP\s+INDEX\b":                           ("DROP INDEX",      Severity.CONTROLLED),
    r"CREATE\s+TYPE\b":                          ("CREATE TYPE",     Severity.SAFE),
    r"DROP\s+TYPE\b":                            ("DROP TYPE",       Severity.CONTROLLED),
    r"ALTER\s+TYPE\b":                           ("ALTER TYPE",      Severity.UNKNOWN),
    r"CREATE\s+SEQUENCE\b":                      ("CREATE SEQUENCE", Severity.SAFE),
}


class TestTheCorpusReachesEverySqlRule:
    """A invariante que faltava nas duas tabelas de `sql.py`.

    A auditoria da QQ-2161 achou `add_key` e `drop_key` sem nenhuma afirmação de
    severidade na suíte: trocar `CONTROLLED` por `SAFE` nas duas passava verde.
    O par de asserções abaixo — paridade de chaves com a tabela, mais a
    severidade que o corpus devolve — fecha isso.
    """

    def test_the_alter_table_row_map_has_the_same_keys_as_the_table(self):
        assert set(ALTER_TABLE_ROWS) == set(sql._ALTER_TABLE_RULES)

    def test_the_alter_column_row_map_has_the_same_keys_as_the_table(self):
        assert set(ALTER_COLUMN_ROWS) == {name for name, _, _ in sql._ALTER_COLUMN_RULES}

    def test_the_statement_row_map_has_the_same_keys_as_the_table(self):
        assert set(STATEMENT_ROWS) == {s.head for s in sql._STATEMENT_RULES}

    @pytest.mark.parametrize(
        "row, expected",
        sorted(
            (list(ALTER_TABLE_ROWS.items())
             + list(ALTER_COLUMN_ROWS.items())
             + list(STATEMENT_ROWS.items())),
            key=lambda item: item[0],
        ),
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_the_row_reaches_the_corpus_with_the_severity_it_promises(self, row, expected):
        operation, severity = expected
        assert (operation, severity.value) in corpus_pairs(), row


class TestTheCorpusReachesEveryTypeormGate:
    @pytest.mark.parametrize("name", sorted(typeorm._GATES))
    def test_the_gate_fires_on_some_fixture(self, name):
        gate = typeorm._GATES[name]
        reasons = [f.reason for f in corpus_findings()]
        assert gate.reason in reasons, name


class TestTheCorpusReachesEveryDoctrineGate:
    """Os seis portões do `doctrine.py`, alcançados por arquivo em disco.

    `tests/test_doctrine.py` já exercita cada portão com fonte escrita à mão. O
    que esta classe acrescenta é a mesma exigência do outro lado: a linha tem que
    ser alcançada **através do dispatch, por um arquivo versionado** — é assim
    que a QQ-2161 achou `add_key` e `drop_key` sem nenhuma afirmação de
    severidade na suíte.
    """

    @pytest.mark.parametrize("name", sorted(doctrine._GATES))
    def test_the_gate_fires_on_some_fixture(self, name):
        gate = doctrine._GATES[name]
        reasons = [f.reason for f in corpus_findings()]
        assert gate.reason in reasons, name


class TestTheCorpusReachesEveryDispatchReason:
    """As cinco razões que o próprio dispatch produz, cada uma por um arquivo."""

    @pytest.mark.parametrize(
        "name, expected",
        [
            (
                "dispatch/activerecord_style_migration.rb",
                detect.NO_STACK.format(suffix=".rb"),
            ),
            ("dispatch/broken_syntax_migration.py", detect.NOT_PYTHON),
            ("dispatch/ambiguous_django_and_alembic.py", detect.AMBIGUOUS),
            (
                "dispatch/yoyo_style_migration.py",
                detect.UNRECOGNISED.format(stacks="django nem alembic"),
            ),
            (
                "dispatch/south_style_migration.py",
                detect.SILENT_PARSER.format(stack="django"),
            ),
            # A do stack novo: o gerador de migrações, que traz o marcador do
            # Doctrine escrito dentro de um heredoc. O marcador roda sobre o
            # texto apagado, então ele não casa, e a resposta é a de dialeto que
            # o pacote não conhece. A migração escrita com o schema builder não
            # passa mais por aqui: o `doctrine.py` responde por ela sozinho.
            (
                "dispatch/doctrine_generator.php",
                detect.UNRECOGNISED.format(stacks="doctrine"),
            ),
        ],
    )
    def test_the_reason_is_the_one_the_dispatch_row_declares(self, name, expected):
        """A frase inteira, renderizada a partir da constante de `detect/`.

        O manifesto já prende o texto que sai; o que este teste acrescenta é o
        vínculo com a constante — reescrever `NO_STACK` sem regravar o manifesto
        quebra os dois, e reescrever o manifesto sem tocar na constante quebra
        este.
        """
        findings = findings_of(name)
        assert len(findings) == 1
        assert findings[0].severity is Severity.UNKNOWN
        assert findings[0].operation == detect.DISPATCH
        assert findings[0].reason == expected

    def test_a_module_without_a_call_is_not_a_migration_and_not_an_unknown(self):
        assert findings_of("dispatch/not_a_migration.py") == []


# ---------------------------------------------------------------------------
# O corpus real, quando ele está por perto
# ---------------------------------------------------------------------------

WORKSPACE = Path(__file__).resolve().parents[3]

# Por que o mapa real↔sintético vive fora do repositório
# -----------------------------------------------------
#
# `iclinic/automations` é **público**, e as fixtures do corpus não podem carregar
# identificador de produção: nem no nome do arquivo, nem no app, nem na tabela ou
# na coluna que a razão cita. Só que `test_the_adr_fixtures_say_what_the_real_
# files_say` compara a frase renderizada da fixture com a do arquivo real, letra
# por letra, e as duas só batem se alguém souber qual identificador sintético
# está no lugar de qual identificador real.
#
# Esse alguém é `tests/fixtures/.identifier-map.json`, que o `.gitignore` prende
# fora de qualquer commit. Quem tem os clones ao lado escreve o mapa e os testes
# daqui rodam; quem não tem — o CI, e qualquer pessoa que clone o público — vê os
# testes pularem, do mesmo jeito que eles já pulavam sem os clones.
IDENTIFIER_MAP_PATH = Path(__file__).parent / "fixtures" / ".identifier-map.json"

IDENTIFIER_MAP: dict | None = (
    json.loads(IDENTIFIER_MAP_PATH.read_text(encoding="utf-8"))
    if IDENTIFIER_MAP_PATH.is_file()
    else None
)

NO_MAP = (
    "tests/fixtures/.identifier-map.json não está presente — o mapa "
    "real↔sintético fica fora do repositório, que é público"
)


def _to_real(text: str, mapping: dict[str, str]) -> str:
    """Traduz a frase sintética de volta para os identificadores reais.

    Casa identificador inteiro — fronteira ciente de `_` — para que o nome de
    uma tabela não seja lido como o nome do app seguido de sufixo.
    """
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])("
        + "|".join(sorted((re.escape(k) for k in mapping), key=len, reverse=True))
        + r")(?![A-Za-z0-9_])"
    )
    return pattern.sub(lambda match: mapping[match.group(1)], text)


def _real_root(stack: str) -> Path | None:
    if IDENTIFIER_MAP is None:
        return None
    return WORKSPACE / IDENTIFIER_MAP["consumers"][stack]


def _real_files(stack: str) -> list[Path]:
    root = _real_root(stack)
    if root is None or not root.is_dir():
        return []
    return sorted(
        p for p in root.glob(REAL_CORPUS[stack]["glob"]) if p.name != "__init__.py"
    )


def _why_it_skipped(stack: str) -> str:
    if IDENTIFIER_MAP is None:
        return NO_MAP
    return f"o clone do consumidor {stack} não está no workspace"


class TestTheRealCorpus:
    """A medição dos três repositórios, reconferida quando eles estão ao lado.

    Pula no CI do `automations`, e é de propósito: o aceite exige que o portão
    de regressão não dependa de checkout dos consumidores. O que roda lá é a
    partição exata sobre o corpus versionado. Este aqui é o que mantém o número
    real sob controle de versão e verificável por quem tem os três clones.
    """

    @pytest.mark.parametrize("stack", sorted(REAL_CORPUS))
    def test_the_repository_did_not_lose_the_migrations_measured(self, stack):
        """O repositório não encolhe, e cresce o tempo todo.

        `==` contra a contagem de um repositório externo põe a suíte vermelha
        na máquina de quem tem os três clones toda vez que um consumidor
        mergeia uma migração — o consumidor Django já está em 599 contra os 591
        da medição da ADR, sem que nenhuma delas tenha mexido na classificação.
        O que este teste protege é o outro lado: o glob parar de casar o que
        casava. Quem guarda a qualidade da classificação é o `<=` de
        `test_the_determinism_rate_did_not_fall`, que é o número que só pode
        piorar.
        """
        files = _real_files(stack)
        if not files:
            pytest.skip(_why_it_skipped(stack))
        assert len(files) >= REAL_CORPUS[stack]["files"]

    @pytest.mark.parametrize("stack", sorted(REAL_CORPUS))
    def test_the_determinism_rate_did_not_fall(self, stack):
        files = _real_files(stack)
        if not files:
            pytest.skip(_why_it_skipped(stack))
        unknown = [
            p
            for p in files
            if any(f.severity is Severity.UNKNOWN for f in detect.classify_file(p))
        ]
        assert len(unknown) <= REAL_CORPUS[stack]["unknown_files"], sorted(
            str(p) for p in unknown
        )

    def test_the_typeorm_statements_did_not_lose_coverage(self):
        """O segundo número do repositório de vídeo: statements, não arquivos.

        O total é piso pelo mesmo motivo da contagem de arquivos: o repositório
        cresce. A migração que entrou em 17/08 trouxe dois `ADD COLUMN` e deixou
        o `==` vermelho sem mudar nenhuma classificação. Quem guarda a qualidade
        é o `<=` do `unknown` logo abaixo.
        """
        files = _real_files("typeorm")
        if not files:
            pytest.skip(_why_it_skipped("typeorm"))
        findings = [f for p in files for f in detect.classify_file(p)]
        unknown = [f for f in findings if f.severity is Severity.UNKNOWN]
        assert len(findings) >= REAL_TYPEORM_STATEMENTS["total"]
        assert len(unknown) <= REAL_TYPEORM_STATEMENTS["unknown"]
        assert {f.operation for f in unknown} == {"ALTER TYPE"}

    def test_the_doctrine_statements_did_not_lose_coverage(self):
        """O segundo número do consumidor Doctrine: statements, não arquivos.

        O total é piso, pelo mesmo motivo do TypeORM: o consumidor cresce, e um
        `==` ficaria vermelho a cada migração nova sem nenhuma classificação ter
        mudado. Quem guarda a qualidade é o teto do `unknown`.
        """
        files = _real_files("doctrine")
        if not files:
            pytest.skip(_why_it_skipped("doctrine"))
        findings = [f for p in files for f in detect.classify_file(p)]
        unknown = [f for f in findings if f.severity is Severity.UNKNOWN]
        assert len(findings) >= REAL_DOCTRINE["statements"]["total"]
        assert len(unknown) <= REAL_DOCTRINE["statements"]["unknown"], [
            f.operation for f in unknown
        ]

    def test_every_doctrine_data_statement_is_controlled(self):
        """Metade do consumidor é migração de dados, e ela sai `controlled`.

        É a mesma severidade do `RunPython` de dados do Django. O `DELETE` sem
        `WHERE` seria `breaking`, como o `TRUNCATE`; o corpus real não tem
        nenhum, e um que entrasse derrubaria este teste para alguém olhar.
        """
        files = _real_files("doctrine")
        if not files:
            pytest.skip(_why_it_skipped("doctrine"))
        data = [
            f
            for p in files
            for f in detect.classify_file(p)
            if f.operation in REAL_DOCTRINE["data_operations"]
        ]
        assert data  # senão o conjunto abaixo é vazio e não afirma nada
        assert {f.operation for f in data} == REAL_DOCTRINE["data_operations"]
        assert {f.severity for f in data} == {Severity.CONTROLLED}

    def test_both_databases_of_the_doctrine_consumer_are_in_the_flow(self):
        """O motivo de existir do cartão, medido.

        As migrações do consumidor moram em um diretório por banco, e um glob que
        alcançasse só um deles deixaria o outro sem alerta nenhum — o silêncio
        que esta action existe para acabar. O teste conta por diretório e exige
        que os dois tenham arquivo **classificado**, não só coletado.
        """
        files = _real_files("doctrine")
        if not files:
            pytest.skip(_why_it_skipped("doctrine"))
        by_database: dict[str, int] = {}
        for path in files:
            assert detect.stack_for(path, detect.read_source(path)) == "doctrine", path
            by_database[path.parent.name] = by_database.get(path.parent.name, 0) + 1
        # As bases são igualdade; a contagem de cada uma é piso, porque as duas
        # recebem migração nova.
        assert set(by_database) == set(REAL_DOCTRINE["databases"])
        for database, floor in REAL_DOCTRINE["databases"].items():
            assert by_database[database] >= floor, database

    def test_the_adr_fixtures_say_what_the_real_files_say(self):
        """As fixturas da ADR reproduzem o veredito dos arquivos reais.

        É o que amarra o corpus sintético ao corpus real: se um dia o
        classificador mudar o que devolve para a migração 0042 do primeiro app,
        o manifesto daqui e este teste discordam ao mesmo tempo.

        A comparação passa pelo mapa de identificadores porque a fixture é
        sintética de ponta a ponta — nome de arquivo, app, tabela e coluna. A
        razão que a fixture devolve é traduzida de volta para os identificadores
        reais antes de encostar na razão do arquivo real; sem isso a asserção
        teria que afrouxar para um fragmento, e um fragmento não distingue "a
        regra certa disparou" de "alguma regra disparou".
        """
        if IDENTIFIER_MAP is None:
            pytest.skip(NO_MAP)
        root = _real_root("django")
        if root is None or not root.is_dir():
            pytest.skip(_why_it_skipped("django"))

        pairs = IDENTIFIER_MAP["adr_fixtures"]
        # O mapa não pode cobrir menos que os quatro arquivos que o corpus
        # versionado nomeia — senão ele silencia o teste em vez de habilitá-lo.
        assert set(pairs) == set(ADR_FILES) | set(RUN_PYTHON_DDL)

        identifiers = IDENTIFIER_MAP["identifiers"]
        for fixture, real in sorted(pairs.items()):
            # Severidade, operação **e a frase renderizada**. O docstring deste
            # módulo diz "statement por statement e frase por frase", e sem a
            # razão a asserção ficava mais fraca que a afirmação.
            got = shape(detect.classify_file(root / real))
            want = [
                {**entry, "reason": _to_real(entry["reason"], identifiers)}
                for entry in shape(findings_of(fixture))
            ]
            assert got == want, fixture
