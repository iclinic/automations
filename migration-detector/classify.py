"""Classifica as migrações de um PR e monta o texto do alerta do Slack.

Este arquivo é a borda: lê o ambiente que o `action.yml` monta, chama o
classificador determinístico de `detect/` e escreve os outputs do step. Ele não
decide severidade — isso mora nas tabelas dos parsers — e não inventa texto a
partir do conteúdo da migração.

Até QQ-2152 o miolo daqui era um prompt e uma chamada de API. A GitHub Models
API foi desligada em 2026-07-30 e o `except Exception` que existia em volta da
chamada devolvia `controlled` com confiança 0.0 para toda migração, com o job
verde — quatro semanas do time de dados recebendo a mesma mensagem amarela
tanto para um `DROP COLUMN` quanto para uma coluna opcional nova. O que
substituiu a IA lê a fonte com `ast` e tabelas, não tem rede no caminho e diz
`unknown` quando não sabe.
"""

import json
import os

from detect import classify_file
from detect.severity import (
    Severity,
    UnknownSeverity,
    max_severity,
    presentation,
    ref,
    severity_rank,
    to_severity,
)
from gha_logger import get_logger

logger = get_logger(__name__)


# Razão do item de um arquivo que o parser leu inteiro e sobre o qual não há
# nada a dizer: `__init__.py` de um pacote `migrations/`, migração de merge, um
# `.sql` só com `BEGIN; COMMIT;`. Ele continua aparecendo em `items` — é assim
# que "li e não achei nada" se distingue de "esqueci deste arquivo".
NOTHING_TO_REPORT = "Nenhuma operação de banco encontrada neste arquivo."

# Arquivo do diff com cara de migração que nenhum padrão de `migration_paths`
# casou. Ele não foi classificado, e é isso que a linha diz: `unknown` é
# literalmente "existe uma migração aqui e eu não a li". Publicar `none`
# tornaria "o PR não tem migração" indistinguível de "o glob não pegou a
# migração", que é a Evidência 4 da ADR — quatro anos de silêncio no
# consumidor TypeORM.
NOT_MATCHED = (
    "Arquivo com cara de migração que nenhum padrão de `migration_paths` casou — "
    "o classificador não o leu. Ajuste `migration_paths` no workflow ou confirme "
    "com o time de dados."
)


# O Slack colapsa o texto do attachment por volta de 700 caracteres, com um
# "Mostrar mais" que abre o resto. Medido nas mensagens reais dos cinco PRs de
# `iclinic/testes-gpi`: das 12 linhas do PR #73, três apareciam sem clicar. O
# conteúdo não se perde — mas quem só passa o olho vê um quarto do PR, e por
# isso a mensagem diz a própria forma numa linha de resumo logo abaixo do
# cabeçalho, que sempre cabe nesse trecho.
#
# O corte aqui é outro problema, e o primeiro que entrou neste arquivo era a
# nova forma do bug que esta entrega conserta: num PR com 14 operações todas
# `breaking`, um orçamento por caractere descartou duas — um `CREATE UNIQUE
# INDEX` e um `DROP TABLE`, que nunca chegaram ao time de dados.
#
# Ordenar por gravidade decrescente não cobre esse caso: quando tudo tem a
# mesma gravidade, a ordenação não separa nada. A regra é explícita — o que
# trava o merge nunca é cortado. `unknown` e `breaking` entram inteiros, e o
# orçamento vale para as severidades que não param ninguém.
BENIGN_BUDGET_CHARS = 1800

# Teto do attachment do Slack, que é dele e não nosso. Passar dele não entrega
# mais informação: entrega menos, porque a mensagem é truncada do lado de lá,
# onde nada avisa que foi.
MAX_TEXT_CHARS = 7000

# Espaço guardado para a linha de "…e mais N", que só existe depois de saber
# quantas ficaram de fora.
_OVERFLOW_ALLOWANCE = 80


# ------------------------------------------------------------------
# Análise
# ------------------------------------------------------------------


def analyze(files: list[str], unmatched: list[str] | None = None) -> dict:
    """Classifica cada arquivo e agrega o resultado do PR.

    Um item por `Finding`, mais um item `none` para cada arquivo que não rendeu
    finding nenhum: **todo arquivo recebido aparece em `items`**. Um arquivo que
    some da saída é indistinguível de um arquivo sem mudança de banco, e essa
    confusão é a forma original do bug desta entrega.

    A contagem de itens não é comparável entre stacks. `typeorm.py` devolve um
    finding por statement; `django.py` e `alembic.py` colapsam um `RunSQL` ou um
    `op.execute` de vários statements num finding só, com `worst()`. A
    severidade agregada não depende disso — `max_severity` é idempotente —, mas
    nada aqui pode contar itens para comparar arquivos ou repositórios.
    """
    items: list[dict] = []

    for path in files:
        findings = classify_file(path)
        logger.info(f"  [OK] {path} — {len(findings)} operação(ões) classificada(s)")
        if findings:
            items.extend(
                {
                    "file": path,
                    "severity": finding.severity.value,
                    "operation": finding.operation,
                    "reason": finding.reason,
                }
                for finding in findings
            )
        else:
            items.append(
                {
                    "file": path,
                    "severity": Severity.NONE.value,
                    "operation": "",
                    "reason": NOTHING_TO_REPORT,
                }
            )

    for path in unmatched or []:
        logger.warning(f"  [?] {path} — nenhum padrão de `migration_paths` casou")
        items.append(
            {
                "file": path,
                "severity": Severity.UNKNOWN.value,
                "operation": "migration_paths",
                "reason": NOT_MATCHED,
            }
        )

    severity = max_severity(item["severity"] for item in items)
    return {
        "has_db_change": severity is not Severity.NONE,
        "highest_severity": severity.value,
        "items": items,
    }


# ------------------------------------------------------------------
# Pós-processamento
# ------------------------------------------------------------------


def severity_of(result: dict) -> Severity:
    """Severidade do resultado, validada contra o vocabulário.

    Estourar é o contrato. `result.get("highest_severity", "none")` era o último
    lugar onde um resultado incompleto virava um alerta verde: bastava a chave
    faltar para o PR ser anunciado como "sem alteração de banco".

    Levanta `UnknownSeverity`, como todo o resto do vocabulário. Quem traduz
    isso em código de saída é o `main()`.
    """
    return to_severity(result.get("highest_severity"))


def confidence_for(severity: Severity | str) -> float:
    """Confiança do output para uma severidade já resolvida.

    Num classificador determinístico não existe meio-termo: ou a operação está
    na tabela do parser, e aí a severidade é a que a tabela diz, ou ela não
    está, e aí é `unknown`. O campo continua no output só por compatibilidade
    com quem já lê `confidence` — `1.0` para severidade resolvida, `0.0` para
    `unknown`, que é o único "não sei" que sobrou. Não há mais promoção de
    `safe` para `controlled` por limiar: `minimum_confidence` está inerte.
    """
    return 0.0 if to_severity(severity) is Severity.UNKNOWN else 1.0


def _summary(reported: list[dict], items: list[dict]) -> str:
    """A forma do PR em uma linha, para quem não clica em "Mostrar mais".

    Vem antes da linha do PR de propósito: o título do PR é longo e variável, e
    o resumo tem que caber no trecho que o Slack mostra sem interação. Sem item
    reportado não há forma a resumir, e a linha some — é o caso do ⚪, cuja
    descrição já diz quantos arquivos foram lidos.
    """
    if not reported:
        return ""
    counts: dict[Severity, int] = {}
    for item in reported:
        found = to_severity(item.get("severity"))
        counts[found] = counts.get(found, 0) + 1
    ordered = sorted(counts, key=severity_rank, reverse=True)
    shape = ", ".join(f"{counts[found]} {found.value}" for found in ordered)
    files = len({item.get("file") for item in items if item.get("file")})
    return f"*{len(reported)} operação(ões) em {files} arquivo(s):* {shape}\n"


def build_slack_text(
    result: dict,
    pr_url: str,
    pr_title: str,
    pr_number: str,
    pr_author: str,
) -> str:
    """Monta a mensagem do Slack.

    Cada item vira uma linha que nomeia o arquivo e a operação antes da razão.
    O arquivo e a operação existem na linha por causa de `unknown`: "não
    entendi uma operação" só serve para quem lê se disser em qual arquivo e
    qual operação. Vale para todas as severidades porque a pergunta "onde?" é a
    mesma nas cinco.

    A linha abre com a severidade do próprio item, no emoji e no rótulo do
    cabeçalho. Um arquivo com um índice único e duas adições rende três linhas, e
    sem isso o resumo dizia "1 breaking, 2 safe" sem dizer qual era qual.

    Nada aqui inventa texto a partir do conteúdo da migração. Razão e operação
    chegam prontas dos parsers de `detect/`, que já cortam valores antes de
    citar um statement — migração de dados carrega CPF de paciente e este texto
    vai para um canal do Slack.
    """
    severity = severity_of(result)
    meta = presentation(severity)

    items = result.get("items") or []
    reported = sorted(
        (
            item
            for item in items
            if item.get("reason") and item.get("severity") != Severity.NONE.value
        ),
        key=lambda item: severity_rank(to_severity(item.get("severity"))),
        reverse=True,
    )

    summary = _summary(reported, items)
    headline = f"{meta.emoji} *{meta.label}* {meta.headline}\n"
    pr_line = f"*PR:* <{pr_url}|#{pr_number} — {pr_title}> por @{pr_author}\n"
    footer = f"<{pr_url}|Ver PR para detalhes>"
    # O teto é da mensagem inteira, e não da lista: cabeçalho, resumo, linha do
    # PR, rodapé e a linha de overflow ocupam lugar no mesmo attachment. Medir
    # só as linhas deixava a mensagem passar do teto por essa diferença.
    ceiling = MAX_TEXT_CHARS - (
        len(headline) + len(summary) + len(pr_line) + len(footer) + _OVERFLOW_ALLOWANCE
    )

    blocks = severity_rank(Severity.UNKNOWN)
    kept: list[str] = []
    used = 0
    for item in reported:
        item_severity = to_severity(item.get("severity"))
        item_meta = presentation(item_severity)
        line = (
            f"• {item_meta.emoji} *{item_meta.label}* · {ref(item.get('file') or '')} — "
            f"{ref(item.get('operation') or '')}: {item['reason']}"
        )
        blocking = severity_rank(item_severity) >= blocks
        # A lista vem do pior para o mais brando, então o primeiro benigno que
        # não couber garante que nenhum dos seguintes cabe.
        if not blocking and used + len(line) + 1 > BENIGN_BUDGET_CHARS:
            break
        if used + len(line) + 1 > ceiling:
            break
        kept.append(line)
        used += len(line) + 1

    left = len(reported) - len(kept)
    if left:
        kept.append(f"…e mais {left} operação(ões) — ver o PR.")

    if kept:
        description = "\n".join(kept)
    elif severity is Severity.NONE:
        # O cabeçalho acabou de dizer "Sem alteração de banco". A linha seguinte
        # dizia "Alteração de banco detectada.", e a mensagem se contradizia em
        # duas linhas.
        #
        # Arquivos, e não itens: um `.sql` com quatro statements rende quatro
        # itens e continua sendo um arquivo. A linha fala de arquivo.
        read = len({item.get("file") for item in items if item.get("file")})
        description = (
            f"{read} arquivo(s) de migração lido(s), nenhuma operação de banco."
        )
    else:
        description = "Alteração de banco detectada."

    return f"{headline}{summary}{pr_line}{description}\n{footer}"


# ------------------------------------------------------------------
# GitHub Actions outputs
# ------------------------------------------------------------------


def write_github_outputs(
    output_path: str,
    result: dict,
    confidence: float,
    slack_text: str,
) -> None:
    with open(output_path, "a", encoding="utf-8") as fh:

        def write(key: str, value: str) -> None:
            if "\n" in value:
                delim = "MIGRATION_DETECTOR_EOF"
                fh.write(f"{key}<<{delim}\n{value}\n{delim}\n")
            else:
                fh.write(f"{key}={value}\n")

        write("has_db_change",    str(result["has_db_change"]).lower())
        write("highest_severity", result["highest_severity"])
        write("confidence",       str(confidence))
        write("slack_text",       slack_text)
        write("analysis_json",    json.dumps(result, ensure_ascii=False))


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------


def _listed(variable: str) -> list[str]:
    """A lista serializada com `|` que o `action.yml` passa por ambiente."""
    return [item.strip() for item in os.environ.get(variable, "").split("|") if item.strip()]


def main() -> None:
    files = _listed("MIGRATION_FILES")
    unmatched = _listed("UNMATCHED_FILES")

    # Este step roda com `has_files == 'true'` ou com arquivo suspeito que o
    # glob não pegou. Chegar aqui sem nenhum dos dois significa que a coleta e a
    # classificação discordam, e o único jeito de terminar seria publicar
    # `none` — um verde que ninguém classificou.
    if not files and not unmatched:
        raise SystemExit(
            "[ERRO] MIGRATION_FILES vazia no step de classificação. "
            "A coleta disse que havia arquivos de migração e nenhum chegou aqui."
        )

    logger.info(
        f"==> Classificando {len(files)} arquivo(s) de migração"
        + (f", mais {len(unmatched)} fora do glob" if unmatched else "")
    )

    # Sem try/except. Qualquer falha daqui para baixo — leitura, dispatch,
    # parse, severidade fora da tabela — derruba o step. Era o `except
    # Exception` com fallback que fazia a GitHub Models API responder 404 por
    # quatro semanas com o job verde e o Slack recebendo `controlled` para todo
    # mundo.
    result = analyze(files, unmatched)
    logger.info(f"==> Classificação:\n{json.dumps(result, ensure_ascii=False, indent=2)}")

    try:
        severity = severity_of(result)
    except UnknownSeverity as exc:
        raise SystemExit(f"[ERRO] Classificação sem severidade reconhecida: {exc}") from None
    confidence = confidence_for(severity)
    result     = {**result, "highest_severity": severity.value, "confidence": confidence}

    pr_url    = os.environ.get("PR_URL", "")
    pr_title  = os.environ.get("PR_TITLE", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    pr_author = os.environ.get("PR_AUTHOR", "")

    slack_text = build_slack_text(result, pr_url, pr_title, pr_number, pr_author)
    result = {**result, "slack_text": slack_text, "pr_url": pr_url}

    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        write_github_outputs(gh_output, result, confidence, slack_text)


if __name__ == "__main__":
    main()
