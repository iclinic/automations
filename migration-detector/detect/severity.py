"""Vocabulário de severidade compartilhado pelos detectores.

Uma severidade responde "quanto isso pode doer para quem consome o banco":

- ``none``       — o statement não mexe no schema (transação, `SET`, comentário);
- ``safe``       — adição que não afeta quem já lê o schema atual;
- ``controlled`` — mudança que afeta o schema mas não quebra leitor existente;
- ``unknown``    — o classificador não sabe dizer; precisa de olho humano;
- ``breaking``   — quem lê o schema atual quebra.

A ordem é `none < safe < controlled < unknown < breaking`. `unknown` fica acima
de `controlled` de propósito: "não consegui classificar" tem que valer mais que
uma classificação benigna quando se agrega a severidade de um arquivo. E fica
abaixo de `breaking` porque nunca pode esconder uma quebra que já foi
identificada.

O módulo também guarda as duas frases que os três classificadores têm que dizer
igual e o `ref` que cita um nome dentro delas. `DUPLICATE` é o risco de um
índice único novo — criado pelo ORM, escrito à mão em SQL ou declarado numa
migração do TypeORM, é a mesma coisa para quem grava. `MANUAL` é o que fecha
toda razão `unknown`, que é a frase mais importante do pacote: é ela que separa
"não sei" de uma classificação benigna com cara de certa.

`SEVERITY_META` fecha o módulo com como cada severidade se apresenta para quem
lê o alerta: emoji, rótulo, cor do anexo do Slack e o resto da primeira linha da
mensagem. É uma tabela só porque antes eram duas — `SEVERITY_META` no
`classify.py` e `COLOR_MAP` no `build_slack_payload.py`, cada uma com o seu
conjunto de severidades e nenhuma com `unknown`. Uma severidade que falta numa
tabela de apresentação não estoura: ela cai num cinza neutro e vira um alerta
com cara de tudo bem, que é exatamente o que esta entrega existe para acabar.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


DUPLICATE = (
    "linha duplicada já existente impede a migração e gravação duplicada passa a falhar."
)

MANUAL = "precisa de revisão manual."


def ref(name: str) -> str:
    """Cita um nome de objeto dentro de uma razão, ou diz que não o achou.

    Nome vazio nunca vira uma crase vazia: `` `` `` no Slack lê como se o
    classificador soubesse o nome e ele fosse em branco.
    """
    return f"`{name}`" if name else "(nome não identificado)"


class UnknownSeverity(ValueError):
    """Não é uma classificação: valor fora do vocabulário, ou sem linha na tabela.

    Um tipo só para o contrato inteiro. Eram três — `ValueError` de
    `Severity(...)`, `KeyError` da tabela e `SystemExit` de `severity_of` — para
    a mesma frase, e quem capturasse uma não pegava as outras. Herda de
    `ValueError` para não quebrar quem já capturava o que `Severity(...)`
    levantava. Traduzir isto em código de saída é trabalho do `main()`, na
    borda; aqui dentro é só uma exceção.
    """


class Severity(str, Enum):
    """Severidade de uma operação de migração."""

    NONE = "none"
    SAFE = "safe"
    CONTROLLED = "controlled"
    UNKNOWN = "unknown"
    BREAKING = "breaking"


_ORDER: tuple[Severity, ...] = (
    Severity.NONE,
    Severity.SAFE,
    Severity.CONTROLLED,
    Severity.UNKNOWN,
    Severity.BREAKING,
)

_RANK: dict[Severity, int] = {severity: i for i, severity in enumerate(_ORDER)}


@dataclass(frozen=True)
class Finding:
    """Uma operação classificada.

    `operation` é o verbo que produziu a severidade, como aparece no SQL ou na
    migração (`DROP COLUMN`, `ADD CONSTRAINT ... UNIQUE`). `reason` é a frase que
    o time de dados lê no Slack.

    Um `Finding` descreve um statement, não um arquivo. Associá-lo a um caminho
    é trabalho de quem chamou o parser.
    """

    severity: Severity
    operation: str
    reason: str


def to_severity(value: Severity | str | None) -> Severity:
    """Converte para `Severity`, ou explode com `UnknownSeverity`."""
    try:
        return Severity(value)
    except ValueError:
        raise UnknownSeverity(
            f"{value!r} não é uma severidade. "
            f"Válidas: {', '.join(s.value for s in Severity)}."
        ) from None


def severity_rank(severity: Severity | str) -> int:
    """Posição da severidade na ordem, do mais brando ao mais grave."""
    return _RANK[Severity(severity)]


def max_severity(severities: Iterable[Severity | str]) -> Severity:
    """Severidade mais grave do conjunto. Sem nenhuma, devolve `none`."""
    return max((Severity(s) for s in severities), key=severity_rank, default=Severity.NONE)


def worst(findings: Iterable[Finding]) -> Finding | None:
    """O `Finding` de maior severidade, com a razão que veio junto.

    Empate fica com o primeiro, que é o que aparece antes no statement. Sem
    nenhum finding, devolve `None` — cabe a quem chamou decidir o que isso
    significa. `max_severity` responde a mesma pergunta quando só a severidade
    importa e a razão não.
    """
    return max(findings, key=lambda finding: severity_rank(finding.severity), default=None)


@dataclass(frozen=True)
class Presentation:
    """Como uma severidade aparece para quem lê o alerta.

    `headline` é o resto da primeira linha da mensagem, depois do emoji e do
    rótulo. Fica na tabela e não num f-string do `classify.py` porque `unknown`
    não cabe na mesma frase que as outras: "Breaking Change detectada em
    migração de banco" descreve uma classificação, e `unknown` é a ausência de
    uma.
    """

    emoji: str
    label: str
    color: str
    headline: str


SEVERITY_META: dict[Severity, Presentation] = {
    Severity.NONE: Presentation(
        "⚪",
        "Sem alteração de banco",
        "#9e9e9e",
        "nos arquivos de migração do PR",
    ),
    Severity.SAFE: Presentation(
        "🟢",
        "Safe Change",
        "good",
        "detectada em migração de banco",
    ),
    Severity.CONTROLLED: Presentation(
        "🟡",
        "Mudança Controlada",
        "warning",
        "detectada em migração de banco",
    ),
    Severity.UNKNOWN: Presentation(
        "🟠",
        "Não classificado",
        "#ff9800",
        "— o classificador não entendeu parte desta migração",
    ),
    Severity.BREAKING: Presentation(
        "🔴",
        "Breaking Change",
        "danger",
        "detectada em migração de banco",
    ),
}


def presentation(severity: Severity | str) -> Presentation:
    """Linha de `SEVERITY_META` da severidade. Severidade fora da tabela estoura.

    Estourar é o contrato. O `.get()` com default que estava nos dois callers é
    o que deixava `unknown` chegar ao Slack pintado de cinza.
    """
    resolved = to_severity(severity)
    try:
        return SEVERITY_META[resolved]
    except KeyError:
        raise UnknownSeverity(
            f"Severidade {resolved.value!r} está no vocabulário mas não tem "
            f"linha em SEVERITY_META."
        ) from None
