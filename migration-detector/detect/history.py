"""Estado anterior de um campo, reconstruído pelo grafo de migrações.

`AlterField` carrega só o estado final do campo. Sozinha, ela não diz se a
coluna encurtou, se passou a NOT NULL ou se só ganhou um `help_text` — e é por
isso que `detect/django.py` a classifica como `unknown`. O estado anterior é o
último `CreateModel`, `AddField` ou `AlterField` que mencionou aquele par
(modelo, campo) antes da migração atual, dentro do mesmo app, e está todo em
disco: os três workflows fazem `checkout` com `fetch-depth: 0`, então o
diretório `migrations/` inteiro está presente quando a action roda.

Este módulo faz três coisas, nesta ordem:

1. **anda a cadeia de `dependencies`** a partir da migração atual, parando em
   cada ancestral do mesmo app — só os ancestrais, porque um ramo paralelo não
   faz parte do estado que o Django monta para esta migração;
2. **reaplica as operações** desses ancestrais, e depois as da própria migração
   à medida que `detect/django.py` as classifica, porque as operações do Django
   valem em ordem dentro do arquivo;
3. **compara** a definição anterior com a nova por uma tabela de severidade.

O que ele não faz: importar Django, abrir conexão, executar migração. Só `ast`.
Isso põe o mesmo teto que o resto do pacote — definição que não é literal
devolve `unknown`, nunca um palpite — e mais um: operação fora de `_HANDLERS`
não altera o estado reconstruído, então um app que mexa no schema por uma
operação de terceiros deixa o estado desatualizado. A guarda contra isso é a
mesma de sempre: campo que não foi achado devolve `unknown`.

A dependência entre os módulos aponta numa direção só. Os leitores de AST e o
portão de confiança moram em `detect/_reading.py`, que os dois usam; daqui para
`detect/django.py` fica o acoplamento que sobra, e é um acoplamento real: os
handlers leem `fields`, `options`, `old_name` e companhia por posição, e a
ordem posicional está declarada uma vez só, na `signature` de cada linha de
`_OPERATIONS`. `django` não importa `history` — conhece só o protocolo
`_PriorState`, e quem chama liga os dois com
`classify_migration(source, prior=prior_state(path))`.

O `AppState` que `prior_state` devolve vale para uma migração e uma só: ele
avança enquanto o arquivo é lido, então reusá-lo num segundo arquivo misturaria
as operações de um no estado anterior do outro. `claim` levanta erro nesse caso
em vez de responder errado.
"""

from __future__ import annotations

import ast
import heapq
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ._reading import (
    call_name,
    class_attribute,
    elements,
    has_unpacked_arguments,
    migration_class,
    operations_of,
    text,
)
from .django import _OPERATIONS, _Operation, _argument
from .severity import DUPLICATE, MANUAL, Severity, ref

__all__ = ["AppState", "prior_state"]


# ---------------------------------------------------------------------------
# Leitura de uma definição de campo
# ---------------------------------------------------------------------------

# Valor que existe no fonte mas não é literal: `default=uuid.uuid4`,
# `on_delete=models.CASCADE`, `validators=[MinValueValidator(1)]`. Compara igual
# a si mesmo, então "continua não literal" não vira "mudou"; e as regras que
# leem um argumento de schema testam se o valor é o literal esperado, então um
# valor opaco nunca satisfaz uma delas por acidente.
_OPAQUE = object()

# Ausente. Diferente de `None`, que é um valor que o fonte pode escrever.
_ABSENT = object()

# Onde mora cada argumento passado por posição. O primeiro posicional de um
# campo comum é o `verbose_name` — `models.CharField("origem", max_length=10)`,
# a forma de `subjects/0028_add_public_api_origin_choice`; o de um campo
# relacional é o alvo.
_POSITIONAL: dict[str, tuple[str, ...]] = {
    "ForeignKey": ("to", "on_delete"),
    "OneToOneField": ("to", "on_delete"),
    "ManyToManyField": ("to",),
}
_POSITIONAL_DEFAULT: tuple[str, ...] = ("verbose_name",)

# Referência a modelo: o Django normaliza `app.Modelo` para minúsculas ao
# desconstruir, então as duas grafias são o mesmo alvo. É leitura, não regra:
# comparar `ml_gateway.PromptRun` com `ml_gateway.promptrun` como
# coisas diferentes seria erro de leitura (`journal/0020`).
_MODEL_REFERENCES = frozenset({"to"})


@dataclass(frozen=True)
class _FieldState:
    """Uma definição de campo como o fonte a escreveu.

    `cls` é o último nome do caminho pontilhado — `util.tsid_fields.TSIDField`
    vira `TSIDField`. Comparar o caminho inteiro faria `models.CharField` e
    `django.db.models.CharField` parecerem classes diferentes.
    """

    cls: str
    arguments: Mapping[str, object]


def _value(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _OPAQUE


def _field_state(node: ast.expr | None) -> _FieldState | None:
    """A definição de campo, ou `None` se o fonte não permitir lê-la."""
    name = call_name(node)
    if name is None or not isinstance(node, ast.Call):
        return None
    if any(keyword.arg is None for keyword in node.keywords):
        return None  # `models.CharField(**spec)`: ausente deixa de significar ausente
    positional = _POSITIONAL.get(name, _POSITIONAL_DEFAULT)
    if len(node.args) > len(positional):
        return None  # posição sem nome conhecido
    arguments: dict[str, object] = {
        keyword.arg: _value(keyword.value) for keyword in node.keywords if keyword.arg
    }
    for index, argument in enumerate(node.args):
        if isinstance(argument, ast.Starred):
            return None
        arguments[positional[index]] = _value(argument)
    for reference in _MODEL_REFERENCES:
        value = arguments.get(reference)
        if isinstance(value, str):
            arguments[reference] = value.lower()
    return _FieldState(name, arguments)


# ---------------------------------------------------------------------------
# A tabela de comparação
# ---------------------------------------------------------------------------
#
# Daqui até `_compare` é a tabela que diz o que cada diferença entre a definição
# anterior e a nova vale. Junto com `_OPERATIONS` em `detect/django.py`, é o
# artefato que a QQ-2162 leva ao time de dados: uma linha por diferença, e a
# primeira que casa responde.
#
# A ordem é de precedência, não de severidade — o específico antes do geral:
#
# 1. a guarda de leitura vem primeiro, porque toda linha abaixo dela lê um
#    argumento de schema e nenhuma saberia decidir sobre um valor não literal;
# 2. as linhas que quebram vêm antes das controladas, para que encurtar *e*
#    passar a NOT NULL responda pela quebra;
# 3. as duas linhas de `max_length` implícito vêm depois das que comparam dois
#    comprimentos escritos, porque só sobra para elas o caso em que um dos lados
#    não declara nada;
# 4. o balde de argumento desconhecido vem antes do fim, e o fim é a única linha
#    que casa com tudo.
#
# `reason` é template: `{model}` é o modelo, `{target}` é o campo, `{before}` e
# `{after}` são valores e `{arguments}` é uma lista de nomes de argumento — quem
# cita algum deles traz um `detail` que os monta.

# Os argumentos que viram DDL. Um valor não literal em qualquer um deles
# devolve `unknown`: a regra que o lê não teria como decidir, e "não decidiu"
# não pode sair como "não mudou".
_SCHEMA_ARGUMENTS: tuple[str, ...] = (
    "null",
    "unique",
    "max_length",
    "db_index",
    "max_digits",
    "decimal_places",
)

# O que o Django resolve sozinho quando o argumento não está escrito.
_DEFAULTS: dict[str, object] = {"null": False, "unique": False, "db_index": False}

# Os argumentos que nunca chegam ao schema. `choices`, `verbose_name`,
# `help_text` e `blank` são validação e admin; `default` não vira DEFAULT no
# banco, o Django preenche na escrita; `on_delete` é aplicado em Python, não por
# constraint. Argumento fora desta lista e fora de `_SCHEMA_ARGUMENTS` devolve
# `unknown` quando muda — é assim que uma lacuna da tabela aparece no Slack em
# vez de sair como `none`.
_WITHOUT_DDL = frozenset(
    {
        "auto_created",
        "auto_now",
        "auto_now_add",
        "blank",
        "choices",
        "default",
        "editable",
        "error_messages",
        "help_text",
        "limit_choices_to",
        "on_delete",
        "related_name",
        "related_query_name",
        "serialize",
        "validators",
        "verbose_name",
    }
)


def _argument_of(state: _FieldState, name: str) -> object:
    return state.arguments.get(name, _DEFAULTS.get(name, _ABSENT))


def _declares_length(state: _FieldState) -> bool:
    """O campo escreve um `max_length` literal?

    Não escrever não quer dizer "sem limite": `FileField`, `EmailField` e
    `SlugField` têm um comprimento implícito, que muda entre versões do Django
    e que este módulo não conhece. Por isso "declara" e "não declara" são casos
    diferentes, e não dois valores comparáveis.
    """
    value = _argument_of(state, "max_length")
    return isinstance(value, int) and not isinstance(value, bool)


def _shortened(before: _FieldState, after: _FieldState) -> bool:
    if not (_declares_length(before) and _declares_length(after)):
        return False
    return _argument_of(after, "max_length") < _argument_of(before, "max_length")


def _lengthened(before: _FieldState, after: _FieldState) -> bool:
    if not (_declares_length(before) and _declares_length(after)):
        return False
    return _argument_of(after, "max_length") > _argument_of(before, "max_length")


def _changed_arguments(before: _FieldState, after: _FieldState) -> set[str]:
    names = set(before.arguments) | set(after.arguments)
    return {
        name for name in names if _argument_of(before, name) != _argument_of(after, name)
    }


def _outside_the_table(before: _FieldState, after: _FieldState) -> set[str]:
    return _changed_arguments(before, after) - _WITHOUT_DDL - set(_SCHEMA_ARGUMENTS)


def _opaque_schema_arguments(before: _FieldState, after: _FieldState) -> list[str]:
    return [
        name
        for name in _SCHEMA_ARGUMENTS
        if _argument_of(before, name) is _OPAQUE or _argument_of(after, name) is _OPAQUE
    ]


def _names(*values: object) -> str:
    return ", ".join(ref(str(value)) for value in values)


def _slot(value: str) -> str:
    """Um valor pronto para atravessar as duas passadas de `format`.

    A razão sai daqui com `{model}` e `{target}` ainda por preencher, então
    `detect/django.py` roda um segundo `format` sobre ela. Uma chave que
    chegasse por um valor viraria um campo de formatação na segunda passada e
    derrubaria a classificação do arquivo inteiro com `KeyError`. Hoje todo
    valor é identificador ou número; a garantia não pode depender disso.
    """
    return value.replace("{", "{{").replace("}", "}}")


@dataclass(frozen=True)
class _Comparison:
    """Uma diferença entre a definição anterior e a nova, e o que ela vale."""

    name: str
    severity: Severity
    reason: str
    applies: Callable[[_FieldState, _FieldState], bool]
    detail: Callable[[_FieldState, _FieldState], dict[str, str]] | None = None


_COMPARISONS: tuple[_Comparison, ...] = (
    _Comparison(
        "argumento de schema não literal",
        Severity.UNKNOWN,
        "Campo {target} de {model} tem argumento de schema que não é literal "
        "({arguments}) — " + MANUAL,
        lambda before, after: bool(_opaque_schema_arguments(before, after)),
        lambda before, after: {
            "arguments": _names(*_opaque_schema_arguments(before, after))
        },
    ),
    _Comparison(
        "classe do campo mudou",
        Severity.BREAKING,
        "Campo {target} de {model} mudou de {before} para {after} — "
        "o tipo da coluna muda e quem lê o valor antigo quebra.",
        lambda before, after: before.cls != after.cls,
        lambda before, after: {"before": ref(before.cls), "after": ref(after.cls)},
    ),
    _Comparison(
        "campo passou a NOT NULL",
        Severity.BREAKING,
        "Campo {target} de {model} passou a NOT NULL — linha com valor nulo "
        "impede a migração e gravação sem valor passa a falhar.",
        lambda before, after: _argument_of(before, "null") is True
        and _argument_of(after, "null") is False,
    ),
    _Comparison(
        "campo passou a UNIQUE",
        Severity.BREAKING,
        "Campo {target} de {model} passou a UNIQUE — " + DUPLICATE,
        lambda before, after: _argument_of(before, "unique") is False
        and _argument_of(after, "unique") is True,
    ),
    _Comparison(
        "max_length reduzido",
        Severity.BREAKING,
        "Campo {target} de {model} encurtou de {before} para {after} — "
        "valor mais longo que o novo limite impede a migração.",
        _shortened,
        lambda before, after: {
            "before": ref(str(_argument_of(before, "max_length"))),
            "after": ref(str(_argument_of(after, "max_length"))),
        },
    ),
    _Comparison(
        "max_length aumentado",
        Severity.CONTROLLED,
        "Campo {target} de {model} alongou de {before} para {after} — "
        "a coluna é reescrita, mas nenhum valor existente se perde.",
        _lengthened,
        lambda before, after: {
            "before": ref(str(_argument_of(before, "max_length"))),
            "after": ref(str(_argument_of(after, "max_length"))),
        },
    ),
    _Comparison(
        "campo passou a aceitar nulo",
        Severity.CONTROLLED,
        "Campo {target} de {model} passou a aceitar nulo — quem lê a coluna "
        "precisa tratar ausência de valor.",
        lambda before, after: _argument_of(before, "null") is False
        and _argument_of(after, "null") is True,
    ),
    _Comparison(
        "unique removido",
        Severity.CONTROLLED,
        "Campo {target} de {model} deixou de ser UNIQUE — o índice único some e "
        "quem contava com a unicidade deixa de tê-la.",
        lambda before, after: _argument_of(before, "unique") is True
        and _argument_of(after, "unique") is False,
    ),
    _Comparison(
        "db_index alterado",
        Severity.CONTROLLED,
        "Índice do campo {target} de {model} mudou — criar ou derrubar índice "
        "reescreve a tabela e muda o plano das consultas.",
        lambda before, after: _argument_of(before, "db_index")
        != _argument_of(after, "db_index"),
    ),
    _Comparison(
        "precisão numérica alterada",
        Severity.CONTROLLED,
        "Precisão numérica do campo {target} de {model} mudou — a coluna é "
        "reescrita e o arredondamento dos valores existentes muda.",
        lambda before, after: _argument_of(before, "max_digits")
        != _argument_of(after, "max_digits")
        or _argument_of(before, "decimal_places") != _argument_of(after, "decimal_places"),
    ),
    _Comparison(
        # `trial_kits/0008`: `FileField()` vira `FileField(max_length=255)`. O
        # comprimento de antes é o default implícito da classe, que não está
        # escrito em lugar nenhum que o `ast` alcance — pode ser um aumento ou
        # uma redução, e dizer qual seria adivinhar.
        "max_length passou a ser declarado",
        Severity.UNKNOWN,
        "Campo {target} de {model} passou a declarar `max_length` {after}, e o "
        "comprimento que valia antes é o implícito da classe, que não está "
        "escrito — " + MANUAL,
        lambda before, after: not _declares_length(before) and _declares_length(after),
        lambda before, after: {"after": ref(str(_argument_of(after, "max_length")))},
    ),
    _Comparison(
        "max_length deixou de ser declarado",
        Severity.UNKNOWN,
        "Campo {target} de {model} deixou de declarar `max_length` {before}, e o "
        "comprimento que passa a valer é o implícito da classe, que não está "
        "escrito — " + MANUAL,
        lambda before, after: _declares_length(before) and not _declares_length(after),
        lambda before, after: {"before": ref(str(_argument_of(before, "max_length")))},
    ),
    _Comparison(
        "argumento fora da tabela",
        Severity.UNKNOWN,
        "Campo {target} de {model} mudou {arguments}, e a tabela de comparação "
        "não sabe se isso altera o schema — " + MANUAL,
        lambda before, after: bool(_outside_the_table(before, after)),
        lambda before, after: {
            "arguments": _names(*sorted(_outside_the_table(before, after)))
        },
    ),
    _Comparison(
        "sem alteração de schema",
        Severity.NONE,
        "Campo {target} de {model} alterado sem DDL — a diferença fica no "
        "estado do Django e não chega ao banco.",
        lambda before, after: True,
    ),
)


def _compare(before: _FieldState, after: _FieldState) -> tuple[Severity, str]:
    for comparison in _COMPARISONS:
        if comparison.applies(before, after):
            detail = comparison.detail(before, after) if comparison.detail else {}
            return comparison.severity, comparison.reason.format(
                model="{model}",
                target="{target}",
                **{name: _slot(value) for name, value in detail.items()},
            )
    raise AssertionError("a última linha da tabela casa com tudo")  # pragma: no cover


# --- unique_together -------------------------------------------------------
#
# Mesma forma, sobre o conjunto de tuplas do `Meta.unique_together`. Sem estado
# anterior, `detect/django.py` continua respondendo pela heurística da QQ-2155
# — exigir combinação única é tratado como quebra —, que não distingue "exigir
# um par novo" de "largar um de dois pares que já existiam".


@dataclass(frozen=True)
class _UniqueTogetherComparison:
    name: str
    severity: Severity
    reason: str
    applies: Callable[[frozenset, frozenset, bool], bool]


# Mesma ordem de precedência: a linha da tabela nova vem antes da de par novo
# porque as duas casam quando o `CreateModel` está neste mesmo arquivo, e quem
# tem que responder é a que sabe que a tabela ainda não tem linha. O fim é a
# única linha que casa com tudo.
_UNIQUE_TOGETHER_COMPARISONS: tuple[_UniqueTogetherComparison, ...] = (
    _UniqueTogetherComparison(
        "tabela criada nesta migração",
        Severity.SAFE,
        "Meta `unique_together` de {model} definida junto com a tabela — "
        "índice único sobre tabela que ainda não tem linha.",
        lambda before, after, created_here: created_here and bool(after - before),
    ),
    _UniqueTogetherComparison(
        "combinação única nova",
        Severity.BREAKING,
        "Meta `unique_together` de {model} passou a exigir combinação única — "
        + DUPLICATE,
        lambda before, after, created_here: bool(after - before),
    ),
    _UniqueTogetherComparison(
        "combinação única removida",
        Severity.CONTROLLED,
        "Meta `unique_together` de {model} deixou de exigir uma combinação — "
        "o índice único correspondente some.",
        lambda before, after, created_here: bool(before - after),
    ),
    _UniqueTogetherComparison(
        "combinação única inalterada",
        Severity.NONE,
        "Meta `unique_together` de {model} reescrita sem mudança — "
        "as mesmas combinações, não altera schema.",
        lambda before, after, created_here: True,
    ),
)


def _compare_unique_together(
    before: frozenset, after: frozenset, created_here: bool
) -> tuple[Severity, str]:
    for comparison in _UNIQUE_TOGETHER_COMPARISONS:
        if comparison.applies(before, after, created_here):
            return comparison.severity, comparison.reason.format(
                model="{model}", target="{target}"
            )
    raise AssertionError("a última linha da tabela casa com tudo")  # pragma: no cover


def _pairs(node: ast.expr | None) -> frozenset[tuple[str, ...]] | None:
    """O conjunto de tuplas de um `unique_together`, ou `None` se não der."""
    items = elements(node)
    if items is None:
        return None
    pairs: set[tuple[str, ...]] = set()
    for item in items:
        columns = elements(item)
        if columns is None:
            return None
        names = tuple(text(column) for column in columns)
        if any(name is None for name in names):
            return None
        pairs.add(tuple(name for name in names if name is not None))
    return frozenset(pairs)


# ---------------------------------------------------------------------------
# Reaplicação das operações
# ---------------------------------------------------------------------------


@dataclass
class _Model:
    """O estado de um modelo: os campos e o `unique_together`.

    Um campo presente com valor `None` é um campo que existe mas cuja definição
    o classificador não conseguiu ler — diferente de um campo ausente, e as duas
    coisas produzem razões diferentes.
    """

    fields: dict[str, _FieldState | None] = field(default_factory=dict)
    # `None` é "não sei": um modelo que apareceu só num `AddField` pode ter
    # `unique_together` declarado num `CreateModel` que este app não escreveu.
    # Só `CreateModel` e `AlterUniqueTogether` estabelecem o conjunto.
    unique_together: frozenset[tuple[str, ...]] | None = None


def _options_unique_together(node: ast.expr | None) -> frozenset | None:
    if not isinstance(node, ast.Dict):
        return frozenset() if node is None else None
    for key, value in zip(node.keys, node.values):
        if text(key) == "unique_together":
            return _pairs(value)
    return frozenset()


class AppState:
    """O estado dos modelos de um app, reconstruído até um ponto da cadeia.

    Nasce posicionado imediatamente antes de uma migração (via `prior_state`) e
    avança conforme `detect/django.py` classifica as operações dessa migração,
    porque as operações do Django valem em ordem dentro do arquivo — é o que
    torna legível o par `AddField` + `AlterField` de
    `schedule/0007_schedule_integration_uuid` e o par `RenameField` +
    `AlterField` de `sync_gateway/0003_auto_20260115_0900`.
    """

    def __init__(self) -> None:
        self._models: dict[str, _Model] = {}
        self._created_here: set[str] = set()
        self._trusted = True
        self._claimed = False

    @classmethod
    def after(cls, ancestors: Iterable[_Migration]) -> "AppState":
        """O estado depois de reaplicar `ancestors`, na ordem recebida.

        É o único construtor que produz um estado utilizável, porque é ele que
        zera `_created_here` no fim: "criado aqui" quer dizer criado na migração
        que está sendo classificada, e um modelo que um ancestral criou não
        conta. Sem essa limpeza um `AlterUniqueTogether` sobre tabela antiga
        sairia `safe` — a resposta exatamente errada.
        """
        state = cls()
        for ancestor in ancestors:
            state._apply_all(ancestor.operations)
        state._created_here.clear()
        return state

    # --- ciclo de vida ----------------------------------------------------

    def claim(self) -> None:
        """Marca o estado como consumido por uma migração.

        `_Walker` chama isto ao nascer. Um segundo `classify_migration` sobre o
        mesmo objeto levanta erro em vez de deixar as operações do primeiro
        arquivo entrarem no estado anterior do segundo — que é o jeito mais
        fácil de esta reconstrução produzir uma severidade confiante e errada.
        """
        if self._claimed:
            raise RuntimeError(
                "O estado anterior é de uma migração só. Chame `prior_state` de "
                "novo para o próximo arquivo; memoize os ancestrais lidos, "
                "nunca o AppState."
            )
        self._claimed = True

    def distrust(self) -> None:
        """Desiste do estado: daqui para frente toda consulta é `unknown`.

        Chamado quando alguma coisa que muda o estado passou sem ser lida. O que
        sobra na memória continua parecendo certo e não é: seguir respondendo
        seria dar a definição de antes da operação que não foi lida.
        """
        self._trusted = False

    # --- consulta, o que `detect/django.py` chama -------------------------

    def field_change(
        self, model: str, name: str, definition: ast.expr | None
    ) -> tuple[Severity, str] | None:
        """A severidade de um `AlterField`, ou `None` sem estado anterior."""
        if not self._trusted:
            return None
        state = self._models.get(model.lower())
        if state is None or name not in state.fields:
            return None
        before = state.fields[name]
        if before is None:
            return None
        after = _field_state(definition)
        if after is None:
            return (
                Severity.UNKNOWN,
                "Campo {target} de {model} alterado com uma definição que o "
                "classificador não consegue ler — " + MANUAL,
            )
        return _compare(before, after)

    def unique_together_change(
        self, model: str, pairs: ast.expr | None
    ) -> tuple[Severity, str] | None:
        """A severidade de um `AlterUniqueTogether`, ou `None` sem estado."""
        if not self._trusted:
            return None
        state = self._models.get(model.lower())
        if state is None or state.unique_together is None:
            return None
        after = _pairs(pairs)
        if after is None:
            return None  # a heurística da QQ-2155 já responde por valor ilegível
        return _compare_unique_together(
            state.unique_together, after, model.lower() in self._created_here
        )

    # --- avanço -----------------------------------------------------------

    def apply(self, node: ast.expr) -> None:
        """Aplica uma operação ao estado, se ela for uma que mexe nele."""
        name = call_name(node)
        if name is None or not isinstance(node, ast.Call):
            return
        handler = _HANDLERS.get(name)
        operation = _OPERATIONS.get(name)
        if handler is None or operation is None:
            # Handler sem linha correspondente na tabela de `detect/django.py`
            # não pode ler parâmetro nenhum por posição. Um `KeyError` aqui
            # derrubaria a classificação do arquivo inteiro;
            # `test_every_handler_belongs_to_an_operation` é o que impede o
            # descasamento de chegar até aqui.
            return
        handler(self, operation, node)

    def _apply_all(self, nodes: Iterable[ast.expr]) -> None:
        for node in nodes:
            self.apply(node)

    def _model(self, name: str) -> _Model:
        return self._models.setdefault(name.lower(), _Model())

    # --- as operações que mexem no estado ---------------------------------

    def _create_model(self, operation, call: ast.Call) -> None:
        name = text(_argument(operation, call, "name"))
        if name is None:
            return
        state = _Model(unique_together=_options_unique_together(
            _argument(operation, call, "options")
        ))
        self._models[name.lower()] = state
        self._created_here.add(name.lower())
        fields = elements(_argument(operation, call, "fields"))
        if fields is None:
            return
        for entry in fields:
            columns = elements(entry)
            if not columns or len(columns) != 2:
                continue
            column = text(columns[0])
            if column is not None:
                state.fields[column] = _field_state(columns[1])

    def _set_field(self, operation, call: ast.Call) -> None:
        model = text(_argument(operation, call, "model_name"))
        name = text(_argument(operation, call, "name"))
        if model is None or name is None:
            return
        self._model(model).fields[name] = _field_state(
            _argument(operation, call, "field")
        )

    def _remove_field(self, operation, call: ast.Call) -> None:
        model = text(_argument(operation, call, "model_name"))
        name = text(_argument(operation, call, "name"))
        if model is None or name is None:
            return
        self._model(model).fields.pop(name, None)

    def _rename_field(self, operation, call: ast.Call) -> None:
        model = text(_argument(operation, call, "model_name"))
        old = text(_argument(operation, call, "old_name"))
        new = text(_argument(operation, call, "new_name"))
        if model is None or old is None or new is None:
            return
        fields = self._model(model).fields
        if old in fields:
            fields[new] = fields.pop(old)

    def _rename_model(self, operation, call: ast.Call) -> None:
        old = text(_argument(operation, call, "old_name"))
        new = text(_argument(operation, call, "new_name"))
        if old is None or new is None:
            return
        state = self._models.pop(old.lower(), None)
        if state is not None:
            self._models[new.lower()] = state
        if old.lower() in self._created_here:
            self._created_here.discard(old.lower())
            self._created_here.add(new.lower())

    def _delete_model(self, operation, call: ast.Call) -> None:
        name = text(_argument(operation, call, "name"))
        if name is None:
            return
        self._models.pop(name.lower(), None)
        self._created_here.discard(name.lower())

    def _alter_unique_together(self, operation, call: ast.Call) -> None:
        name = text(_argument(operation, call, "name"))
        if name is None:
            return
        self._model(name).unique_together = _pairs(
            _argument(operation, call, "unique_together")
        )

    def _separate_database_and_state(self, operation, call: ast.Call) -> None:
        # Só `state_operations` altera o estado que o Django carrega; o ramo de
        # banco executa DDL sem mexer no estado, que é a razão de a operação
        # existir. É por `state_operations` que `bookings/0034` e `subjects/0027`
        # declaram o campo que o RunPython altera no banco.
        if has_unpacked_arguments(call):
            # `SeparateDatabaseAndState(**spec)`: nem dá para saber se existe
            # ramo de estado, quanto mais o que tem dentro.
            self.distrust()
            return
        declared = _argument(operation, call, "state_operations")
        if declared is None:
            # Ramo ausente é o caso comum — `bookings/0034` e as migrações de
            # índice do corpus só têm `database_operations`. Não há o que
            # aplicar, e não há nada de errado nisso.
            return
        items = elements(declared)
        if items is None:
            # Ramo escrito mas ilegível: as operações escondidas nele são
            # justamente as que mudariam um campo. Seguir com o resto devolveria
            # uma definição desatualizada como se fosse a anterior.
            self.distrust()
            return
        self._apply_all(items)


# Uma entrada por operação que mexe no estado dos modelos. Operação fora daqui
# passa sem alterar nada — inclusive as que este pacote nem conhece.
_HANDLERS: dict[str, Callable[[AppState, _Operation, ast.Call], None]] = {
    "CreateModel": AppState._create_model,
    "AddField": AppState._set_field,
    "AlterField": AppState._set_field,
    "RemoveField": AppState._remove_field,
    "RenameField": AppState._rename_field,
    "RenameModel": AppState._rename_model,
    "DeleteModel": AppState._delete_model,
    "AlterUniqueTogether": AppState._alter_unique_together,
    "SeparateDatabaseAndState": AppState._separate_database_and_state,
}


# ---------------------------------------------------------------------------
# A cadeia de dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Migration:
    stem: str
    dependencies: tuple[str, ...]
    operations: tuple[ast.expr, ...]


def _same_app_dependencies(value: ast.expr | None, app: str) -> tuple[str, ...]:
    """Os nomes de migração do próprio app citados em `dependencies`.

    Dependência de outro app não ordena nada aqui, e
    `migrations.swappable_dependency(...)` — 77 das dependências do corpus — nem
    é uma tupla.
    """
    entries = elements(value)
    if entries is None:
        return ()
    names: list[str] = []
    for entry in entries:
        pair = elements(entry)
        if pair is None or len(pair) != 2:
            continue
        if text(pair[0]) == app:
            name = text(pair[1])
            if name is not None:
                names.append(name)
    return tuple(names)


def _read(path: Path, app: str) -> _Migration | None:
    """Lê uma migração do disco, ou `None` se ela não der para ler inteira.

    "Inteira" é a palavra que importa. Uma migração cuja lista `operations` o
    classificador não consegue ler não pode ser pulada: as operações que ela
    esconde são justamente as que mudariam o estado, e reaplicar as outras
    devolveria uma definição *desatualizada* — que sai como uma severidade
    confiante e errada. `_ancestors` trata este `None` como "não há estado
    anterior", que é a resposta honesta.

    Quem decide se dá para acreditar no arquivo é `operations_of`, o mesmo
    portão que `classify_migration` usa; aqui só a razão é descartada, porque
    ninguém vai ler no Slack a razão de um ancestral.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    migration = migration_class(tree)
    if migration is None:
        return None
    operations, _ = operations_of(tree)
    if operations is None:
        return None
    return _Migration(
        path.stem,
        _same_app_dependencies(class_attribute(migration, "dependencies"), app),
        tuple(operations),
    )


def _number(stem: str) -> int:
    prefix = stem.split("_", 1)[0]
    return int(prefix) if prefix.isdigit() else sys.maxsize


def _in_dependency_order(migrations: Mapping[str, _Migration]) -> list[_Migration] | None:
    """As migrações na ordem em que o Django as aplicaria, ou `None` num ciclo.

    A cadeia de `dependencies` manda; o prefixo numérico do nome desempata os
    que a cadeia deixa sem ordem entre si, que é o caso dos dois pais de uma
    migração de merge. Ciclo devolve `None`: aplicar parte da cadeia daria um
    estado anterior *errado*, e errado é pior que ausente.
    """
    pending = {stem: 0 for stem in migrations}
    children: dict[str, list[str]] = {stem: [] for stem in migrations}
    for stem, migration in migrations.items():
        for parent in migration.dependencies:
            if parent in migrations:
                children[parent].append(stem)
                pending[stem] += 1
    ready = [(_number(stem), stem) for stem, count in pending.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[_Migration] = []
    while ready:
        _, stem = heapq.heappop(ready)
        ordered.append(migrations[stem])
        for child in children[stem]:
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(ready, (_number(child), child))
    return ordered if len(ordered) == len(migrations) else None


def _ancestors(path: Path, app: str) -> list[_Migration] | None:
    """As migrações que a cadeia alcança a partir de `path`, sem ela própria.

    Só os ancestrais: um ramo paralelo que ninguém mergeou ainda não faz parte
    do estado desta migração. Cada arquivo é lido e parseado uma vez, e só os
    que a cadeia alcança — o app inteiro nunca é varrido.

    Devolve `None` se a cadeia não puder ser percorrida inteira — ancestral que
    falta em disco, que não dá para ler, ou ciclo. Meia cadeia produz um estado
    anterior errado, e errado é pior que ausente.
    """
    current = _read(path, app)
    if current is None:
        return None
    found: dict[str, _Migration] = {}
    queue = deque(current.dependencies)
    while queue:
        stem = queue.popleft()
        if stem in found or stem == path.stem:
            continue
        migration = _read(path.parent / f"{stem}.py", app)
        if migration is None:
            return None
        found[stem] = migration
        queue.extend(migration.dependencies)
    return _in_dependency_order(found)


def prior_state(path: str | Path) -> AppState:
    """O estado do app imediatamente antes da migração em `path`.

    `path` é o arquivo da migração dentro de `<app>/migrations/`; o nome do app
    sai do diretório, que é o que separa `('subjects', '0001_initial')` de
    `('ledger', '0001_initial')` na lista de dependências.

    Devolve sempre um `AppState`. Um que não achou nada responde `None` a toda
    consulta, e `detect/django.py` mantém o `unknown` da tabela — que é a
    resposta certa quando o estado anterior não foi encontrado.

    **O objeto devolvido é consumido por exatamente uma chamada de
    `classify_migration`.** Ele não é um cache: `classify_migration` avança o
    estado operação a operação enquanto lê o arquivo, então o mesmo objeto numa
    segunda migração carregaria as operações da primeira como se fossem estado
    anterior. Quem quiser economizar numa PR que mexe em vários arquivos do
    mesmo app deve memoizar os ancestrais lidos, nunca o `AppState` — reusá-lo
    levanta `RuntimeError` na segunda chamada, de propósito.
    """
    migration = Path(path)
    return AppState.after(_ancestors(migration, migration.parent.parent.name) or ())
