import json
import os
import sys
import time

from openai import OpenAI

# ------------------------------------------------------------------
# Constantes
# ------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um analista especialista em migrações de banco de dados.
Analise os arquivos de migração fornecidos e classifique cada um de acordo com os critérios abaixo.

Critérios de classificação:
- safe (Safe Change): adição de coluna opcional, nova tabela não consumida, novo valor enum com fallback, novo índice, campo novo na camada analítica.
- controlled (Mudança Controlada): alterar tamanho de varchar, alterar precisão numérica, tornar campo nullable, alterar valor default.
- breaking (Breaking Change): remover campo, renomear campo, alterar tipo de dado, alterar chave primária, remover tabela consumida.

Regras:
1. Inclua TODOS os arquivos fornecidos no array "items", exatamente um objeto por arquivo — mesmo que a classificação seja "none".
2. Se o arquivo não contiver nenhuma mudança de banco de dados, classifique como "none".
3. Prefira a classificação mais conservadora quando houver dúvida.
4. O campo "reason" deve ser uma frase curta em português descrevendo objetivamente a mudança.
5. O campo "confidence" deve refletir sua certeza (0.0 a 1.0).
6. O campo "highest_severity" deve refletir a severidade máxima entre todos os arquivos.

Responda SOMENTE com JSON válido neste formato exato, sem nenhum texto extra ou bloco de código markdown:
{
    "has_db_change": true,
    "highest_severity": "breaking",
    "confidence": 0.95,
    "items": [
    {
        "file": "caminho/arquivo.py",
        "severity": "breaking",
        "reason": "Remoção do campo qty na tabela orders"
    }
    ]
}"""

SEVERITY_META: dict[str, tuple[str, str]] = {
    "safe":       ("🟢", "Safe Change"),
    "controlled": ("🟡", "Mudança Controlada"),
    "breaking":   ("🔴", "Breaking Change"),
    "none":       ("⚪", "Sem alteração de banco"),
}

MAX_FILE_BYTES = 6000


# ------------------------------------------------------------------
# Credenciais
# ------------------------------------------------------------------


def resolve_credentials(ai_api_key: str, github_token: str) -> tuple[str, bool]:
    """Retorna (api_key_a_usar, usando_github_models)."""
    using_github_models = not bool(ai_api_key)
    api_key = ai_api_key if ai_api_key else github_token
    return api_key, using_github_models


# ------------------------------------------------------------------
# Leitura de arquivos
# ------------------------------------------------------------------


def read_migration_files(files: list[str]) -> dict[str, str]:
    """Lê os arquivos de migração e retorna {caminho: conteúdo}."""
    contents: dict[str, str] = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(MAX_FILE_BYTES)
                if len(content) == MAX_FILE_BYTES:
                    remaining = fh.read(1)
                    if remaining:
                        print(f"  [WARN] {path} truncado em {MAX_FILE_BYTES} bytes", file=sys.stderr)
            contents[path] = content
            print(f"  [OK] {path} ({len(content)} chars)")
        except Exception as exc:
            contents[path] = f"[Erro ao ler arquivo: {exc}]"
            print(f"  [WARN] {path}: {exc}", file=sys.stderr)
    return contents


def build_context_block(file_contents: dict[str, str]) -> str:
    """Formata o bloco de contexto enviado ao modelo."""
    return "\n\n".join(f"=== {p} ===\n{c}" for p, c in file_contents.items())


# ------------------------------------------------------------------
# Prompt
# ------------------------------------------------------------------


def build_user_prompt(
    context_block: str,
    pr_number: str,
    pr_title: str,
    repo: str,
) -> str:
    return (
        f"Analise as seguintes migrações de banco de dados:\n\n"
        f"{context_block}\n\n"
        f"PR #{pr_number} — {pr_title}\n"
        f"Repositório: {repo}\n\n"
        f"Retorne SOMENTE o JSON solicitado."
    )


# ------------------------------------------------------------------
# Análise via IA
# ------------------------------------------------------------------


def call_ai(client: OpenAI, model: str, user_prompt: str) -> str:
    """Chama a API e retorna o conteúdo bruto da resposta."""
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=1200,
    )
    return response.choices[0].message.content.strip()


def parse_ai_response(raw: str) -> dict:
    """Remove envelope markdown opcional e faz parse do JSON."""
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) >= 2 else raw
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def make_fallback_result(files: list[str]) -> dict:
    """Resultado conservador usado quando a chamada à IA falha."""
    return {
        "has_db_change": True,
        "highest_severity": "controlled",
        "confidence": 0.0,
        "items": [
            {
                "file": f,
                "severity": "controlled",
                "reason": "Análise automática falhou — revisão manual necessária",
            }
            for f in files
        ],
    }


# ------------------------------------------------------------------
# Pós-processamento
# ------------------------------------------------------------------


def apply_confidence_threshold(result: dict, min_conf: float) -> dict:
    """Promove `safe` → `controlled` quando confiança está abaixo do limiar."""
    confidence = float(result.get("confidence", 1.0))
    if confidence < min_conf and result.get("highest_severity") == "safe":
        result = {**result, "highest_severity": "controlled"}
        print(
            f"[INFO] Confiança {confidence:.2f} < {min_conf}"
            " → promovido de safe para controlled"
        )
    return result


def build_slack_text(
    result: dict,
    pr_url: str,
    pr_title: str,
    pr_number: str,
    pr_author: str,
) -> str:
    severity = result.get("highest_severity", "none")
    emoji, label = SEVERITY_META.get(severity, ("⚪", severity))

    items = result.get("items") or []
    reasons = [
        i["reason"] for i in items if i.get("reason") and i.get("severity") != "none"
    ]
    description = "\n• ".join(reasons) if reasons else "Alteração de banco detectada."
    if reasons:
        description = "• " + description

    return (
        f"{emoji} *{label}* detectada em migração de banco\n"
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

        write("has_db_change",    str(result.get("has_db_change", False)).lower())
        write("highest_severity", result.get("highest_severity", "none"))
        write("confidence",       str(confidence))
        write("slack_text",       slack_text)
        write("analysis_json",    json.dumps(result, ensure_ascii=False))


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------


def main() -> None:
    ai_api_key   = os.environ.get("AI_API_KEY", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    api_url = os.environ.get("AI_API_URL", "").strip()
    model   = os.environ.get("AI_MODEL", "").strip()

    if not api_url:
        raise SystemExit("[ERRO] AI_API_URL nao configurada.")
    if not model:
        raise SystemExit("[ERRO] AI_MODEL nao configurado.")

    api_key, using_github_models = resolve_credentials(ai_api_key, github_token)
    print(f"==> Provedor de IA: {'GitHub Models API' if using_github_models else 'Hub externo'}")
    print(f"==> URL: {api_url}  |  Modelo: {model}")

    client = OpenAI(base_url=api_url, api_key=api_key, timeout=30.0)

    files_raw = os.environ.get("MIGRATION_FILES", "")
    files = [f.strip() for f in files_raw.split("|") if f.strip()]

    file_contents = read_migration_files(files)
    context_block = build_context_block(file_contents)

    user_prompt = build_user_prompt(
        context_block,
        pr_number=os.environ.get("PR_NUMBER", ""),
        pr_title=os.environ.get("PR_TITLE", ""),
        repo=os.environ.get("REPO", ""),
    )

    MAX_RETRIES = 2
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw = call_ai(client, model, user_prompt)
            result = parse_ai_response(raw)
            print(f"==> Resposta da IA:\n{json.dumps(result, ensure_ascii=False, indent=2)}")
            break
        except Exception as exc:
            if attempt < MAX_RETRIES:
                print(f"[WARN] Tentativa {attempt + 1} falhou: {exc}. Retentando...")
                time.sleep(2)
            else:
                print(f"[ERRO] Falha na análise com IA: {exc}", file=sys.stderr)
                result = make_fallback_result(files)

    min_conf   = float(os.environ.get("MINIMUM_CONFIDENCE", "0.70"))
    result     = apply_confidence_threshold(result, min_conf)
    confidence = float(result.get("confidence", 1.0))

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
