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
    to_severity,
)
from gha_logger import get_logger

logger = get_logger(__name__)


# Razão do item de um arquivo que o parser leu inteiro e sobre o qual não há
# nada a dizer: `__init__.py` de um pacote `migrations/`, migração de merge, um
# `.sql` só com `BEGIN; COMMIT;`. Ele continua aparecendo em `items` — é assim
# que "li e não achei nada" se distingue de "esqueci deste arquivo".
NOTHING_TO_REPORT = "Nenhuma operação de banco encontrada neste arquivo."


# ------------------------------------------------------------------
# Análise
# ------------------------------------------------------------------


def analyze(files: list[str]) -> dict:
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

    Nada aqui inventa texto a partir do conteúdo da migração. Razão e operação
    chegam prontas dos parsers de `detect/`, que já cortam valores antes de
    citar um statement — migração de dados carrega CPF de paciente e este texto
    vai para um canal do Slack.
    """
    meta = presentation(severity_of(result))

    items = result.get("items") or []
    lines = [
        f"• {ref(item.get('file') or '')} — {ref(item.get('operation') or '')}: {item['reason']}"
        for item in items
        if item.get("reason") and item.get("severity") != Severity.NONE.value
    ]

    description = "\n".join(lines) if lines else "Alteração de banco detectada."

    return (
        f"{meta.emoji} *{meta.label}* {meta.headline}\n"
        f"*PR:* <{pr_url}|#{pr_number} — {pr_title}> por @{pr_author}\n"
        f"{description}\n"
        f"<{pr_url}|Ver PR para detalhes>"
    )


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


def main() -> None:
    files_raw = os.environ.get("MIGRATION_FILES", "")
    files = [f.strip() for f in files_raw.split("|") if f.strip()]

    # Este step só roda com `has_files == 'true'`. Chegar aqui sem arquivo
    # significa que a coleta e a classificação discordam, e o único jeito de
    # terminar seria publicar `none` — um verde que ninguém classificou.
    if not files:
        raise SystemExit(
            "[ERRO] MIGRATION_FILES vazia no step de classificação. "
            "A coleta disse que havia arquivos de migração e nenhum chegou aqui."
        )

    logger.info(f"==> Classificando {len(files)} arquivo(s) de migração")

    # Sem try/except. Qualquer falha daqui para baixo — leitura, dispatch,
    # parse, severidade fora da tabela — derruba o step. Era o `except
    # Exception` com fallback que fazia a GitHub Models API responder 404 por
    # quatro semanas com o job verde e o Slack recebendo `controlled` para todo
    # mundo.
    result = analyze(files)
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
