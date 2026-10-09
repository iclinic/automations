"""Classificador de migrações do Doctrine Migrations, em PHP.

Lê os statements SQL do corpo do método `up()` e delega cada um para
`detect/sql.py`. Como o `typeorm.py`, não tem tabela de severidade própria: o
consumidor escreve DDL literal dentro de `$this->addSql(...)`, e quem sabe o que
um DDL faz é o `detect/sql.py` — o mesmo módulo que atende o `RunSQL` do Django,
o `op.execute()` do Alembic e o `queryRunner.query()` do TypeORM. O mesmo DDL
tem que sair com a mesma cor nas quatro stacks.

Duas bases, um parser
---------------------

O consumidor deste stack tem **dois bancos**, cada um com o seu diretório de
migrações e a sua conexão: um MySQL, que guarda o financeiro do SaaS, e um
PostgreSQL, que guarda o resto do sistema. Os dois entram pelo mesmo glob de
`migration_paths` e pelo mesmo parser, e é isso que a entrega precisa garantir:
um alerta que cobrisse só o PostgreSQL deixaria a base financeira sem proteção.

**O parser não recebe dica de dialeto, e é de propósito.** O caminho do arquivo
diz qual banco é (`Migrations/mysql/` contra `Migrations/pgsql/`), então a dica
estaria disponível — mas não há o que fazer com ela. `detect/sql.py` reconhece
os dois vocabulários ao mesmo tempo porque a diferença entre eles é vocabulário
e não gramática: `MODIFY col ... NOT NULL` só existe no MySQL, `ALTER COLUMN col
SET NOT NULL` só existe no PostgreSQL, e as duas formas já têm linha própria na
tabela. Não existe statement cuja severidade mude conforme o dialeto, logo não
existe teste que distinga o parâmetro presente do parâmetro ausente — e um
parâmetro que nenhum teste alcança é código que ninguém mantém. Qual banco foi
mexido continua visível onde sempre esteve: no caminho do arquivo, que a
mensagem do Slack imprime ao lado de cada finding.

O scanner, e o que ele não é
----------------------------

O arquivo é PHP, então não há `ast` para chamar. O que existe aqui é o mesmo
scanner léxico do `typeorm.py`: apaga o conteúdo de string, heredoc, nowdoc e
comentário **preservando os índices**, guarda onde cada literal começa e
termina, e então casa a chave que abre o corpo de `up()` com a que o fecha no
texto apagado, lendo o SQL no texto original. Não é um parser de PHP e o aceite
é explícito em não pedir um.

Dentro do corpo de `up()`, a unidade é a chamada de método. De `->addSql()` —
em qualquer caixa, como o PHP lê o nome — sai o SQL, quando o primeiro argumento
é um literal inteiro; todo o resto é `unknown`. Qualquer outra chamada que não
seja leitura também é `unknown`, com o nome do método como operação.
`down()` fica de fora por construção: o que roda no deploy é o `up()`, e
classificar o rollback pintaria de vermelho toda migração que sabe se desfazer.

As duas formas literais do PHP, e as duas que não são
-----------------------------------------------------

O PHP tem quatro sintaxes de string, e elas se dividem em duas famílias pelo
que o **autor declarou**, não pelo que o conteúdo parece ser:

- `'...'` e `<<<'SQL' ... SQL` (nowdoc) **não interpolam nada**. O que está
  escrito é o que roda, e o classificador lê;
- `"..."` e `<<<SQL ... SQL` (heredoc) **interpolam**. O SQL que roda pode não
  estar escrito no arquivo, e classificar a parte legível seria afirmar o que a
  interpolação pode desmentir. Resposta: `unknown`.

A regra é a forma do delimitador, e não uma varredura por `$` no corpo. Varrer
o corpo re-deriva uma resposta que o autor já escreveu — e erra justamente onde
dói, porque `$$` de corpo de função e `$1` de placeholder são SQL legítimo do
PostgreSQL dentro de um heredoc que o PHP nem tentaria interpolar. A forma do
delimitador é decidível sem ler o conteúdo, e é o que este módulo usa.

O parâmetro nomeado, e por que ele não muda nada aqui
-----------------------------------------------------

`addSql()` aceita um segundo argumento com os parâmetros do statement
(`addSql('... WHERE id = :id', ['id' => $id])`). **O segundo argumento nunca é
lido.** Ele carrega valor, não schema, e o corpus real já tem `INSERT ... VALUES
(:test_key, :value)` com dado de cliente do lado de lá — pôr isso numa razão que
vai para um canal do Slack é exatamente o vazamento que `tests/test_pii.py`
guarda.

O primeiro argumento continua indo para o `detect/sql.py` como qualquer outro, e
`INSERT`, `UPDATE` e `DELETE` saem `controlled`, como migração de dados, com a
tabela na razão e nenhum valor. É a mesma resposta que o `RunSQL` do Django e o
`op.execute()` do Alembic dão para o mesmo statement, e ela tem que continuar
sendo a mesma.

Onde o scanner tem teto, e o teto é conhecido
---------------------------------------------

- **SQL que não passa por `->addSql()`** — `$this->connection->executeStatement($sql)`,
  o schema builder do Doctrine (`$schema->createTable(...)`) — não é lido. Cada
  chamada dessas sai `unknown`, e não só quando o arquivo não tem `addSql`
  nenhum: o portão do parser mudo em `detect/__init__.py` não dispara se há um
  finding ao lado, e um `dropTable` junto de um `addSql` seguro sumiria. O que
  passa sem finding é a leitura (`get*`, `has*`, `is*`, `fetch*`) e as guardas
  do `AbstractMigration` (`write`, `warnIf`, `abortIf`, `skipIf`). Chamada
  estática (`Foo::bar()`) e função solta não são lidas. Nenhum arquivo do corpus
  real chama outra coisa além de `$this->addSql()`.
- **`up` definido mais de uma vez** no arquivo devolve `unknown`: sem escopo não
  dá para dizer qual deles o Doctrine chama.
- **Tipo de retorno escrito com chave** (`up($s): array{a: int} {`) faria a
  primeira chave passar por corpo. O corpus escreve `: void`.
- **Modo HTML** — texto fora de `<?php ... ?>` — é lido como se fosse código.
  Migração é arquivo PHP puro; nenhuma do corpus abre e fecha a tag.
- **Atributos do PHP 8** (`#[Foo]`) são reconhecidos como código e não como
  comentário, mas um `#` seguido de `[` dentro de outro contexto confundiria a
  varredura.

Um arquivo sem `up()` devolve lista vazia — não é uma migração que este módulo
saiba ler, e dizer isso é diferente de dizer que nada acontece.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .severity import MANUAL, Finding, Severity
from .sql import classify_sql

__all__ = ["classify_migration", "declares_migration"]


# ---------------------------------------------------------------------------
# Varredura léxica: strings, heredocs e comentários
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Literal:
    """Uma string literal do arquivo, na sintaxe que for.

    `start` é o primeiro caractere do delimitador de abertura — a aspa, ou o
    primeiro `<` de um `<<<`. `end` é o índice logo depois do delimitador que
    fecha. `text` é o conteúdo com os escapes resolvidos; quando `interpolating`,
    ele não vale nada — é o pedaço que estava escrito, não o SQL que roda.
    """

    start: int
    end: int
    text: str
    interpolating: bool


# Escapes que só existem na string com aspas duplas e no heredoc. O nowdoc e a
# string com aspas simples não os têm — lá `\n` são dois caracteres.
_DOUBLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "v": "\v",
    "f": "\f",
    "e": "\x1b",
    "0": "\0",
}

# `<<<SQL`, `<<<"SQL"` e `<<<'SQL'`. O grupo que casar diz se interpola: só o
# rótulo entre aspas simples é nowdoc.
_HEREDOC_OPEN = re.compile(
    r"<<<[ \t]*(?:"
    r"'(?P<nowdoc>[A-Za-z_]\w*)'"
    r"|\"(?P<quoted>[A-Za-z_]\w*)\""
    r"|(?P<bare>[A-Za-z_]\w*)"
    r")\r?\n"
)


class _Scanner:
    """Apaga o texto que não é código e guarda os literais.

    O texto apagado tem o mesmo comprimento do original — é o que permite achar
    a chave no texto apagado e ler o SQL no original com o mesmo índice, do
    mesmo jeito que `detect/sql.py` e `detect/typeorm.py` fazem com as máscaras
    deles.

    Ao contrário do scanner do TypeORM, este não desce dentro da interpolação:
    `"{$a}"` e `<<<SQL ... $a ... SQL` são apagados por inteiro, chaves
    incluídas. O conteúdo de uma string que interpola nunca é lido aqui — o
    literal já sai marcado como `interpolating` e vira `unknown` —, e apagar
    tudo é o que mantém o casamento de chaves do corpo de `up()` correto quando
    o texto apagado é um trecho de PHP, que é exatamente o caso do gerador de
    migrações do consumidor.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.size = len(source)
        self.chars = list(source)
        self.literals: list[_Literal] = []

    def scan(self) -> tuple[str, list[_Literal]]:
        source = self.source
        index = 0
        while index < self.size:
            char = source[index]
            if char == "'":
                index = self._single_quoted(index)
            elif char == '"':
                index = self._double_quoted(index)
            elif source.startswith("<<<", index):
                index = self._heredoc(index)
            elif source.startswith("//", index):
                index = self._line_comment(index)
            elif char == "#" and not source.startswith("#[", index):
                index = self._line_comment(index)
            elif source.startswith("/*", index):
                index = self._block_comment(index)
            else:
                index += 1
        return "".join(self.chars), self.literals

    # -- apagar ------------------------------------------------------------

    def _blank(self, start: int, end: int) -> None:
        for index in range(max(start, 0), min(end, self.size)):
            self.chars[index] = " "

    # -- as quatro sintaxes de string --------------------------------------

    def _single_quoted(self, index: int) -> int:
        """`'...'`. Os únicos escapes são `\\'` e `\\\\`; o resto é literal."""
        parts: list[str] = []
        position = index + 1
        while position < self.size:
            char = self.source[position]
            if char == "\\" and self.source[position + 1 : position + 2] in ("'", "\\"):
                parts.append(self.source[position + 1])
                position += 2
                continue
            if char == "'":
                break
            parts.append(char)
            position += 1
        return self._close(index, position, "".join(parts), interpolating=False)

    def _double_quoted(self, index: int) -> int:
        """`"..."`. Interpola, então o texto lido não vale como SQL."""
        parts: list[str] = []
        position = index + 1
        while position < self.size:
            char = self.source[position]
            if char == "\\":
                escaped = self.source[position + 1 : position + 2]
                parts.append(_DOUBLE_ESCAPES.get(escaped, escaped))
                position += 2
                continue
            if char == '"':
                break
            parts.append(char)
            position += 1
        return self._close(index, position, "".join(parts), interpolating=True)

    def _close(self, index: int, position: int, text: str, interpolating: bool) -> int:
        """Registra a string entre aspas e devolve o índice depois dela.

        `position` é a aspa que fecha, ou o fim do arquivo quando ela não existe
        — string não terminada apaga até o fim, e o corpo de `up()` deixa de
        fechar junto, que é a resposta certa para um arquivo truncado.
        """
        self._blank(index + 1, position)
        end = min(position + 1, self.size)
        self.literals.append(_Literal(index, end, text, interpolating))
        return end

    def _heredoc(self, index: int) -> int:
        """`<<<SQL`, `<<<"SQL"` ou `<<<'SQL'`, até a linha que traz o rótulo.

        O rótulo de fechamento pode vir indentado (PHP 7.3+), e a indentação
        dele sai de todas as linhas do corpo. Ele também pode ser seguido na
        mesma linha por `)` e `;`, que é como o corpus escreve — por isso o
        apagamento para no rótulo e não no fim da linha.
        """
        match = _HEREDOC_OPEN.match(self.source, index)
        if match is None:
            return index + 1  # `<<<` que não abre heredoc é só operador

        label = match.group("nowdoc") or match.group("quoted") or match.group("bare")
        interpolating = match.group("nowdoc") is None
        body = match.end()

        close = re.compile(
            rf"^([ \t]*){re.escape(label)}(?![A-Za-z0-9_])", re.MULTILINE
        ).search(self.source, body)
        if close is None:
            # O rótulo nunca aparece: arquivo truncado. Apagar até o fim faz o
            # corpo de `up()` não fechar, e a resposta vira `unknown`.
            self._blank(body, self.size)
            self.literals.append(
                _Literal(index, self.size, self.source[body:], interpolating)
            )
            return self.size

        text = self.source[body : close.start()]
        for newline in ("\r\n", "\n"):
            if text.endswith(newline):
                text = text[: -len(newline)]
                break
        indent = close.group(1)
        if indent:
            text = "\n".join(
                line[len(indent) :] if line.startswith(indent) else line.lstrip()
                for line in text.split("\n")
            )
        if interpolating:
            text = _unescape_heredoc(text)

        self._blank(body, close.start())
        end = close.end()
        self.literals.append(_Literal(index, end, text, interpolating))
        return end

    # -- comentários -------------------------------------------------------

    def _line_comment(self, index: int) -> int:
        newline = self.source.find("\n", index)
        end = self.size if newline == -1 else newline
        self._blank(index, end)
        return end

    def _block_comment(self, index: int) -> int:
        close = self.source.find("*/", index + 2)
        end = self.size if close == -1 else close + 2
        self._blank(index, end)
        return end


def _unescape_heredoc(text: str) -> str:
    """Resolve os escapes do heredoc, que são os da string com aspas duplas."""
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            escaped = text[index + 1]
            out.append(_DOUBLE_ESCAPES.get(escaped, escaped))
            index += 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _scan(source: str) -> tuple[str, list[_Literal]]:
    """O fonte com o texto apagado, e os literais que ele carrega."""
    return _Scanner(source).scan()


# ---------------------------------------------------------------------------
# O marcador do dispatch
# ---------------------------------------------------------------------------

# `extends AbstractMigration`, com ou sem o namespace escrito por extenso. É o
# contrato de runtime do Doctrine: o `DependencyFactory` só executa subclasse de
# `AbstractMigration`.
_EXTENDS_ABSTRACT_MIGRATION = re.compile(
    r"\bextends\s+(?:\\?[A-Za-z_]\w*\\)*AbstractMigration\b"
)


def declares_migration(source: str) -> bool:
    """A fonte declara, **em posição de código**, uma migração do Doctrine?

    O marcador do dispatch mora aqui, ao lado do parser, e roda sobre o texto
    apagado pelo mesmo scanner que o parser usa. Isso não é detalhe: o
    consumidor tem um **gerador de migrações** que monta o arquivo novo dentro
    de um heredoc, e esse heredoc contém, palavra por palavra,
    `use Doctrine\\Migrations\\AbstractMigration`, `extends AbstractMigration` e
    `public function up(Schema $schema): void`. Um marcador por substring leria
    o gerador como migração; o parser abriria o `up()` do template, não acharia
    `addSql` nenhum e o dispatch publicaria um `unknown` todo santo dia em que
    alguém mexesse no gerador.

    Rodar sobre o texto apagado resolve isso pela raiz e sem lista de exceção:
    dentro do heredoc não há código, e o marcador vê exatamente o que o parser
    vê. É a mesma propriedade que `declares_migration_class` dá ao Django e
    `declares_upgrade` dá ao Alembic — marcador e portão de confiança olhando o
    mesmo arquivo com os mesmos olhos.
    """
    masked, _ = _scan(source)
    return _EXTENDS_ABSTRACT_MIGRATION.search(masked) is not None


# ---------------------------------------------------------------------------
# A tabela de regras
# ---------------------------------------------------------------------------
#
# Curta pelo mesmo motivo da tabela do `typeorm.py`: quase toda severidade vem
# de `detect/sql.py`, e as linhas daqui são as quatro respostas que o parser dá
# por conta própria, todas sobre o que ele não conseguiu ler. É o artefato que a
# QQ-2162 leva ao time de dados junto com a tabela do DDL, então nenhuma
# severidade é decidida no meio do código.
#
# Cada linha é uma constante do módulo, e é a constante que o código usa: um
# nome errado vira erro de nome antes de rodar, não um `KeyError` num PR. O
# dicionário existe para a tabela ser lida de uma vez e para os testes de
# invariante alcançarem todas as linhas.


@dataclass(frozen=True)
class _Gate:
    severity: Severity
    operation: str
    reason: str


# O primeiro argumento não é literal nenhum: variável, concatenação, chamada de
# função, constante de classe. A frase é, palavra por palavra, a que `alembic.py`
# dá para `op.execute(sql)` e `typeorm.py` para o template concatenado — é a
# mesma situação nas três stacks.
_DYNAMIC_SQL = _Gate(
    Severity.UNKNOWN,
    "addSql",
    "SQL montado dinamicamente — o classificador só lê SQL literal, " + MANUAL,
)

# O primeiro argumento é uma string, mas na sintaxe que interpola: `"..."` ou
# `<<<SQL`. Não é o mesmo caso do de cima e a frase não pode dizer que é — o
# autor escolheu a forma que aceita interpolação, e o que roda pode não ser o
# que está escrito.
# A frase não escreve as duas sintaxes literais com as aspas que elas têm, e não
# é preguiça: `tests/test_pii.py` exige que nenhuma razão carregue aspa crua,
# porque aspa numa razão é como os três vazamentos desta entrega apareceram. A
# crase é o único delimitador que uma razão usa.
_INTERPOLATING_SQL = _Gate(
    Severity.UNKNOWN,
    "addSql",
    "SQL em string que interpola — o classificador só lê as duas formas literais "
    "do PHP, a de aspa simples e o nowdoc, " + MANUAL,
)

_EMPTY_SQL = _Gate(
    Severity.NONE,
    "addSql",
    "Chamada `addSql()` sem SQL a executar — não altera schema.",
)

# Uma chamada de `up()` que não é `addSql` nem leitura: o schema builder
# (`$schema->dropTable()`), a conexão (`->executeStatement()`). O `operation` do
# finding é o nome do método, e não o desta linha — é ele que diz ao time o que
# olhar.
_UNREAD_CALL = _Gate(
    Severity.UNKNOWN,
    "chamada",
    "Chamada fora de `addSql()` no `up()` — o classificador não lê o que ela faz no "
    "banco, " + MANUAL,
)

_AMBIGUOUS_UP = _Gate(
    Severity.UNKNOWN,
    "up",
    "O arquivo define `up()` mais de uma vez e o classificador não sabe qual roda — "
    + MANUAL,
)

_UNTERMINATED_UP = _Gate(
    Severity.UNKNOWN,
    "up",
    "O corpo de `up()` não fecha e o classificador não delimitou os statements — "
    + MANUAL,
)

_GATES: dict[str, _Gate] = {
    "dynamic_sql": _DYNAMIC_SQL,
    "interpolating_sql": _INTERPOLATING_SQL,
    "empty_sql": _EMPTY_SQL,
    "unread_call": _UNREAD_CALL,
    "ambiguous_up": _AMBIGUOUS_UP,
    "unterminated_up": _UNTERMINATED_UP,
}


def _finding(gate: _Gate) -> Finding:
    return Finding(gate.severity, gate.operation, gate.reason)


# ---------------------------------------------------------------------------
# Achar o corpo de up()
# ---------------------------------------------------------------------------

# A definição de um método em PHP começa sempre por `function`, então o nome
# sozinho não basta e não atrapalha: `$this->up()`, `'up'` e a chave `up` de um
# array não trazem a palavra antes. O `&` opcional é o retorno por referência.
#
# `\b` no fim rejeita `upgrade` — e ele é cinto e suspensório, porque quem
# rejeitaria de todo jeito é o primeiro caractere que `_open_brace` lê depois do
# nome, que precisa ser `(`.
_UP = re.compile(r"\bfunction\s+&?\s*up\b")

# Entre o fim da lista de parâmetros e a chave do corpo só pode haver a anotação
# de tipo de retorno. `;`, `{` e `}` ali significam que não era uma definição com
# corpo — método abstrato, declaração de interface.
_RETURN_TYPE = re.compile(r"\s*(?::[^;{}]*)?\s*\Z")

_SPACE = re.compile(r"\s*")


def _skip_space(masked: str, index: int) -> int:
    return _SPACE.match(masked, index).end()


def _closing_paren(masked: str, index: int) -> int | None:
    """Índice logo depois do parêntese que fecha o que abre em `index`."""
    depth = 0
    for position in range(index, len(masked)):
        if masked[position] == "(":
            depth += 1
        elif masked[position] == ")":
            depth -= 1
            if depth == 0:
                return position + 1
    return None


def _open_brace(masked: str, index: int) -> int | _Gate | None:
    """A chave que abre o corpo da definição de `up` que termina em `index`.

    Três respostas: a chave, a linha da tabela que diz por que não dá para
    achá-la, ou `None` para "isto não é uma definição com corpo".
    """
    start = _skip_space(masked, index)
    if masked[start : start + 1] != "(":
        return None

    after = _closing_paren(masked, start)
    if after is None:
        # O arquivo acaba dentro da lista de parâmetros. É a mesma coisa que um
        # corpo que não fecha — arquivo truncado — e tem que dar a mesma
        # resposta, não uma lista vazia que se lê como "não é migração".
        return _UNTERMINATED_UP

    brace = masked.find("{", after)
    if brace == -1 or not _RETURN_TYPE.fullmatch(masked[after:brace]):
        return None
    return brace


def _matching_brace(masked: str, brace: int) -> int | None:
    """Índice da chave que fecha a que abre em `brace`."""
    depth = 0
    for position in range(brace, len(masked)):
        if masked[position] == "{":
            depth += 1
        elif masked[position] == "}":
            depth -= 1
            if depth == 0:
                return position
    return None


def _up_body(masked: str) -> tuple[tuple[int, int] | None, _Gate | None]:
    """O intervalo do corpo de `up()`, ou a razão de não dar para lê-lo.

    Três respostas, e a diferença entre elas é o ponto do portão:

    - `(None, None)` — o arquivo não define `up`. Não é uma migração;
    - `((início, fim), None)` — o corpo, sem as chaves;
    - `(None, portão)` — o arquivo define `up` de um jeito que o classificador
      não resolve, e a resposta honesta é `unknown`.
    """
    braces: list[int] = []
    for match in _UP.finditer(masked):
        found = _open_brace(masked, match.end())
        if isinstance(found, _Gate):
            return None, found
        if found is not None:
            braces.append(found)
    if not braces:
        return None, None
    if len(braces) > 1:
        return None, _AMBIGUOUS_UP
    close = _matching_brace(masked, braces[0])
    if close is None:
        return None, _UNTERMINATED_UP
    return (braces[0] + 1, close), None


# ---------------------------------------------------------------------------
# Statements do corpo
# ---------------------------------------------------------------------------

# `->metodo(`. O `->` é o que separa a chamada da definição de um método com o
# mesmo nome, e é como o Doctrine escreve: `$this->addSql(...)`.
_METHOD_CALL = re.compile(r"->\s*(?P<name>[A-Za-z_]\w*)\s*\(")

# Nome de método em PHP não diferencia caixa: `addSQL` é o mesmo `addSql`.
_ADD_SQL = "addsql"

# Chamadas de `up()` que não mexem no banco e não viram finding. As do
# `AbstractMigration` que só falam ou interrompem, e as de leitura pelo prefixo
# do nome — `$schema->hasTable()` de guarda, a plataforma no `abortIf` que o
# Doctrine gera. A lista é do que se deixa passar, e não do que se denuncia: o
# método que ninguém previu sai `unknown`, que é o lado barulhento do erro.
_GUARDS = frozenset({"write", "warnif", "abortif", "skipif"})
_READER = re.compile(r"(?:get|has|is|fetch)(?=[A-Z_]|$)")


def _only_reads(name: str) -> bool:
    return name.lower() in _GUARDS or _READER.match(name) is not None


def _is_whole_argument(masked: str, literal: _Literal) -> bool:
    """O literal é o argumento inteiro, ou só o começo de uma expressão?

    `addSql('ALTER TABLE t ADD c ' . $type)` não é SQL literal: o que roda tem
    um pedaço que não está escrito ali, e esse pedaço muda a severidade. Depois
    do delimitador que fecha só pode vir a vírgula do argumento de parâmetros ou
    o parêntese que fecha a chamada.
    """
    return masked[_skip_space(masked, literal.end) :][:1] in (",", ")")


def _argument_findings(
    masked: str, literals: dict[int, _Literal], index: int
) -> list[Finding]:
    """O que o primeiro argumento de uma chamada `->addSql()` diz sobre o schema.

    `index` é o caractere logo depois do parêntese que abre a chamada. O segundo
    argumento — os parâmetros do statement — não é lido: ele carrega valor, e
    valor não entra numa razão que vai para o Slack.
    """
    start = _skip_space(masked, index)
    if masked[start : start + 1] == ")":
        return [_finding(_EMPTY_SQL)]

    literal = literals.get(start)
    if literal is None:
        return [_finding(_DYNAMIC_SQL)]
    if literal.interpolating:
        return [_finding(_INTERPOLATING_SQL)]
    if not _is_whole_argument(masked, literal):
        return [_finding(_DYNAMIC_SQL)]
    return classify_sql(literal.text) or [_finding(_EMPTY_SQL)]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def classify_migration(source: str) -> list[Finding]:
    """Classifica cada statement SQL do `up()` de uma migração do Doctrine.

    Devolve um `Finding` por statement, na ordem em que aparecem no corpo do
    método. Um arquivo sem `up` devolve lista vazia: não é uma migração que este
    módulo saiba ler. `down()` é ignorado — o que roda no deploy é o `up()`.

    Um statement que o `detect/sql.py` não reconhece sai como `unknown` e os
    outros continuam classificados; o mesmo vale para uma chamada cujo SQL não é
    um literal inteiro. Parar na primeira esconderia o `DROP COLUMN` que vem
    depois.

    A unidade é o statement e não a chamada: um `addSql` com dois statements
    separados por `;` devolve dois findings, igual ao `queryRunner.query()` do
    TypeORM. Quem chama associa os findings ao caminho do arquivo — que é onde
    fica escrito qual dos dois bancos foi mexido — e agrega a severidade.
    """
    masked, literals = _scan(source)
    body, gate = _up_body(masked)
    if gate is not None:
        return [_finding(gate)]
    if body is None:
        return []

    start, end = body
    by_start = {literal.start: literal for literal in literals}
    findings: list[Finding] = []
    inside_add_sql = start
    for call in _METHOD_CALL.finditer(masked, start, end):
        name = call.group("name")
        if call.start() < inside_add_sql:
            # Chamada dentro do argumento de um `addSql`, que já saiu dinâmico.
            continue
        if name.lower() == _ADD_SQL:
            findings.extend(_argument_findings(masked, by_start, call.end()))
            inside_add_sql = _closing_paren(masked, call.end() - 1) or end
        elif not _only_reads(name):
            findings.append(Finding(_UNREAD_CALL.severity, name, _UNREAD_CALL.reason))
    return findings
