"""Leitura de um arquivo de migração do Django com `ast`.

Duas coisas moram aqui, e as duas são compartilhadas por `detect/django.py` e
`detect/history.py`:

- os leitores de valor literal — nome da chamada, itens de uma coleção, texto de
  uma string. São o teto do que os dois módulos conseguem afirmar: valor que não
  é literal não é lido, e não ser lido vira `unknown` lá em cima;
- o **portão de confiança**: `operations_of` responde "dá para acreditar na
  lista `operations` deste arquivo?". `classify_migration` usa a resposta para
  mostrar a razão no Slack e `detect/history.py` para decidir se pode reaplicar
  a migração ao estado. É a mesma política — classe `Migration` condicional,
  lista montada fora do corpo da classe, lista que não é literal — e ela tem que
  existir uma vez só: as duas cópias que existiam antes podiam divergir, e a
  divergência sairia como uma severidade confiante calculada sobre um arquivo
  que ninguém leu direito.

O módulo é privado no pacote (`_reading`), então os nomes dentro dele não
precisam de underscore — quem importa daqui está importando de um lugar feito
para ser importado, não espiando o interior de um módulo irmão.
"""

from __future__ import annotations

import ast

from .severity import MANUAL

__all__ = [
    "call_name",
    "class_attribute",
    "elements",
    "has_unpacked_arguments",
    "migration_class",
    "operations_of",
    "text",
]


def call_name(node: ast.expr | None) -> str | None:
    """Nome da operação chamada: `migrations.AddField(...)` vira `AddField`.

    Devolve `None` para qualquer coisa que não seja chamada de um nome simples
    ou de um atributo — variável, lambda, condicional, desempacotamento.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def elements(node: ast.expr | None) -> list[ast.expr] | None:
    """Elementos de um literal de coleção, ou `None` se não der para ler.

    Cobre `[...]`, `(...)`, `{...}` e as chamadas de construtor que as
    migrações antigas do corpus usam — `set([('a', 'b')])`, `set()`.
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return list(node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"set", "frozenset", "list", "tuple"} and not node.keywords:
            if not node.args:
                return []
            if len(node.args) == 1:
                return elements(node.args[0])
    return None


def text(node: ast.expr | None) -> str | None:
    """O texto de uma string literal, ou `None`.

    O parser já junta concatenação implícita (`'a' 'b'`) num único nó; f-string,
    `%`, `.format()` e `join()` não são literais e caem no `None`.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def has_unpacked_arguments(call: ast.Call) -> bool:
    """A chamada recebe `*args` ou `**kwargs`?

    Nos dois casos os argumentos vêm de um valor que o classificador não lê, e
    um parâmetro ausente deixa de significar "não foi passado". Só importa para
    as operações cuja severidade depende dos argumentos; `DeleteModel(**spec)`
    quebra qualquer que seja o modelo.
    """
    return any(argument.arg is None for argument in call.keywords) or any(
        isinstance(argument, ast.Starred) for argument in call.args
    )


# ---------------------------------------------------------------------------
# O portão de confiança
# ---------------------------------------------------------------------------


def migration_class(tree: ast.Module) -> ast.ClassDef | None:
    """A última classe `Migration` do módulo, que é a que o Django carrega."""
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Migration"
    ]
    return classes[-1] if classes else None


def _has_nested_migration_class(tree: ast.Module) -> bool:
    """Existe uma classe `Migration` fora do corpo do módulo?

    O Django carrega `module.Migration`, então uma definição dentro de um `if`
    ou de um `try` no topo do arquivo vale de verdade — mas qual delas vale
    depende de algo que o classificador não executa. Uma classe `Migration`
    dentro de uma função também cai aqui, e `unknown` continua sendo a resposta
    preferível a adivinhar.
    """
    return any(
        isinstance(node, ast.ClassDef) and node.name == "Migration"
        for statement in tree.body
        if not isinstance(statement, ast.ClassDef)
        for node in ast.walk(statement)
    )


def declares_migration_class(tree: ast.Module) -> bool:
    """O módulo declara uma classe `Migration` que este pacote saberia julgar?

    A união exata do que `migration_class` e `_has_nested_migration_class` veem:
    uma classe no corpo do módulo, ou uma dentro de um `if`/`try` no topo. Uma
    `Migration` aninhada em **outra classe** fica de fora de propósito — o
    Django carrega `module.Migration` por `getattr`, e um atributo de outra
    classe não é atributo do módulo, então ele também a ignoraria.

    Existe para o dispatch de `detect/__init__.py` escolher o parser pelo mesmo
    critério que o parser usa para decidir se tem o que dizer. Se os dois
    critérios divergirem, um arquivo é encaminhado para cá e volta com lista
    vazia — que se lê como "nada a reportar".
    """
    return migration_class(tree) is not None or _has_nested_migration_class(tree)


def class_attribute(migration: ast.ClassDef, name: str) -> ast.expr | None:
    """O valor da última atribuição direta de `name` no corpo da classe."""
    value: ast.expr | None = None
    for statement in migration.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = statement.value
    return value


# Métodos que mudam a lista no lugar. `operations.append(...)` deixa
# `operations` em contexto de leitura, então o teste de escrita não o vê — e o
# arquivo sairia com zero findings, que a jusante lê como "nada a reportar".
_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear"})


def _stores_operations(node: ast.AST) -> bool:
    """`operations = ...` ou `operations += ...`."""
    return (
        isinstance(node, ast.Name)
        and node.id == "operations"
        and isinstance(node.ctx, ast.Store)
    )


def _mutates_operations(node: ast.AST) -> bool:
    """`operations.append(...)` e parentes."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "operations"
    )


def _touches_operations(migration: ast.ClassDef) -> bool:
    """A classe mexe em `operations` fora da atribuição direta no corpo?

    Atribuição dentro de um `if`, `operations += ...`, um `for` que chama
    `operations.append(...)`: tudo isso significa que o que `class_attribute`
    leu não é a lista final, e a resposta honesta passa a ser `unknown`.
    """
    for statement in migration.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue  # a atribuição direta é justamente o que se sabe ler
        for node in ast.walk(statement):
            if _stores_operations(node) or _mutates_operations(node):
                return True
    return False


def operations_of(tree: ast.Module) -> tuple[list[ast.expr] | None, str | None]:
    """Os itens da lista `operations`, ou `None` e a razão de não dar para lê-la.

    Três respostas, e a diferença entre elas é o ponto do portão:

    - `([], None)` — não é uma migração, ou é uma que não mexe no banco (merge,
      migração só de estado). Nada a classificar e nada a reaplicar;
    - `(itens, None)` — a lista é literal e vale o que está escrito;
    - `(None, razão)` — o arquivo mexe em `operations` de um jeito que o
      classificador não lê. Para quem classifica, é um `unknown` com esta razão;
      para quem reconstrói o estado, é um arquivo que não pode ser reaplicado.
    """
    if _has_nested_migration_class(tree):
        return None, "A classe `Migration` não é definida no corpo do módulo — " + MANUAL

    migration = migration_class(tree)
    if migration is None:
        return [], None

    if _touches_operations(migration):
        return (
            None,
            "A lista `operations` é montada fora do corpo da classe `Migration` — "
            + MANUAL,
        )

    declared = class_attribute(migration, "operations")
    if declared is None:
        return [], None

    items = elements(declared)
    if items is None:
        return None, "A lista `operations` não é um literal — " + MANUAL
    return items, None
