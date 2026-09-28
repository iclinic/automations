"""Classificador de migrações do Alembic.

Lê as chamadas `op.*` do corpo da função `upgrade()` com o módulo `ast`: sem
importar Alembic nem SQLAlchemy, sem abrir conexão, sem executar nada do
arquivo. O que o classificador sabe é o que está escrito no fonte.

Isso põe um teto no que dá para afirmar, e o teto é a regra dos três módulos de
detecção: valor que não é literal — SQL montado em runtime, operação chamada a
partir de uma variável, corpo de `upgrade()` que não é uma lista de chamadas —
devolve `unknown`. O modo de falha que este classificador substitui é o de um
provedor que respondia `controlled` com confiança 0.0 para tudo enquanto o job
seguia verde; um `unknown` visível no Slack é o oposto disso.

Quatro decisões não são óbvias:

- **`downgrade()` é ignorado.** Todo arquivo do corpus tem `drop_table` e
  `drop_column` lá dentro, e classificá-los pintaria de vermelho toda migração
  que sabe se desfazer. O que roda no deploy é `upgrade()`.
- **`op.f()` não é operação.** São 42 ocorrências no corpus, todas como nome de
  índice dentro dos argumentos de um `create_index`/`drop_index`. Só chamada em
  posição de statement conta como operação, então o helper aninhado nunca vira
  finding — e a linha `f` da tabela existe para o caso, absurdo mas possível, de
  ele aparecer sozinho.
- **`op.execute()` não classifica SQL:** delega para `detect/sql.py`, o mesmo
  módulo que atende o `RunSQL` do Django e o `queryRunner.query()` do TypeORM.
  O mesmo DDL tem que sair com a mesma cor nas três stacks.
- **`alter_column` só compara quando o arquivo carrega o estado anterior.** O
  Alembic, como o Django, escreve o estado final; sem `existing_type` não há com
  o que comparar e a resposta é `unknown`. Com ele — e com `existing_nullable`,
  que o autogenerate escreve junto — dá para dizer o que mudou.

O portão de confiança do corpo de `upgrade()` mora aqui e não em
`detect/_reading.py`: ele é de uma stack só. O que veio de lá são os leitores de
valor literal, que são de todas.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from typing import Callable

from ._reading import call_name, has_unpacked_arguments, text
from .severity import (
    DUPLICATE,
    MANUAL,
    Finding,
    Severity,
    ref,
    severity_rank,
    worst,
)
from .sql import classify_sql

__all__ = ["classify_migration"]


# ---------------------------------------------------------------------------
# Leitura da AST
# ---------------------------------------------------------------------------


def _is_true(node: ast.expr | None) -> bool:
    """O nó é o literal `True`? `1` e um nome qualquer não são."""
    return isinstance(node, ast.Constant) and node.value is True


def _is_none(node: ast.expr | None) -> bool:
    """O nó é o literal `None`?"""
    return isinstance(node, ast.Constant) and node.value is None


def _is_false(node: ast.expr | None) -> bool:
    """O nó é o literal `False`? `0` e uma lista vazia não são."""
    return isinstance(node, ast.Constant) and node.value is False


def _is_none(node: ast.expr | None) -> bool:
    """O nó é o literal `None`?

    O Alembic usa `None` e `False` como sentinela de "não especificado" em vários
    parâmetros de `alter_column`. Escritos à mão, eles chegam como um nó da AST
    igual a qualquer outro — e "existe um nó aqui" não é a mesma pergunta que
    "foi passado um valor".
    """
    return isinstance(node, ast.Constant) and node.value is None


_DEFAULT_ALIAS = "op"


def _op_aliases(tree: ast.Module) -> frozenset[str]:
    """Os nomes ligados ao `op` do Alembic neste arquivo.

    O corpus inteiro escreve `from alembic import op`, mas o nome é só uma
    convenção do template. Sem nenhum import reconhecível fica valendo `op`:
    quem chegou até aqui já sabe que o arquivo é uma versão do Alembic, e o
    contrário — não reconhecer nenhuma operação — devolveria um arquivo inteiro
    sem findings, que a jusante lê como "nada a reportar".
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "alembic":
            for alias in node.names:
                if alias.name == "op":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "alembic.op" and alias.asname:
                    aliases.add(alias.asname)
    return frozenset(aliases) or frozenset({_DEFAULT_ALIAS})


def _op_method(node: ast.expr, aliases: frozenset[str]) -> str | None:
    """O método do `op` chamado: `op.drop_table(...)` vira `drop_table`.

    `alembic.op.drop_table(...)`, `OPS["drop"](...)` e a chamada de qualquer
    outro objeto devolvem `None` — são chamadas que este módulo não afirma
    conhecer.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in aliases
    ):
        return func.attr
    return None


# ---------------------------------------------------------------------------
# O portão de confiança: dá para acreditar no corpo deste `upgrade()`?
# ---------------------------------------------------------------------------


def _is_upgrade(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade"


def _has_nested_upgrade(tree: ast.Module) -> bool:
    """Existe uma `def upgrade` fora do corpo do módulo?

    O Alembic chama `module.upgrade()`, então uma definição dentro de um `if` ou
    de um `try` no topo do arquivo vale de verdade — mas qual delas vale depende
    de algo que o classificador não executa.
    """
    return any(
        _is_upgrade(node)
        for statement in tree.body
        if not _is_upgrade(statement)
        for node in ast.walk(statement)
    )


_Upgrade = ast.FunctionDef | ast.AsyncFunctionDef


def _upgrade_of(tree: ast.Module) -> tuple[_Upgrade | None, str | None]:
    """A função `upgrade()` do módulo, ou a razão de não dar para escolhê-la.

    Três respostas, e a diferença entre elas é o ponto do portão:

    - `(None, None)` — o arquivo não tem `upgrade()`. Não é uma migração;
    - `(função, None)` — a última `def upgrade` do módulo, que é a que o Alembic
      carrega;
    - `(None, razão)` — o arquivo define `upgrade` de um jeito que o
      classificador não resolve, e a resposta honesta é `unknown`.
    """
    if _has_nested_upgrade(tree):
        return None, "A função `upgrade()` não é definida no corpo do módulo — " + MANUAL
    functions = [node for node in tree.body if _is_upgrade(node)]
    if not functions:
        return None, None
    return functions[-1], None


# Statements que não são uma chamada de operação. A severidade é sempre
# `unknown`; o que muda é a frase, porque "bloco `with`" e "operação dentro de
# um laço" são coisas diferentes para quem vai revisar à mão.
_UNREADABLE_STATEMENTS: tuple[tuple[tuple[type[ast.AST], ...], str], ...] = (
    (
        (ast.With, ast.AsyncWith),
        "Bloco `with` no corpo de `upgrade()` — o classificador não lê as operações "
        "de um `batch_alter_table` — " + MANUAL,
    ),
    (
        (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.Match),
        "Operação dentro de condicional ou laço no corpo de `upgrade()` — " + MANUAL,
    ),
)

_UNREADABLE_STATEMENT = (
    "Statement no corpo de `upgrade()` que não é uma chamada de operação — " + MANUAL
)


def declares_upgrade(tree: ast.Module) -> bool:
    """O módulo declara um `upgrade()` que este parser saberia julgar?

    A união exata do que `_upgrade_of` enxerga: uma `def upgrade` no corpo do
    módulo, ou uma dentro de um `if`/`try` no topo. É o critério que o dispatch
    de `detect/__init__.py` usa para escolher este parser — igual ao portão
    daqui, para que nenhum arquivo seja encaminhado para cá e volte vazio.
    """
    return _has_nested_upgrade(tree) or any(_is_upgrade(node) for node in tree.body)


def _unreadable_reason(statement: ast.stmt) -> str:
    for types, reason in _UNREADABLE_STATEMENTS:
        if isinstance(statement, types):
            return reason
    return _UNREADABLE_STATEMENT


# ---------------------------------------------------------------------------
# A tabela de regras
# ---------------------------------------------------------------------------
#
# Daqui para baixo é a tabela de severidade: que operação do Alembic vale
# quanto, e por quê. É o artefato que a QQ-2162 leva ao time de dados para
# validar, então é para ser lida e editada por quem não conhece o resto do
# módulo. Uma linha por operação; a mecânica de ler a AST ficou toda acima.
#
# `reason` é template: `{table}` é a tabela que a operação mexe e `{target}` é o
# objeto dentro dela — coluna, índice, constraint. `table` e `target` dizem em
# qual parâmetro da operação cada um mora, e `signature` é a ordem posicional
# dos parâmetros no Alembic, que é o que permite ler
# `op.drop_column("docs_signature", "external_id")` sem keyword.
#
# Encurtar uma `signature` não quebra a leitura por keyword e faz a leitura por
# posição parar de enxergar o argumento em silêncio — os dois testes
# `test_the_parameters_*_are_in_the_signature`, em `tests/test_alembic.py`, são
# o que segura as duas pontas juntas.
#
# Os três leitores logo abaixo são o que transforma uma linha em `Finding`;
# vêm antes da tabela para que ela possa ser lida de cima a baixo sem saltos.


@dataclass(frozen=True)
class _Operation:
    severity: Severity
    reason: str
    signature: tuple[str, ...] = ()
    table: str = ""
    target: str = ""


def _argument(operation: _Operation, call: ast.Call, parameter: str) -> ast.expr | None:
    """O valor de um parâmetro, dado por keyword ou por posição."""
    for keyword in call.keywords:
        if keyword.arg == parameter:
            return keyword.value
    if parameter in operation.signature:
        index = operation.signature.index(parameter)
        if index < len(call.args):
            argument = call.args[index]
            if not isinstance(argument, ast.Starred):
                return argument
    return None


def _name_of(operation: _Operation, call: ast.Call, parameter: str) -> str:
    """O nome do objeto que mora num parâmetro.

    Duas formas, e as duas aparecem no corpus: o nome é uma string
    (`op.drop_column("docs_signature", "external_id")`) ou é o primeiro argumento
    da chamada que define o objeto — `sa.Column("external_id", ...)` no
    `add_column`, `op.f("ix_docs_ai_feedback_rating")` no `create_index`.
    """
    if not parameter:
        return ""
    node = _argument(operation, call, parameter)
    name = text(node)
    if name is not None:
        return name
    if isinstance(node, ast.Call) and node.args:
        return text(node.args[0]) or ""
    return ""


def _finding(label: str, operation: _Operation, call: ast.Call) -> Finding:
    names = {
        "table": _name_of(operation, call, operation.table),
        "target": _name_of(operation, call, operation.target),
    }
    return Finding(
        operation.severity,
        label,
        operation.reason.format(**{key: ref(value) for key, value in names.items()}),
    )


_OPERATIONS: dict[str, _Operation] = {
    # --- tabelas ---
    "create_table": _Operation(
        Severity.SAFE,
        "Tabela {table} criada — tabela nova, ninguém lê ainda.",
        signature=("table_name",),
        table="table_name",
    ),
    "drop_table": _Operation(
        Severity.BREAKING,
        "Tabela {table} removida — quem lê essa tabela quebra.",
        signature=("table_name",),
        table="table_name",
    ),
    "rename_table": _Operation(
        Severity.BREAKING,
        "Tabela {table} renomeada — quem lê o nome antigo quebra.",
        signature=("old_table_name", "new_table_name", "schema"),
        table="old_table_name",
    ),
    # --- colunas ---
    "add_column": _Operation(
        # Refinado por `_refine_add_column`: a nulidade e o default moram dentro
        # do `sa.Column(...)`, não nos argumentos do `add_column`.
        Severity.SAFE,
        "Coluna {target} adicionada em {table}.",
        signature=("table_name", "column", "schema"),
        table="table_name",
        target="column",
    ),
    "drop_column": _Operation(
        Severity.BREAKING,
        "Coluna {target} removida de {table} — quem lê essa coluna quebra.",
        signature=("table_name", "column_name", "schema"),
        table="table_name",
        target="column_name",
    ),
    "alter_column": _Operation(
        # Refinado por `_refine_alter_column`: com `existing_type` no arquivo, a
        # comparação diz o que mudou. A regra desta linha é o que sobra sem ele —
        # a definição nova sozinha não diz nada.
        Severity.UNKNOWN,
        "Coluna {target} de {table} alterada — "
        "o classificador não conhece a definição anterior, " + MANUAL,
        signature=(
            "table_name",
            "column_name",
            "nullable",
            "comment",
            "server_default",
            "new_column_name",
            "type_",
            "existing_type",
            "existing_server_default",
            "existing_nullable",
        ),
        table="table_name",
        target="column_name",
    ),
    # --- índices e constraints ---
    "create_index": _Operation(
        # Refinado por `_refine_create_index`: índice único tem o mesmo risco de
        # gravação que uma constraint UNIQUE.
        Severity.SAFE,
        "Índice {target} criado em {table}.",
        signature=("index_name", "table_name", "columns", "schema", "unique"),
        table="table_name",
        target="index_name",
    ),
    "drop_index": _Operation(
        Severity.CONTROLLED,
        "Índice {target} removido de {table} — consulta que dependia dele fica mais lenta.",
        signature=("index_name", "table_name", "schema"),
        table="table_name",
        target="index_name",
    ),
    "create_unique_constraint": _Operation(
        Severity.BREAKING,
        "Constraint UNIQUE {target} adicionada em {table} — " + DUPLICATE,
        signature=("constraint_name", "table_name", "columns", "schema"),
        table="table_name",
        target="constraint_name",
    ),
    "create_primary_key": _Operation(
        Severity.BREAKING,
        "Chave primária {target} criada em {table} — " + DUPLICATE,
        signature=("constraint_name", "table_name", "columns", "schema"),
        table="table_name",
        target="constraint_name",
    ),
    "drop_constraint": _Operation(
        Severity.CONTROLLED,
        "Constraint {target} removida de {table}.",
        signature=("constraint_name", "table_name", "type_", "schema"),
        table="table_name",
        target="constraint_name",
    ),
    # --- SQL arbitrário ---
    "execute": _Operation(
        # Expandido por `_expand_execute`, que delega para `detect/sql.py`. A
        # regra desta linha é o que sobra quando o SQL não é literal.
        Severity.UNKNOWN,
        "SQL montado dinamicamente — o classificador só lê SQL literal, " + MANUAL,
        signature=("sqltext", "execution_options"),
    ),
    # --- helper que não é operação ---
    "f": _Operation(
        # `op.f()` só marca um nome de índice como já resolvido. Aninhado nos
        # argumentos de uma operação nem chega aqui; esta linha é para o caso de
        # ele aparecer sozinho, para não virar um `unknown` que não é dúvida
        # nenhuma.
        Severity.NONE,
        "Helper `op.f()` de nome de índice — não altera schema.",
    ),
}


# --- refinamentos ----------------------------------------------------------
#
# As operações cuja severidade depende de mais que o nome. Cada uma recebe a
# linha da tabela e a chamada, e devolve a linha que a substitui — sempre via
# `replace`, para não repetir a assinatura — ou `None` para manter a da tabela.


def _refine_add_column(operation: _Operation, call: ast.Call) -> _Operation | None:
    """A nulidade e o default da coluna nova moram dentro do `sa.Column(...)`.

    A regra do aceite é a mesma do `AddField` do Django: sem `nullable=True` e
    sem `server_default`, a coluna entra NOT NULL sem valor e a tabela que já tem
    linha barra a migração. `sa.Column` sozinho tem `nullable=True` como padrão,
    mas o Alembic autogenerate sempre escreve `nullable=` — a ausência é sinal de
    que o classificador está lendo uma coluna que não veio do autogenerate, e
    `controlled` é a resposta conservadora.
    """
    column = _argument(operation, call, "column")
    if (
        has_unpacked_arguments(call)
        or not isinstance(column, ast.Call)
        # `sa.Column("c", sa.Integer(), **spec)`: a nulidade e o default podem
        # estar dentro do `spec`, e um keyword ausente deixa de significar "não
        # foi passado" — que é justamente a pergunta que este refinador faz.
        or has_unpacked_arguments(column)
    ):
        return replace(
            operation,
            severity=Severity.UNKNOWN,
            reason="Coluna adicionada em {table} com uma definição que o classificador "
            "não consegue ler — " + MANUAL,
        )
    nullable = any(
        keyword.arg == "nullable" and _is_true(keyword.value) for keyword in column.keywords
    )
    has_default = any(keyword.arg == "server_default" for keyword in column.keywords)
    fills_a_value = any(
        keyword.arg == "server_default" and not _is_none(keyword.value)
        for keyword in column.keywords
    )
    unique = any(
        keyword.arg == "unique" and _is_true(keyword.value) for keyword in column.keywords
    )
    if unique and (fills_a_value or not nullable):
        # Mesma regra do `AddField` único do Django. Só `server_default`
        # preenche as linhas existentes; o `default` do SQLAlchemy vale para
        # insert novo e deixa as antigas em NULL, que não colide.
        return replace(
            operation,
            severity=Severity.BREAKING,
            reason="Coluna única {target} adicionada em {table} com o mesmo valor "
            "em toda linha existente — " + DUPLICATE,
        )
    if nullable or has_default:
        return None
    return replace(
        operation,
        severity=Severity.CONTROLLED,
        reason="Coluna {target} adicionada em {table} como NOT NULL sem default — "
        "tabela que já tem linha impede a migração.",
    )


_CREATE_INDEX_UNREADABLE_UNIQUENESS = (
    "Índice {target} criado em {table} com unicidade que o classificador "
    "não consegue ler — " + MANUAL
)


def _refine_create_index(operation: _Operation, call: ast.Call) -> _Operation | None:
    """Índice único é o mesmo risco de gravação que uma constraint UNIQUE.

    Com os argumentos desempacotados, `unique` ausente deixa de significar
    `unique=False`: o `True` pode estar dentro do valor que o classificador não
    lê, e a diferença entre os dois é a diferença entre `safe` e `breaking`.
    """
    if has_unpacked_arguments(call):
        return replace(
            operation,
            severity=Severity.UNKNOWN,
            reason=_CREATE_INDEX_UNREADABLE_UNIQUENESS,
        )
    unique = _argument(operation, call, "unique")
    if unique is None or _is_false(unique):
        return None
    if _is_true(unique):
        return replace(
            operation,
            severity=Severity.BREAKING,
            reason="Índice único {target} criado em {table} — " + DUPLICATE,
        )
    return replace(
        operation, severity=Severity.UNKNOWN, reason=_CREATE_INDEX_UNREADABLE_UNIQUENESS
    )


# As mudanças que `_refine_alter_column` sabe reconhecer, com a frase de cada
# uma. Fora da tabela `_OPERATIONS` porque são todas da mesma linha: o que muda
# entre elas é qual argumento a chamada trouxe.
_ALTER_COLUMN_RENAMED = (
    Severity.BREAKING,
    "Coluna {target} de {table} renomeada — quem lê o nome antigo quebra.",
)
_ALTER_COLUMN_RENAMED_UNKNOWN = (
    Severity.UNKNOWN,
    "Coluna {target} de {table} renomeada para um nome que o classificador não "
    "consegue ler — " + MANUAL,
)
_ALTER_COLUMN_RETYPED = (
    Severity.BREAKING,
    "Tipo da coluna {target} de {table} alterado — quem lê o tipo antigo quebra.",
)
_ALTER_COLUMN_NOT_NULL = (
    Severity.BREAKING,
    "Coluna {target} de {table} passou a NOT NULL — insert sem valor passa a falhar.",
)
_ALTER_COLUMN_NULLABLE = (
    Severity.CONTROLLED,
    "Coluna {target} de {table} passou a aceitar NULL.",
)
_ALTER_COLUMN_NOT_NULL_UNKNOWN = (
    Severity.UNKNOWN,
    "Coluna {target} de {table} declarada NOT NULL e o classificador não conhece a "
    "nulidade anterior — " + MANUAL,
)
_ALTER_COLUMN_DEFAULT = (
    Severity.CONTROLLED,
    "Default da coluna {target} de {table} alterado.",
)
_ALTER_COLUMN_NO_CHANGE = (
    Severity.UNKNOWN,
    "Alteração em {target} de {table} que o classificador não reconhece — " + MANUAL,
)
_ALTER_COLUMN_UNPACKED = (
    Severity.UNKNOWN,
    "Argumentos de `op.alter_column` desempacotados de um valor que o classificador "
    "não consegue ler — " + MANUAL,
)


def _refine_alter_column(operation: _Operation, call: ast.Call) -> _Operation | None:
    """Compara o estado final com o anterior, quando o arquivo carrega os dois.

    O Alembic escreve o estado final; o anterior só chega quando o autogenerate
    escreveu `existing_type` e `existing_nullable` junto. Sem eles a resposta é a
    linha `unknown` da tabela — um palpite aqui seria exatamente o defeito que
    este classificador existe para corrigir.

    A comparação de tipo é textual: `sa.String(length=50)` contra
    `sa.String(length=100)` é mudança, e o mesmo texto duas vezes não é. Escrever
    o mesmo tipo de duas formas (`sa.VARCHAR(50)` contra `sa.String(50)`) sai
    como mudança — erra para `breaking`, que é o lado de errar que não esconde
    quebra.

    Os sentinelas do Alembic são lidos como o Alembic os lê. `new_column_name` e
    `type_` têm `None` como padrão e `server_default` tem `False`; escritos à mão
    — e o autogenerate escreve — eles chegam como um nó da AST. Perguntar "existe
    um nó aqui" em vez de "foi passado um valor" põe um `breaking` de coluna
    renomeada numa migração que não renomeia nada.

    Quando a chamada traz mais de uma mudança, vale a mais grave.
    """
    if has_unpacked_arguments(call):
        return replace(
            operation, severity=_ALTER_COLUMN_UNPACKED[0], reason=_ALTER_COLUMN_UNPACKED[1]
        )

    changes: list[tuple[Severity, str]] = []
    renamed = _argument(operation, call, "new_column_name")
    if renamed is not None and not _is_none(renamed):
        # Renomear é legível sem estado anterior nenhum: o nome novo está ali —
        # desde que seja um nome. Vindo de uma variável, a política é a mesma do
        # `unique=` do `create_index`: não é `breaking`, é `unknown`.
        changes.append(
            _ALTER_COLUMN_RENAMED if text(renamed) is not None else _ALTER_COLUMN_RENAMED_UNKNOWN
        )

    existing_type = _argument(operation, call, "existing_type")
    if existing_type is None or _is_none(existing_type):
        if not changes:
            return None
    else:
        new_type = _argument(operation, call, "type_")
        if (
            new_type is not None
            and not _is_none(new_type)
            and ast.dump(new_type) != ast.dump(existing_type)
        ):
            changes.append(_ALTER_COLUMN_RETYPED)

        nullable = _argument(operation, call, "nullable")
        existing_nullable = _argument(operation, call, "existing_nullable")
        if _is_true(nullable) and not _is_true(existing_nullable):
            changes.append(_ALTER_COLUMN_NULLABLE)
        elif _is_false(nullable) and not _is_false(existing_nullable):
            changes.append(
                _ALTER_COLUMN_NOT_NULL
                if _is_true(existing_nullable)
                else _ALTER_COLUMN_NOT_NULL_UNKNOWN
            )

        server_default = _argument(operation, call, "server_default")
        if server_default is not None and not _is_false(server_default):
            # `server_default=False` é o sentinela do Alembic para "não mexe no
            # default"; `server_default=None` é DROP DEFAULT, que é mudança.
            changes.append(_ALTER_COLUMN_DEFAULT)

    severity, reason = max(
        changes, key=lambda change: severity_rank(change[0]), default=_ALTER_COLUMN_NO_CHANGE
    )
    return replace(operation, severity=severity, reason=reason)


_REFINERS: dict[str, Callable[[_Operation, ast.Call], _Operation | None]] = {
    "add_column": _refine_add_column,
    "alter_column": _refine_alter_column,
    "create_index": _refine_create_index,
}


# --- expansões -------------------------------------------------------------
#
# A operação que não é uma severidade só: `op.execute()` carrega SQL, e o SQL é
# de `detect/sql.py`. Devolve a lista de findings que substitui a linha da
# tabela.


def _sql_text(node: ast.expr | None) -> str | None:
    """O SQL literal de um argumento, atravessando `sa.text('...')`.

    `op.execute(sa.text("..."))` é a forma idiomática do Alembic, e o que está
    dentro do `text()` é literal do mesmo jeito. Qualquer outra coisa em volta —
    `.bindparams(...)`, uma f-string, uma variável — não é literal e não passa
    daqui.
    """
    if (
        isinstance(node, ast.Call)
        and call_name(node) == "text"
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return text(node)


def _expand_execute(operation: _Operation, call: ast.Call) -> list[Finding]:
    """Classifica o SQL do `op.execute()` e devolve o statement mais grave."""
    if has_unpacked_arguments(call):
        return [_finding("op.execute", operation, call)]
    statement = _sql_text(_argument(operation, call, "sqltext"))
    if statement is None:
        return [_finding("op.execute", operation, call)]
    findings = classify_sql(statement)
    return [
        worst(findings)
        or Finding(
            Severity.NONE,
            "op.execute",
            "Chamada `op.execute()` sem SQL a executar — não altera schema.",
        )
    ]


_EXPANDERS: dict[str, Callable[[_Operation, ast.Call], list[Finding]]] = {
    "execute": _expand_execute,
}


# ---------------------------------------------------------------------------
# Despacho de um statement do corpo de upgrade()
# ---------------------------------------------------------------------------


def _classify_call(name: str, call: ast.Call) -> list[Finding]:
    """Classifica uma chamada `op.<name>(...)`."""
    label = f"op.{name}"
    operation = _OPERATIONS.get(name)
    if operation is None:
        return [
            Finding(
                Severity.UNKNOWN,
                label,
                f"Operação `{label}` fora da tabela do classificador — " + MANUAL,
            )
        ]

    expander = _EXPANDERS.get(name)
    if expander is not None:
        return expander(operation, call)

    refiner = _REFINERS.get(name)
    if refiner is not None:
        operation = refiner(operation, call) or operation
    return [_finding(label, operation, call)]


def _classify_statement(statement: ast.stmt, aliases: frozenset[str]) -> list[Finding]:
    """Classifica um statement do corpo de `upgrade()`.

    Só chamada em posição de statement é operação. É essa regra — e não uma
    lista de exceções — que faz `op.f()` aninhado nos argumentos de um
    `create_index` não contar como operação.
    """
    if isinstance(statement, ast.Pass):
        return []
    if isinstance(statement, ast.Expr):
        value = statement.value
        if isinstance(value, ast.Constant):
            # Docstring da função, ou `...`. Não é operação e não é dúvida.
            return []
        if isinstance(value, ast.Call):
            name = _op_method(value, aliases)
            if name is not None:
                return _classify_call(name, value)
            return [
                Finding(
                    Severity.UNKNOWN,
                    "?",
                    f"Chamada a {ref(call_name(value) or '')} no corpo de `upgrade()` "
                    "que não é uma operação do Alembic — " + MANUAL,
                )
            ]
    return [Finding(Severity.UNKNOWN, "?", _unreadable_reason(statement))]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def _unreadable(reason: str) -> list[Finding]:
    return [Finding(Severity.UNKNOWN, "upgrade", reason)]


def classify_migration(source: str) -> list[Finding]:
    """Classifica cada operação do `upgrade()` de uma versão do Alembic.

    Devolve um `Finding` por operação, na ordem em que aparecem no corpo da
    função. Um arquivo sem `upgrade()` devolve lista vazia: não é uma migração.
    `downgrade()` é ignorado — o que roda no deploy é o `upgrade()`.

    Um statement que não é chamada de operação não interrompe a leitura: sai
    como `unknown` e as operações seguintes continuam classificadas. Parar no
    primeiro esconderia o `drop_table` que vem depois de um `if`.

    Quem chama associa os findings ao caminho do arquivo e agrega a severidade;
    o `Finding` descreve uma operação, não um arquivo.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _unreadable(
            "Arquivo não é Python válido — o classificador não leu as operações."
        )

    upgrade, reason = _upgrade_of(tree)
    if reason is not None:
        return _unreadable(reason)
    if upgrade is None:
        return []

    aliases = _op_aliases(tree)
    findings: list[Finding] = []
    for statement in upgrade.body:
        findings.extend(_classify_statement(statement, aliases))
    return findings
