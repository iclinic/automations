"""Classificador de migrações do TypeORM.

Lê os statements SQL do corpo do método `up()` e delega cada um para
`detect/sql.py`. É o mais fino dos três detectores porque não tem tabela de
severidade própria: os 34 arquivos do consumidor TypeORM não usam o
schema builder do TypeORM — são 133 chamadas `queryRunner.query()` com DDL
literal dentro de uma template string, e quem sabe o que um DDL faz é o
`detect/sql.py`, o mesmo módulo que atende o `RunSQL` do Django e o
`op.execute()` do Alembic. O mesmo DDL tem que sair com a mesma cor nas três
stacks.

O arquivo é TypeScript, então não há `ast` para chamar. O que existe aqui é um
scanner de literais e comentários — não um parser de TypeScript, e o aceite é
explícito em não pedir um. Ele faz três coisas:

1. apaga o conteúdo de string, template e comentário, preservando os índices,
   para que chave e parêntese dentro de texto não contem;
2. guarda onde começa e termina cada template literal de primeiro nível;
3. com o texto apagado, casa a chave que abre o corpo de `up()` com a que o
   fecha.

Dentro desse intervalo, a unidade é a chamada `.query()`: cada uma é lida, e o
SQL sai dela quando o primeiro argumento é uma template string inteira. Todo o
resto — interpolação, concatenação, variável, string entre aspas — é `unknown`.
`down()` fica de fora por construção: o que roda no deploy é o `up()`, e
classificar o rollback pintaria de vermelho toda migração que sabe se desfazer.

Quatro decisões não são óbvias:

- **Só o argumento de uma chamada `.query()` é lido como SQL.** Template solto
  no corpo de `up()` é texto qualquer — uma mensagem de log, o nome de um
  arquivo — e mandá-lo para o classificador de DDL fazia duas coisas erradas:
  punha texto livre da migração dentro da razão que vai para o Slack, e subia
  para `unknown` um arquivo que tinha sido lido inteiro só porque havia um
  `console.log` ali. É a mesma garantia que o `alembic.py` e o `django.py` têm
  de graça, porque lá o SQL só chega pelo argumento de `execute`/`RunSQL`.
- **O argumento tem que ser a template string inteira.** ``query(`ALTER TABLE t
  ADD c varchar` + notNull)`` não é SQL literal: o que roda tem um pedaço que
  não está escrito ali, e esse pedaço muda a severidade. Depois da crase que
  fecha só pode vir `,` ou `)`.
- **Template com `${}` é `unknown`, não um palpite.** O SQL que roda não está
  escrito no arquivo. Classificar a parte legível de ``DROP TABLE ${t}`` seria
  afirmar o que a interpolação pode desmentir. É a mesma resposta — e a mesma
  frase — que o Alembic dá para `op.execute(sql)`.
- **A template string é a unidade, não a chamada.** Um `queryRunner.query()`
  com dois statements devolve dois findings, ao contrário do `op.execute()` do
  Alembic, que devolve o pior: lá a unidade é a operação que o ORM lista, aqui o
  arquivo já é uma lista de statements SQL, e agregar dentro da chamada
  esconderia um `DROP COLUMN` atrás de um `CREATE TABLE` da mesma chamada. Quem
  agrega por arquivo é quem chama — e a contagem de statements daqui não é
  comparável com a contagem de operações das outras duas stacks.

Controle de fluxo não é avaliado: um `query()` dentro de `if` ou de `for` é
classificado como qualquer outro. O DDL pode rodar, e reportá-lo é o lado
conservador de errar; nome de tabela que varia no laço chega como `${}` e cai
no caso de cima.

Onde o scanner tem teto, e o teto é conhecido:

- **SQL que não passa pelo argumento de uma chamada `.query()`** — `await
  this.run(sql)`, ou `await this.run(`...`)` com o DDL escrito ali — não é
  lido, e o arquivo sai sem finding nenhum. É o teto de ler texto em vez de
  resolver nomes; nenhum arquivo do corpus faz isso.
- **Expressão regular literal do JavaScript não é reconhecida.** `/` só começa
  algo quando vem seguido de `/` ou `*`. Uma regex contendo crase ou chave
  desemparelhada confunde a varredura. Nenhum arquivo do corpus tem uma, e
  reconhecê-las exige saber se `/` é divisão ou início de literal — que é a
  pergunta que só um parser de TypeScript responde.
- **Tipo de retorno escrito como objeto** (`up(): { ok: boolean } {`) faria a
  primeira chave passar por corpo. O corpus escreve `Promise<any>` e
  `Promise<void>`.
- **Parâmetro de tipo** (`async up<T>(qr): Promise<void>`) não é reconhecido: o
  que vem depois do nome é `<`, e não a lista de parâmetros. O arquivo sai sem
  finding nenhum.
- **Chave `up` de um objeto literal seguida de qualquer função de seta**
  (`const hooks = { up: X, run: (x) => {} }`) passa por definição. Com um
  `up()` de verdade no mesmo arquivo, o resultado é um `ambiguous_up` que não
  existe; sozinha, o corpo lido é o da seta errada.
- **`up` definido mais de uma vez** no arquivo devolve `unknown`: sem escopo
  não dá para dizer qual deles o TypeORM chama.
- **Escapes de unicode** (`\\u0041`) dentro do template não são decodificados.

Um arquivo sem `up()` devolve lista vazia — não é uma migração que este módulo
saiba ler, e dizer isso é diferente de dizer que nada acontece.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .severity import MANUAL, Finding, Severity
from .sql import classify_sql

__all__ = ["classify_migration"]


# ---------------------------------------------------------------------------
# Varredura léxica: strings, templates e comentários
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Template:
    """Um template literal de primeiro nível do arquivo.

    `start` é a crase de abertura e `end` é o índice logo depois da crase que
    fecha. `text` é o conteúdo com os escapes resolvidos; quando `interpolated`,
    ele não vale nada — é o pedaço que estava escrito, não o SQL que roda.
    """

    start: int
    end: int
    text: str
    interpolated: bool


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


class _Scanner:
    """Apaga o texto que não é código e guarda os templates de primeiro nível.

    O texto apagado tem o mesmo comprimento do original — é o que permite achar
    a chave no texto apagado e ler o SQL no original com o mesmo índice, do
    mesmo jeito que `detect/sql.py` faz com a máscara dele.

    Os templates aninhados dentro de uma interpolação não entram na lista: o
    template que os contém já é `unknown`, e reportar ```schedule``` de
    ``ALTER TABLE ${`schedule`}`` seria classificar um pedaço em vez do
    statement.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.size = len(source)
        self.chars = list(source)
        self.templates: list[_Template] = []

    def scan(self) -> tuple[str, list[_Template]]:
        self._code(0, 0, nested=False)
        return "".join(self.chars), self.templates

    # -- apagar ------------------------------------------------------------

    def _blank(self, start: int, end: int) -> None:
        for index in range(max(start, 0), min(end, self.size)):
            self.chars[index] = " "

    # -- percorrer ---------------------------------------------------------

    def _code(self, index: int, depth: int, nested: bool) -> int:
        """Percorre código até o fim do fonte, ou até fechar a chave aberta.

        Com `depth` maior que zero, para logo depois da chave que zera a
        contagem — é assim que a interpolação `${...}` acha o próprio fim. É
        também o que faz `nested` valer: o código dentro de uma interpolação
        está dentro de um template, e os templates que ele contém não são de
        primeiro nível.
        """
        source = self.source
        while index < self.size:
            char = source[index]
            if depth and char == "{":
                depth += 1
                index += 1
            elif depth and char == "}":
                depth -= 1
                index += 1
                if depth == 0:
                    return index
            elif char in "\"'":
                index = self._quoted(index)
            elif char == "`":
                index = self._template(index, nested=nested)
            elif source.startswith("//", index):
                index = self._line_comment(index)
            elif source.startswith("/*", index):
                index = self._block_comment(index)
            else:
                index += 1
        return index

    def _quoted(self, index: int) -> int:
        """Uma string entre aspas. Uma quebra de linha a encerra, como no JS."""
        quote = self.source[index]
        end = index + 1
        while end < self.size:
            char = self.source[end]
            if char == "\\":
                end += 2
                continue
            if char == quote or char == "\n":
                break
            end += 1
        self._blank(index + 1, end)
        return min(end + 1, self.size)

    def _template(self, index: int, nested: bool) -> int:
        """Um template literal, com as interpolações que ele carregar.

        As chaves do `${...}` ficam visíveis no texto apagado de propósito: elas
        são pares, então o casamento de chaves do corpo de `up()` continua
        certo, e o código dentro da interpolação é código de verdade.
        """
        start = index
        end = index + 1
        parts: list[str] = []
        interpolated = False
        while end < self.size:
            char = self.source[end]
            if char == "\\":
                escaped = self.source[end + 1 : end + 2]
                parts.append(_ESCAPES.get(escaped, escaped))
                self._blank(end, end + 2)
                end += 2
                continue
            if char == "`":
                end += 1
                break
            if char == "$" and self.source.startswith("${", end):
                interpolated = True
                end = self._code(end + 2, 1, nested=True)
                continue
            parts.append(char)
            self._blank(end, end + 1)
            end += 1
        if not nested:
            self.templates.append(_Template(start, end, "".join(parts), interpolated))
        return end

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


def _scan(source: str) -> tuple[str, list[_Template]]:
    """O fonte com o texto apagado, e os templates literais de primeiro nível."""
    return _Scanner(source).scan()


# ---------------------------------------------------------------------------
# A tabela de regras
# ---------------------------------------------------------------------------
#
# A tabela de severidade deste módulo é curta porque quase toda severidade vem
# de `detect/sql.py`: as linhas daqui são as cinco respostas que o parser dá por
# conta própria, todas sobre o que ele não conseguiu ler. É o artefato que a
# QQ-2162 leva ao time de dados junto com a tabela do DDL, então cada resposta
# do módulo que não vem do SQL tem que estar aqui — nenhuma severidade é
# decidida no meio do código.
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


# `dynamic_sql` é o SQL montado em tempo de execução: interpolação,
# concatenação, `.replace(...)`, variável. A frase é, palavra por palavra, a que
# `alembic.py` dá para `op.execute(sql)` — é a mesma situação nas duas stacks.
_DYNAMIC_SQL = _Gate(
    Severity.UNKNOWN,
    "queryRunner.query",
    "SQL montado dinamicamente — o classificador só lê SQL literal, " + MANUAL,
)

# `unread_sql` é o SQL que está escrito ali, inteiro, mas fora de uma template
# string. Não é dinâmico e a frase não pode dizer que é: é uma forma que este
# parser não lê, e o corpus não tem nenhuma.
_UNREAD_SQL = _Gate(
    Severity.UNKNOWN,
    "queryRunner.query",
    "SQL escrito fora de uma template string — o classificador só lê o SQL que está "
    "entre crases no corpo de `up()`, " + MANUAL,
)

_EMPTY_SQL = _Gate(
    Severity.NONE,
    "queryRunner.query",
    "Chamada `queryRunner.query()` sem SQL a executar — não altera schema.",
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
    "unread_sql": _UNREAD_SQL,
    "empty_sql": _EMPTY_SQL,
    "ambiguous_up": _AMBIGUOUS_UP,
    "unterminated_up": _UNTERMINATED_UP,
}


def _finding(gate: _Gate) -> Finding:
    return Finding(gate.severity, gate.operation, gate.reason)


# ---------------------------------------------------------------------------
# Achar o corpo de up()
# ---------------------------------------------------------------------------

# `up` como palavra inteira, sem `.` nem letra antes: `cleanup`, `this.up` e
# `?.up` não são a definição que se procura.
#
# O `\b` do fim é cinto e suspensório: quem rejeita `upgrade` é o primeiro
# caractere que `_open_brace` lê depois do nome, que nunca vai ser `(`, `?`, `=`
# ou `:` se ainda houver identificador ali. Não dá para escrever um teste que
# distinga a presença dele — está aqui para a regex dizer o que quer dizer, e
# para que afrouxar `_open_brace` um dia não faça `upgrade = async () => {}`
# virar `up`.
_UP = re.compile(r"(?<![\w$.])up\b")

# Entre o fim da lista de parâmetros e a chave do corpo só pode haver a anotação
# de tipo de retorno. `;`, `{` e `}` ali significam que não era uma definição.
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

    Três respostas: a chave, a linha da tabela que diz por que não dá para achá-la,
    ou `None` para "isto não é uma definição".

    Duas formas de definição contam: o método (`async up(qr): Promise<void> {`),
    que é o que o corpus escreve, e a propriedade de função de seta
    (`up = async (qr) => {`). Qualquer outra coisa — uma chamada `up(qr);`, uma
    declaração de tipo sem corpo, o nome numa expressão — devolve `None`, e é o
    que impede o corpo de `down()` de ser lido no lugar.
    """
    start = _skip_space(masked, index)
    if masked[start : start + 1] == "?":  # `up?(): ... {`
        start = _skip_space(masked, start + 1)

    if masked[start : start + 1] == "(":
        after = _closing_paren(masked, start)
        if after is None:
            # O arquivo acaba dentro da lista de parâmetros. É a mesma coisa que
            # um corpo que não fecha — arquivo truncado — e tem que dar a mesma
            # resposta, não uma lista vazia que se lê como "não é migração".
            return _UNTERMINATED_UP
    else:
        after = _arrow(masked, start)
        if after is None:
            return None

    brace = masked.find("{", after)
    if brace == -1 or not _RETURN_TYPE.fullmatch(masked[after:brace]):
        return None
    return brace


def _arrow(masked: str, start: int) -> int | None:
    """Índice logo depois do `=>` de uma propriedade de função de seta."""
    brace = masked.find("{", start)
    if brace == -1:
        return None
    gap = masked[start:brace]
    arrow = gap.find("=>")
    if arrow == -1 or gap[:1] not in ("=", ":"):
        return None
    head = gap[:arrow]
    if ";" in head or head.count("(") != head.count(")"):
        return None
    return start + arrow + 2


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

_QUERY_CALL = re.compile(r"\.\s*query\s*\(")


def _is_whole_argument(masked: str, template: _Template) -> bool:
    """O template é o argumento inteiro, ou só o começo de uma expressão?

    ``query(`ALTER TABLE t ADD c varchar` + notNull)`` e
    ``query(`...`.replace(a, b))`` não são SQL literal: o que roda tem um pedaço
    que não está escrito ali, e esse pedaço muda a severidade. Depois da crase
    que fecha só pode vir a vírgula do segundo argumento ou o parêntese que
    fecha a chamada.
    """
    return masked[_skip_space(masked, template.end) :][:1] in (",", ")")


def _argument_findings(
    masked: str, templates: dict[int, _Template], index: int
) -> list[Finding]:
    """O que o primeiro argumento de uma chamada `.query()` diz sobre o schema.

    `index` é o caractere logo depois do parêntese que abre a chamada.
    """
    start = _skip_space(masked, index)
    first = masked[start : start + 1]
    if first == ")":
        return [_finding(_EMPTY_SQL)]
    if first in ('"', "'"):
        return [_finding(_UNREAD_SQL)]

    template = templates.get(start)
    if template is None or template.interpolated or not _is_whole_argument(masked, template):
        return [_finding(_DYNAMIC_SQL)]
    return classify_sql(template.text) or [_finding(_EMPTY_SQL)]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def classify_migration(source: str) -> list[Finding]:
    """Classifica cada statement SQL do `up()` de uma migração do TypeORM.

    Devolve um `Finding` por statement, na ordem em que aparecem no corpo do
    método. Um arquivo sem `up` devolve lista vazia: não é uma migração que este
    módulo saiba ler. `down()` é ignorado — o que roda no deploy é o `up()`.

    Um statement que o `detect/sql.py` não reconhece sai como `unknown` e os
    outros continuam classificados; o mesmo vale para uma chamada cujo SQL não é
    uma template string inteira. Parar na primeira esconderia o `DROP COLUMN`
    que vem depois.

    Quem chama associa os findings ao caminho do arquivo e agrega a severidade;
    o `Finding` descreve um statement, não um arquivo.
    """
    masked, templates = _scan(source)
    body, gate = _up_body(masked)
    if gate is not None:
        return [_finding(gate)]
    if body is None:
        return []

    start, end = body
    by_start = {template.start: template for template in templates}
    return [
        finding
        for call in _QUERY_CALL.finditer(masked, start, end)
        for finding in _argument_findings(masked, by_start, call.end())
    ]
