"""Classificador de migrações do Django.

Lê a lista `operations` da classe `Migration` com o módulo `ast`: sem importar
Django, sem abrir conexão, sem executar nada do arquivo. O que o classificador
sabe é o que está escrito no fonte.

Isso põe um teto no que dá para afirmar, e o teto é a regra do módulo: valor
que não é literal — SQL montado em runtime, operação vinda de uma variável,
lista `operations` construída por compreensão — devolve `unknown`. O modo de
falha que este classificador substitui é o de um provedor que respondia
`controlled` com confiança 0.0 para tudo enquanto o job seguia verde; um
`unknown` visível no Slack é o oposto disso.

Dois caminhos não são óbvios:

- `SeparateDatabaseAndState` desce nos dois ramos. O DDL real mora em
  `database_operations` — é por lá que o par `RunSQL` + `AlterField` das
  migrações de `external_id` do consumidor Django/MySQL fica classificável.
- `RunSQL` não classifica SQL: delega para `detect/sql.py`, o mesmo módulo que
  atende o `queryRunner.query()` do TypeORM e o `op.execute()` do Alembic. O
  mesmo DDL tem que sair com a mesma cor nas três stacks.

`AlterField` e `AlterUniqueTogether` respondem melhor quando quem chama tem o
diretório `migrations/` em mãos: `classify_migration(source, prior=...)` recebe
o estado anterior do app, reconstruído por `detect/history.py`, e as duas
operações passam a comparar o que mudou em vez de reportar `unknown`. Sem
`prior` — quem só tem o texto do arquivo — a resposta é a mesma de sempre. A
dependência aponta numa direção só: este módulo declara o protocolo
`_PriorState` e não conhece o módulo que o implementa.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol

from ._reading import (
    call_name,
    elements,
    has_unpacked_arguments,
    operations_of,
    text,
)
from .severity import DUPLICATE, MANUAL, Finding, Severity, ref, worst
from .sql import classify_sql

__all__ = ["classify_migration"]

# ---------------------------------------------------------------------------
# Leitura da AST
# ---------------------------------------------------------------------------


def _is_true(node: ast.expr | None) -> bool:
    """O nó é o literal `True`? `1` e um nome qualquer não são."""
    return isinstance(node, ast.Constant) and node.value is True


# ---------------------------------------------------------------------------
# A tabela de regras
# ---------------------------------------------------------------------------
#
# Daqui para baixo é a tabela de severidade: que operação do Django vale
# quanto, e por quê. É o artefato que a QQ-2162 leva ao time de dados para
# validar, então é para ser lida e editada por quem não conhece o resto do
# módulo. Uma linha por operação; a mecânica de ler a AST ficou toda acima.
#
# `reason` é template: `{model}` é o modelo que a operação mexe e `{target}` é
# o objeto dentro dele — campo, índice, constraint, nome novo. `model` e
# `target` dizem em qual parâmetro da operação cada um mora, e `signature` é a
# ordem posicional dos parâmetros no Django, que é o que permite ler
# `migrations.RemoveField('subject', 'code')` sem keyword.
#
# `signature` tem um segundo consumidor: `detect/history.py` lê `fields`,
# `options`, `old_name`, `new_name`, `unique_together` e `state_operations` por
# posição para reconstruir o estado anterior. Encurtar uma tupla dessas não
# quebra nada aqui e faz a reconstrução parar de enxergar o argumento em
# silêncio — `test_the_parameters_each_handler_reads_are_in_the_signature`, em
# `tests/test_history.py`, é o que segura as duas pontas juntas.
#
# Os três leitores logo abaixo são o que transforma uma linha em `Finding`;
# vêm antes da tabela para que ela possa ser lida de cima a baixo sem saltos.


@dataclass(frozen=True)
class _Operation:
    severity: Severity
    reason: str
    signature: tuple[str, ...] = ()
    model: str = ""
    target: str = ""


def _argument(operation: _Operation, call: ast.Call, parameter: str) -> ast.expr | None:
    """O valor de um parâmetro, dado por keyword ou por posição."""
    for keyword in call.keywords:
        if keyword.arg == parameter:
            return keyword.value
    if parameter in operation.signature:
        index = operation.signature.index(parameter)
        if index < len(call.args):
            return call.args[index]
    return None


def _name_of(operation: _Operation, call: ast.Call, parameter: str) -> str:
    if not parameter:
        return ""
    return text(_argument(operation, call, parameter)) or ""


def _finding(name: str, operation: _Operation, call: ast.Call, **overrides: str) -> Finding:
    names = {
        "model": _name_of(operation, call, operation.model),
        "target": _name_of(operation, call, operation.target),
    }
    names.update(overrides)
    return Finding(
        operation.severity,
        name,
        operation.reason.format(**{key: ref(value) for key, value in names.items()}),
    )


_OPERATIONS: dict[str, _Operation] = {
    # --- modelos ---
    "CreateModel": _Operation(
        Severity.SAFE,
        "Modelo {model} criado — tabela nova, ninguém lê ainda.",
        signature=("name", "fields", "options", "bases", "managers"),
        model="name",
    ),
    "DeleteModel": _Operation(
        Severity.BREAKING,
        "Modelo {model} removido — quem lê essa tabela quebra.",
        signature=("name",),
        model="name",
    ),
    "RenameModel": _Operation(
        Severity.BREAKING,
        "Modelo {model} renomeado — quem lê o nome antigo quebra.",
        signature=("old_name", "new_name"),
        model="old_name",
    ),
    "AlterModelOptions": _Operation(
        Severity.NONE,
        "Opções do modelo {model} alteradas — metadado do Django, não altera schema.",
        signature=("name", "options"),
        model="name",
    ),
    "AlterOrderWithRespectTo": _Operation(
        Severity.CONTROLLED,
        "Ordenação de {model} passou a depender de {target} — "
        "o Django cria a coluna `_order` e a preenche.",
        signature=("name", "order_with_respect_to"),
        model="name",
        target="order_with_respect_to",
    ),
    # --- campos ---
    "AddField": _Operation(
        # Refinado por `_refine_add_field`: sem `null=True` e sem `default`, a
        # coluna entra NOT NULL sem valor e a tabela que já tem linha barra a
        # migração.
        Severity.SAFE,
        "Campo {target} adicionado em {model}.",
        signature=("model_name", "name", "field", "preserve_default"),
        model="model_name",
        target="name",
    ),
    "RemoveField": _Operation(
        Severity.BREAKING,
        "Campo {target} removido de {model} — quem lê essa coluna quebra.",
        signature=("model_name", "name"),
        model="model_name",
        target="name",
    ),
    "AlterField": _Operation(
        # Refinado por `_refine_alter_field`: com o estado anterior em mãos, a
        # comparação das duas definições diz o que mudou. A regra desta linha é
        # o que sobra sem ele — a definição nova sozinha não diz nada.
        Severity.UNKNOWN,
        "Campo {target} de {model} alterado — "
        "o classificador não conhece a definição anterior, " + MANUAL,
        signature=("model_name", "name", "field", "preserve_default"),
        model="model_name",
        target="name",
    ),
    "RenameField": _Operation(
        Severity.BREAKING,
        "Campo {target} de {model} renomeado — quem lê o nome antigo quebra.",
        signature=("model_name", "old_name", "new_name"),
        model="model_name",
        target="old_name",
    ),
    # --- índices e constraints ---
    "AddIndex": _Operation(
        Severity.SAFE,
        "Índice adicionado em {model}.",
        signature=("model_name", "index"),
        model="model_name",
    ),
    "RemoveIndex": _Operation(
        Severity.CONTROLLED,
        "Índice {target} removido de {model} — consulta que dependia dele fica mais lenta.",
        signature=("model_name", "name"),
        model="model_name",
        target="name",
    ),
    "AddConstraint": _Operation(
        # Refinado por `_refine_add_constraint`: `UniqueConstraint` quebra.
        Severity.CONTROLLED,
        "Constraint adicionada em {model}.",
        signature=("model_name", "constraint"),
        model="model_name",
    ),
    "RemoveConstraint": _Operation(
        Severity.CONTROLLED,
        "Constraint {target} removida de {model}.",
        signature=("model_name", "name"),
        model="model_name",
        target="name",
    ),
    "AlterUniqueTogether": _Operation(
        # Refinado por `_refine_alter_unique_together`: com o estado anterior,
        # o conjunto de pares de antes contra o de agora. Sem ele, esvaziar só
        # derruba o índice, e exigir combinação única é tratado como quebra pela
        # mesma razão que `ADD UNIQUE` em `detect/sql.py`.
        Severity.BREAKING,
        "Meta `unique_together` de {model} passou a exigir combinação única — " + DUPLICATE,
        signature=("name", "unique_together"),
        model="name",
    ),
    # --- código arbitrário ---
    "RunPython": _Operation(
        Severity.CONTROLLED,
        "Migração de dados em Python — o classificador não lê o que a função faz. "
        "Confirmar o volume e o impacto com o time de dados.",
    ),
    "RunSQL": _Operation(
        # Expandido por `_expand_run_sql`, que delega para `detect/sql.py`. A
        # regra desta linha é o que sobra quando o SQL não é literal.
        Severity.UNKNOWN,
        "SQL montado dinamicamente — o classificador só lê SQL literal, " + MANUAL,
        signature=("sql", "reverse_sql", "state_operations", "hints", "elidable"),
    ),
    "SeparateDatabaseAndState": _Operation(
        # Expandido por `_expand_separate_database_and_state`. A regra desta
        # linha é o que sobra quando um dos ramos não é uma lista literal.
        Severity.UNKNOWN,
        "Ramo {target} de SeparateDatabaseAndState não é uma lista literal — " + MANUAL,
        signature=("database_operations", "state_operations"),
        # `{target}` aqui é o nome do ramo, que o expansor passa; não sai de um
        # parâmetro da chamada, por isso a linha não declara `target`.
    ),
}


# --- refinamentos ----------------------------------------------------------
#
# As operações cuja severidade depende de mais que o nome. Cada uma recebe o
# walker que está percorrendo o arquivo, a linha da tabela e a chamada, e
# devolve a linha que a substitui — sempre via `replace`, para não repetir a
# assinatura — ou `None` para manter a da tabela. O walker é por onde o estado
# anterior chega: só dois dos quatro o leem, e os outros dois não precisam saber
# o que ele carrega.


def _refine_add_field(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> _Operation | None:
    field = _argument(operation, call, "field")
    if not isinstance(field, ast.Call):
        return replace(
            operation,
            severity=Severity.UNKNOWN,
            reason="Campo {target} adicionado em {model} com uma definição que o "
            "classificador não consegue ler — " + MANUAL,
        )
    nullable = any(
        keyword.arg == "null" and _is_true(keyword.value) for keyword in field.keywords
    )
    has_default = any(keyword.arg == "default" for keyword in field.keywords)
    if nullable or has_default:
        return None
    return replace(
        operation,
        severity=Severity.CONTROLLED,
        reason="Campo {target} adicionado em {model} como NOT NULL sem default — "
        "tabela que já tem linha impede a migração.",
    )


def _refine_alter_field(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> _Operation | None:
    """Compara a definição nova com a anterior, quando ela é conhecida.

    Sem estado anterior — quem chamou não passou `prior`, ou o campo vem de
    outro app, do estado base, de um modelo abstrato — devolve `None`, e o que
    sai é a linha `unknown` da tabela. Um palpite aqui seria exatamente o
    defeito que este classificador existe para corrigir.
    """
    if walker.prior is None:
        return None
    model = _name_of(operation, call, operation.model)
    target = _name_of(operation, call, operation.target)
    if not model or not target:
        return None
    verdict = walker.prior.field_change(
        model, target, _argument(operation, call, "field")
    )
    if verdict is None:
        return None
    severity, reason = verdict
    return replace(operation, severity=severity, reason=reason)


def _refine_add_constraint(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> _Operation | None:
    constraint = call_name(_argument(operation, call, "constraint"))
    if constraint is None:
        return replace(
            operation,
            severity=Severity.UNKNOWN,
            reason="Constraint adicionada em {model} que o classificador não consegue ler — "
            + MANUAL,
        )
    if constraint == "UniqueConstraint":
        return replace(
            operation,
            severity=Severity.BREAKING,
            reason="Constraint UNIQUE adicionada em {model} — " + DUPLICATE,
        )
    return None


def _refine_alter_unique_together(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> _Operation | None:
    declared = _argument(operation, call, "unique_together")
    model = _name_of(operation, call, operation.model)
    if walker.prior is not None and model:
        verdict = walker.prior.unique_together_change(model, declared)
        if verdict is not None:
            severity, reason = verdict
            return replace(operation, severity=severity, reason=reason)
    pairs = elements(declared)
    if pairs is None:
        return replace(
            operation,
            severity=Severity.UNKNOWN,
            reason="Meta `unique_together` de {model} definida por um valor que o "
            "classificador não consegue ler — " + MANUAL,
        )
    if pairs:
        return None
    return replace(
        operation,
        severity=Severity.CONTROLLED,
        reason="Meta `unique_together` de {model} esvaziada — o índice único deixa de existir.",
    )


_REFINERS: dict[str, Callable[["_Walker", _Operation, ast.Call], _Operation | None]] = {
    "AddField": _refine_add_field,
    "AlterField": _refine_alter_field,
    "AddConstraint": _refine_add_constraint,
    "AlterUniqueTogether": _refine_alter_unique_together,
}


# --- expansões -------------------------------------------------------------
#
# As duas operações que não são uma severidade só: uma delega o SQL, a outra
# carrega outras operações dentro. Cada uma devolve a lista de findings que
# substitui a linha da tabela.


def _statements(node: ast.expr | None) -> list[str] | None:
    """Os textos SQL de um argumento `sql`, ou `None` se algum não for literal.

    O Django aceita uma string ou uma lista delas. Aceita também
    `[(sql, params)]`, com parâmetros ligados — que não é literal e por isso
    não passa daqui.
    """
    single = text(node)
    if single is not None:
        return [single]
    items = elements(node)
    if items is None:
        return None
    texts = [text(item) for item in items]
    if any(statement is None for statement in texts):
        return None
    return [statement for statement in texts if statement is not None]


def _expand_run_sql(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> list[Finding]:
    """Classifica o `sql` do RunSQL e devolve o statement mais grave.

    Só o `sql` é lido. O `reverse_sql` só roda em rollback, e classificá-lo
    pintaria de vermelho toda migração que sabe se desfazer.
    """
    statements = _statements(_argument(operation, call, "sql"))
    if statements is None:
        return [_finding("RunSQL", operation, call)]
    findings = [finding for text in statements for finding in classify_sql(text)]
    return [
        worst(findings)
        or Finding(Severity.NONE, "RunSQL", "RunSQL sem SQL a executar — não altera schema.")
    ]


def _expand_separate_database_and_state(
    walker: "_Walker", operation: _Operation, call: ast.Call
) -> list[Finding]:
    """Classifica os dois ramos, `database_operations` primeiro.

    Dentro de `state_operations` a ordem é a do arquivo, com o mesmo
    classifica-e-avança do laço de cima: são elas que montam o estado, e um
    `AddField` seguido de um `AlterField` no mesmo ramo tem que ser lido como
    o Django lê. O ramo de banco não avança estado nenhum — é justamente o que
    esta operação existe para separar.

    A ordem entre os dois ramos é a do impacto, não a do arquivo: o DDL real
    mora no ramo de banco, e é o que o time de dados precisa ver primeiro no
    Slack. O ramo de estado entra depois porque uma `AlterField` lá dentro ainda
    é a única pista do que a migração pretendia — é por ela que `bookings/0034` e
    `subjects/0027`, cujo `ALTER TABLE` é montado por f-string dentro de um
    `RunPython`, ficam classificáveis quando o walker carrega o estado
    anterior.
    """
    # Argumento desempacotado invalida os dois caminhos de leitura de uma vez:
    # `**kwargs` chega com `arg=None` e faz os ramos parecerem ausentes, e
    # `*args` faz a posição 0 deixar de significar `database_operations`. Sem
    # esta guarda o primeiro caso responderia `none` — "não altera schema", sem
    # ter lido nada — e o segundo culparia um ramo que a chamada nem nomeia.
    if has_unpacked_arguments(call):
        # Não dá nem para saber se existe um ramo de estado, quanto mais o que
        # tem dentro. O que já estava reconstruído deixa de valer daqui para
        # frente no arquivo.
        walker.distrust()
        return [
            Finding(
                Severity.UNKNOWN,
                "SeparateDatabaseAndState",
                "Argumentos de SeparateDatabaseAndState desempacotados de um valor que o "
                "classificador não consegue ler — " + MANUAL,
            )
        ]

    findings: list[Finding] = []
    for branch in ("database_operations", "state_operations"):
        node = _argument(operation, call, branch)
        if node is None:
            continue
        items = elements(node)
        if items is None:
            findings.append(
                _finding("SeparateDatabaseAndState", operation, call, target=branch)
            )
            if branch == "state_operations":
                # É este ramo que monta o estado do Django. Não conseguir lê-lo
                # não deixa o estado como estava: deixa-o desatualizado, e o que
                # vier depois no arquivo compararia contra a definição errada.
                walker.distrust()
            continue
        if branch == "state_operations":
            findings.extend(walker.classify_all(items))
        else:
            for element in items:
                findings.extend(walker.classify(element))
    return findings or [
        Finding(
            Severity.NONE,
            "SeparateDatabaseAndState",
            "SeparateDatabaseAndState sem operações — não altera schema.",
        )
    ]


_EXPANDERS: dict[str, Callable[["_Walker", _Operation, ast.Call], list[Finding]]] = {
    "RunSQL": _expand_run_sql,
    "SeparateDatabaseAndState": _expand_separate_database_and_state,
}


# ---------------------------------------------------------------------------
# Despacho de uma operação
# ---------------------------------------------------------------------------


class _PriorState(Protocol):
    """O estado do app imediatamente antes da migração que está sendo lida.

    `detect/history.py` é quem implementa; este módulo só declara o que precisa.
    Os dois métodos de consulta devolvem `(severidade, razão)` — a razão ainda é
    template, com `{model}` e `{target}` por preencher, como qualquer linha da
    tabela — ou `None` quando não há estado anterior para aquele objeto, que é
    quando a linha crua da tabela vale.

    `apply` avança o estado uma operação por vez e `claim` marca o objeto como
    consumido. O estado é de uma migração só: `_Walker` chama `claim` ao nascer,
    e reusar o mesmo objeto num segundo arquivo levanta erro em vez de misturar
    as operações de um na leitura do outro. `distrust` é o contrário de avançar:
    diz que uma operação que mexeria no estado passou sem ser lida, e a partir
    daí toda consulta volta a ser `unknown`.
    """

    def field_change(
        self, model: str, name: str, definition: ast.expr | None
    ) -> tuple[Severity, str] | None: ...

    def unique_together_change(
        self, model: str, pairs: ast.expr | None
    ) -> tuple[Severity, str] | None: ...

    def apply(self, node: ast.expr) -> None: ...

    def claim(self) -> None: ...

    def distrust(self) -> None: ...


class _Walker:
    """Percorre as operações de um arquivo carregando o estado anterior.

    Existe porque o estado precisa alcançar os refinadores e os expansores ficam
    no caminho: `_expand_separate_database_and_state` volta a chamar `classify`,
    e a `AlterField` aninhada num `state_operations` é justamente a forma de
    `bookings/0034`. Passar o estado adiante como parâmetro faria nove assinaturas
    carregarem um argumento que a maioria não lê.

    Sem `prior` o walker não guarda nada, e o classificador responde como
    respondia antes de existir `detect/history.py`.
    """

    def __init__(self, prior: _PriorState | None = None) -> None:
        self.prior = prior
        if prior is not None:
            # O estado é de um arquivo só. Reusar o mesmo objeto em dois
            # `classify_migration` faria as operações do primeiro entrarem no
            # estado anterior do segundo — uma severidade confiante calculada
            # sobre um estado que nunca existiu.
            prior.claim()

    def classify_all(self, nodes: Iterable[ast.expr]) -> list[Finding]:
        """Classifica uma sequência de operações na ordem em que ela vale.

        Classifica e avança, um item por vez: é o único lugar onde a ordem das
        operações do Django é aplicada, e vale igual para a lista `operations`
        do arquivo e para o `state_operations` de um `SeparateDatabaseAndState`.
        """
        findings: list[Finding] = []
        for node in nodes:
            findings.extend(self.classify(node))
            self.advance(node)
        return findings

    def advance(self, node: ast.expr) -> None:
        """Aplica ao estado a operação que acabou de ser classificada.

        As operações do Django valem em ordem dentro do arquivo, então o estado
        anterior de uma `AlterField` inclui o que as operações acima dela
        fizeram — o `AddField` de `schedule/0007` e o `RenameField` de
        `sync_gateway/0003`.

        `SeparateDatabaseAndState` não passa por aqui: o expansor já percorreu
        o ramo de estado com `classify_all`, avançando operação a operação, e
        reaplicar o bloco inteiro renomearia duas vezes. O handler equivalente
        em `detect/history.py` continua existindo para a releitura dos
        ancestrais, que não tem walker.
        """
        if self.prior is None or call_name(node) == "SeparateDatabaseAndState":
            return
        self.prior.apply(node)

    def distrust(self) -> None:
        """Desiste do estado anterior: alguma coisa mexeu nele sem ser lida."""
        if self.prior is not None:
            self.prior.distrust()

    def classify(self, node: ast.expr) -> list[Finding]:
        """Classifica um item da lista `operations`."""
        if not isinstance(node, ast.Call):
            # Variável, condicional, lambda, desempacotamento: o item nem chega a
            # ser uma chamada. O nome do nó da AST não diz nada a quem lê o Slack.
            return [
                Finding(
                    Severity.UNKNOWN,
                    "?",
                    "Item da lista `operations` que não é uma chamada de operação — "
                    + MANUAL,
                )
            ]

        name = call_name(node)
        if name is None:
            # É chamada, mas de uma expressão — `OPS["drop"]()`. Dizer que "não é
            # uma chamada" aqui afirmaria o contrário do que o arquivo diz.
            return [
                Finding(
                    Severity.UNKNOWN,
                    "?",
                    "Operação chamada a partir de uma expressão que o classificador "
                    "não resolve — " + MANUAL,
                )
            ]

        operation = _OPERATIONS.get(name)
        if operation is None:
            return [
                Finding(
                    Severity.UNKNOWN,
                    name,
                    f"Operação `{name}` fora da tabela do classificador — " + MANUAL,
                )
            ]

        expander = _EXPANDERS.get(name)
        if expander is not None:
            return expander(self, operation, node)

        refiner = _REFINERS.get(name)
        if refiner is not None:
            operation = refiner(self, operation, node) or operation
        return [_finding(name, operation, node)]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def _unreadable(reason: str) -> list[Finding]:
    return [Finding(Severity.UNKNOWN, "Migration", reason)]


def classify_migration(source: str, prior: _PriorState | None = None) -> list[Finding]:
    """Classifica cada operação de um arquivo de migração do Django.

    Devolve um `Finding` por operação, na ordem em que aparecem — e, para
    `SeparateDatabaseAndState`, um por operação aninhada, com o ramo de banco
    antes do ramo de estado. Um arquivo sem classe `Migration` ou sem
    `operations` devolve lista vazia: não é uma migração, ou é uma migração de
    merge que não mexe no banco.

    Quem chama associa os findings ao caminho do arquivo e agrega a severidade;
    o `Finding` descreve uma operação, não um arquivo.

    `prior` é o estado do app antes desta migração, que `detect/history.py`
    monta a partir do diretório `migrations/`. É opcional porque nem todo
    chamador tem o diretório: sem ele, `AlterField` sai `unknown` e
    `AlterUniqueTogether` responde pela heurística — o comportamento que esta
    função sempre teve. Um parâmetro e não uma função irmã porque o estado
    precisa acompanhar a leitura operação a operação, dentro do mesmo laço.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _unreadable("Arquivo não é Python válido — o classificador não leu as operações.")

    items, reason = operations_of(tree)
    if items is None:
        return _unreadable(reason or "")
    return _Walker(prior).classify_all(items)
