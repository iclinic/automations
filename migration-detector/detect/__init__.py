"""Classificação determinística de impacto de migrações de banco.

Este módulo é o dispatch: dado um caminho, decide **qual parser lê o arquivo** e
devolve os `Finding` dele. É a única porta de entrada do pacote para quem está
de fora — `classify.py` não importa `sql`, `django`, `alembic`, `typeorm` nem
`doctrine` direto.

O dispatch é uma tabela, `STACKS`, e não uma cadeia de `if`. Cada linha diz três
coisas: por qual extensão o arquivo entra, qual marcador de conteúdo confirma o
stack, e quem classifica.

Extensão sozinha resolve `.sql`. Não resolve `.py`, que é onde moram Django e
Alembic; nem `.ts`, onde o TypeORM divide o glob `**/migrations/*.ts` com
qualquer outro migrador de TypeScript; nem `.php`, onde o glob do Doctrine casa
tanto a migração quanto o **gerador** de migrações do consumidor, que traz
`extends AbstractMigration` escrito por extenso dentro de um heredoc. Daí a
coluna do marcador.


Django ou Alembic: por que o sinal é o conteúdo, e não o caminho
---------------------------------------------------------------

Os globs de `migration_paths` sugerem separar pelo caminho: `**/migrations/*.py`
seria Django e `**/alembic/versions/*.py` seria Alembic. Nos três consumidores
de hoje isso até funcionaria — o repo Django põe as migrações em
`django/app/<app>/migrations/` e o de Alembic em `app/alembic/versions/`.
Mas o caminho é convenção configurável nos dois frameworks: o Alembic aponta
onde quiser por `script_location` no `alembic.ini`, e o tutorial oficial dele
usa justamente `migrations/` como diretório de versões; o Django remapeia por
`MIGRATION_MODULES`. Um projeto Alembic com `script_location = migrations`
casaria `**/migrations/*.py` e cairia no parser do Django.

E o custo de errar o dispatch é exatamente o bug que esta entrega existe para
matar: entregue a fonte de um stack ao parser do outro, os dois devolvem lista
vazia — não um erro. Lista vazia lê-se, mais adiante, como "nada a reportar", o
job termina verde e ninguém olha a migração.

Então o sinal é o conteúdo, e o marcador de cada stack é o **contrato de
runtime do framework**, não uma convenção:

- o Django carrega `module.Migration`, então o marcador é declarar uma classe
  `Migration`;
- o Alembic chama `module.upgrade()`, então o marcador é declarar uma função
  `upgrade`;
- o TypeORM exige `implements MigrationInterface`, então é esse o marcador. Por
  texto, e não por AST, porque o pacote não parseia TypeScript;
- o Doctrine só executa subclasse de `AbstractMigration`, então é esse o
  marcador — mas lido **em posição de código**, sobre o texto que o scanner do
  `doctrine.py` já apagou. É o que separa a migração do gerador que a escreve.

Os dois marcadores de `.py` são as funções `declares_migration_class` e
`declares_upgrade`, e o de `.php` é `doctrine.declares_migration`: todos moram
ao lado do portão de confiança do seu parser e enxergam exatamente o que ele
enxerga. Isso importa nas duas direções. Um
marcador mais **largo** que o portão manda para o parser um arquivo sobre o qual
ele não tem o que dizer, e a resposta volta lista vazia; um marcador mais
**estreito** deixa passar batido um arquivo que o parser classificaria como
`unknown`. As duas divergências terminam no mesmo lugar: silêncio.

`.sql` é a única linha sem marcador, e por um motivo: `sql.py` classifica todo
statement e devolve `unknown` para o verbo que não reconhece, então um `.sql` de
dialeto estranho já sai visível pelo próprio parser.


Nada sai em silêncio
--------------------

Quando nenhum marcador casa, duas coisas muito diferentes podem estar
acontecendo, e confundi-las é o pior defeito possível aqui:

- o arquivo **não declara operação nenhuma** — `__init__.py` de um pacote
  `migrations/`, módulo auxiliar, barrel de TypeScript. Os frameworks também o
  ignorariam. Não é uma migração, e zero findings é a resposta certa;
- o arquivo **declara operações num framework que este pacote não conhece** —
  yoyo, South, peewee, Knex, Prisma. Ele casou `migration_paths`, logo alguém o
  considera migração. Aqui a resposta é `unknown`.

O que separa os dois é `_declares_operations`: em todo dialeto de migração uma
operação é uma **chamada**. Um módulo sem chamada nenhuma não declara operação
nenhuma.

Há ainda um último portão, depois de o parser rodar: parser certo escolhido,
zero findings, e o arquivo declara operações. É o caso do South, que traz
`class Migration` — casando o marcador do Django — com `def forwards` e nenhum
atributo `operations`. Também vira `unknown`.

Tudo isso existe porque errar aqui não estoura. Entregue a fonte de um stack ao
parser do outro, ou um dialeto desconhecido a qualquer um deles, e a resposta é
lista vazia — não um erro. Lista vazia lê-se, mais adiante, como "nada a
reportar": o job termina verde, `has_db_change` sai `false`, e ninguém olha a
migração que dropou a coluna.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import alembic, django, doctrine, history, sql, typeorm
from ._reading import declares_migration_class
from .alembic import declares_upgrade
from .doctrine import declares_migration as declares_doctrine_migration
from .severity import MANUAL, Finding, Severity

__all__ = [
    "AMBIGUOUS",
    "SILENT_PARSER",
    "UNRECOGNISED",
    "DISPATCH",
    "NOT_PYTHON",
    "NO_STACK",
    "STACKS",
    "classify_file",
    "read_source",
    "stack_for",
]


# ---------------------------------------------------------------------------
# Os marcadores de conteúdo
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _parsed(source: str) -> ast.Module | None:
    """A árvore da fonte, ou `None` se não for Python válido.

    Memoizada porque os dois marcadores de `.py`, o portão de "não é Python" e a
    checagem de operações leem a mesma árvore do mesmo arquivo. O parser do
    stack escolhido ainda parseia por conta própria — ele recebe `source`, não a
    árvore, e manter a assinatura dele é o que deixa cada parser testável
    sozinho.

    A chave é a fonte inteira, então o cache retém até oito arquivos completos e
    suas árvores. `read_source` não trunca mais, então isso é o tamanho real dos
    arquivos: as maiores migrações do corpus passam de 20 KB. Um PR traz poucos
    arquivos e o processo morre no fim do step, então o teto é irrelevante aqui —
    mas quem reusar isto em lote deve saber o que está segurando.
    """
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _is_django(source: str) -> bool:
    """Declara uma classe `Migration`? É o que o Django carrega."""
    tree = _parsed(source)
    return tree is not None and declares_migration_class(tree)


def _is_alembic(source: str) -> bool:
    """Declara uma função `upgrade`? É o que o Alembic chama."""
    tree = _parsed(source)
    return tree is not None and declares_upgrade(tree)


def _is_typeorm(source: str) -> bool:
    """Implementa `MigrationInterface`? É o contrato que o TypeORM exige.

    Texto e não AST porque o pacote não parseia TypeScript — `typeorm.py` lê o
    corpo de `up()` por varredura. Os 34 arquivos do corpus do consumidor
    TypeORM/PostgreSQL trazem o nome.
    """
    return "MigrationInterface" in source


def _is_doctrine(source: str) -> bool:
    """Estende `AbstractMigration`, em posição de código? É o que o Doctrine executa.

    O marcador é `doctrine.declares_migration`, e ele mora no parser porque
    precisa do scanner do parser: o consumidor tem um **gerador de migrações**
    que monta o arquivo novo dentro de um heredoc, e esse heredoc contém
    `extends AbstractMigration` e `public function up(Schema $schema): void`
    escritos por extenso. Um marcador por substring — como o do TypeORM, que
    pode ser um porque nenhum arquivo do consumidor de lá gera TypeScript —
    leria o gerador como migração.

    Rodando sobre o texto que o scanner apagou, o marcador vê exatamente o que o
    parser vê: dentro do heredoc não há código, então o gerador não casa.
    """
    return declares_doctrine_migration(source)


_LINE_COMMENT = re.compile(r"//[^\n]*")


def _declares_operations(source: str) -> bool:
    """A fonte declara alguma operação, mesmo que num dialeto desconhecido?

    Em todo dialeto de migração uma operação é uma **chamada**:
    `migrations.RemoveField(...)`, `op.drop_column(...)`, `queryRunner.query(...)`,
    `step("ALTER TABLE ...")` do yoyo, `knex.raw(...)`. Um módulo sem chamada
    nenhuma não declara operação nenhuma.

    É o que separa "este arquivo não é uma migração" — o `__init__.py` vazio de
    um pacote `migrations/`, um barrel `export * from ...` — de "este arquivo é
    uma migração de um framework que eu não conheço", que é um `unknown`.
    """
    tree = _parsed(source)
    if tree is not None:
        return any(isinstance(node, ast.Call) for node in ast.walk(tree))
    return "(" in _LINE_COMMENT.sub("", source)


# ---------------------------------------------------------------------------
# Os classificadores, com a assinatura que a tabela usa
# ---------------------------------------------------------------------------


def _classify_sql(source: str, path: Path) -> list[Finding]:
    return sql.classify_sql(source)


def _classify_typeorm(source: str, path: Path) -> list[Finding]:
    return typeorm.classify_migration(source)


def _classify_alembic(source: str, path: Path) -> list[Finding]:
    return alembic.classify_migration(source)


def _classify_doctrine(source: str, path: Path) -> list[Finding]:
    return doctrine.classify_migration(source)


def _classify_django(source: str, path: Path) -> list[Finding]:
    """Django, com o estado anterior do app reconstruído a partir do disco.

    `prior_state` lê os ancestrais da migração no diretório `migrations/` e é o
    que permite dizer se um `AlterField` encurtou a coluna, tornou o campo NOT
    NULL ou só mexeu num `help_text`. Sem ele, `AlterField` — a operação mais
    comum do corpus — sai `unknown` porque carrega apenas o estado final.

    O `AppState` é criado aqui, dentro da chamada que o consome, e morre com
    ela. Ele avança operação a operação enquanto o arquivo é lido, então
    reaproveitá-lo num segundo arquivo levaria as operações do primeiro para o
    estado anterior do segundo — `claim()` levanta `RuntimeError` nesse caso.
    Um arquivo, um `AppState`.
    """
    return django.classify_migration(source, prior=history.prior_state(path))


# ---------------------------------------------------------------------------
# A tabela
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stack:
    """Uma linha do dispatch.

    `marker` é `None` quando a extensão já resolve sozinha. Quando duas linhas
    dividem a mesma extensão, as duas precisam de marcador — é ele que decide.
    """

    name: str
    suffix: str
    marker: Callable[[str], bool] | None
    classify: Callable[[str, Path], list[Finding]]


STACKS: tuple[_Stack, ...] = (
    # `.sql` não tem marcador porque não precisa: `sql.py` classifica todo
    # statement e devolve `unknown` para o verbo que não reconhece, então um
    # `.sql` de dialeto estranho não some — ele aparece como `unknown` vindo do
    # parser. Os outros quatro precisam, cada um pelo contrato de runtime do seu
    # framework.
    _Stack("sql", ".sql", None, _classify_sql),
    _Stack("typeorm", ".ts", _is_typeorm, _classify_typeorm),
    _Stack("doctrine", ".php", _is_doctrine, _classify_doctrine),
    _Stack("django", ".py", _is_django, _classify_django),
    _Stack("alembic", ".py", _is_alembic, _classify_alembic),
)


# ---------------------------------------------------------------------------
# As razões que o próprio dispatch produz
# ---------------------------------------------------------------------------

# `operation` de um finding do dispatch. Não sai do conteúdo do arquivo: o
# dispatch não leu operação nenhuma, ele não chegou lá.
DISPATCH = "Arquivo"

NO_STACK = (
    "Extensão `{suffix}` não tem classificador — o arquivo casou `migration_paths` "
    "mas nenhum parser lê esse formato — " + MANUAL
)

NOT_PYTHON = "Arquivo não é Python válido — o classificador não leu as operações — " + MANUAL

AMBIGUOUS = (
    "O arquivo define `Migration` e `upgrade` ao mesmo tempo — o classificador não "
    "sabe se é Django ou Alembic — " + MANUAL
)

UNRECOGNISED = (
    "O arquivo casou `migration_paths` e declara operações, mas não é uma migração de "
    "{stacks} — o classificador não conhece este formato — " + MANUAL
)

SILENT_PARSER = (
    "O arquivo declara operações que o classificador de {stack} não leu — " + MANUAL
)


def _unreadable(reason: str) -> list[Finding]:
    return [Finding(Severity.UNKNOWN, DISPATCH, reason)]


# ---------------------------------------------------------------------------
# A escolha
# ---------------------------------------------------------------------------


def _select(path: Path, source: str) -> _Stack | str | None:
    """O stack que lê este arquivo, a razão de não dar para decidir, ou `None`.

    Três respostas, e a diferença entre elas é o ponto do dispatch:

    - `_Stack` — este parser lê o arquivo;
    - `str` — não dá para decidir, e a string é a razão do `unknown`. Devolver a
      razão em vez de nada é o que permite `classify_file` publicar um `unknown`
      que diz o que houve, em vez de um arquivo que sumiu da saída;
    - `None` — o arquivo não declara operação nenhuma, e isso é uma resposta
      completa: não é uma migração. Não é uma dúvida, então não é um `unknown`.
    """
    suffix = path.suffix.lower()
    candidates = [stack for stack in STACKS if stack.suffix == suffix]

    if not candidates:
        return NO_STACK.format(suffix=suffix or path.name)

    if len(candidates) == 1 and candidates[0].marker is None:
        return candidates[0]

    if suffix == ".py" and _parsed(source) is None:
        return NOT_PYTHON

    matched = [stack for stack in candidates if stack.marker and stack.marker(source)]

    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        return AMBIGUOUS

    # Nenhum marcador. Duas coisas muito diferentes moram aqui, e confundi-las é
    # o pior defeito possível neste pacote:
    #
    #   - o arquivo não declara operação nenhuma — `__init__.py` de um pacote
    #     `migrations/`, módulo auxiliar, barrel de TypeScript. Django, Alembic,
    #     TypeORM e Doctrine também o ignorariam. Não é uma migração;
    #   - o arquivo declara operações num framework que este pacote não conhece —
    #     yoyo, South, peewee, Knex, Prisma, Phinx. Ele casou `migration_paths`, então
    #     alguém o considera migração. Devolver lista vazia aqui faria um PR que
    #     dropa coluna terminar verde com `has_db_change=false`, que é
    #     exatamente o bug que esta entrega existe para matar.
    #
    # A mesma lógica que `NO_STACK` já aplicava à extensão desconhecida: casou o
    # glob e não sei ler, logo é `unknown`.
    if _declares_operations(source):
        return UNRECOGNISED.format(
            stacks=" nem ".join(stack.name for stack in candidates)
        )
    return None


def stack_for(path: str | Path, source: str) -> str | None:
    """Nome do stack que lê este arquivo, ou `None` se nenhum lê.

    `None` cobre tanto "não é migração" quanto "não dá para decidir"; quem
    precisa da diferença chama `classify_file` e lê a razão do finding.
    """
    selected = _select(Path(path), source)
    return selected.name if isinstance(selected, _Stack) else None


def read_source(path: str | Path) -> str:
    """Lê o arquivo inteiro.

    Sem truncar. O `MAX_FILE_BYTES = 6000` que existia aqui era orçamento de
    prompt, e um classificador que lê a fonte não pode ter isso: 22 das 591
    migrações do consumidor Django passam de 6000 bytes, e um arquivo cortado ou não
    é Python válido — virando um `unknown` falso — ou, pior, ainda parseia e o
    `DROP COLUMN` que estava depois do corte simplesmente não existe.

    Erro de leitura sobe. Arquivo que não abre não vira classificação.
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def classify_file(path: str | Path) -> list[Finding]:
    """Classifica um arquivo de migração pelo parser do seu stack.

    Devolve os `Finding` do parser, na ordem em que ele os produziu. Lista vazia
    significa "não há o que reportar neste arquivo" — um `__init__.py`, uma
    migração de merge, um `BEGIN;` sozinho. Quem chama continua obrigado a
    listar o arquivo na saída: é lá que "nada a reportar" fica visível.

    Um arquivo que o dispatch não sabe encaminhar sai com um `unknown` que diz
    por quê. Nenhum caminho devolve lista vazia por não ter sabido decidir.
    """
    file = Path(path)
    source = read_source(file)
    selected = _select(file, source)

    if selected is None:
        return []
    if isinstance(selected, str):
        return _unreadable(selected)

    findings = selected.classify(source, file)

    # O último portão, e o mais geral: o parser certo foi escolhido e voltou de
    # mãos vazias sobre um arquivo que declara operações. Lista vazia significa
    # "li e não há o que reportar" — uma migração de merge do Django, um barrel
    # de TypeScript — e ela não pode cobrir "li e não entendi".
    #
    # O caso concreto é o South: `class Migration` com `def forwards(self, orm)`
    # e nenhum atributo `operations`. O marcador do Django casa (a classe está
    # lá), `operations_of` não acha a lista e devolve lista vazia, e um
    # `db.delete_column('subjects', 'cpf')` sairia como "nada a reportar". As
    # 591 migrações do consumidor Django declaram `operations`; as 10 de merge
    # declaram `operations = []` e não têm chamada nenhuma, então continuam
    # vazias e continuam certas.
    #
    # O portão não vale para `.sql`, a única linha sem marcador. `sql.py` já
    # devolve `unknown` para o statement que não reconhece, então ele não tem
    # como sair em silêncio — é o mesmo argumento que dispensa o marcador. O que
    # o portão acrescentaria ali é só falso positivo: `_declares_operations` não
    # sabe ler SQL, tenta `ast.parse` num texto que não é Python e cai no teste
    # de parêntese, então uma migração inteiramente comentada com `--` ou `/* */`
    # — artefato de review corriqueiro — viraria um `unknown` vermelho.
    if selected.marker is not None and not findings and _declares_operations(source):
        return _unreadable(SILENT_PARSER.format(stack=selected.name))
    return findings
