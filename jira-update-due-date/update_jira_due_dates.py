"""Atualiza Due Date de cards do Jira em "In Progress" sem Due Date e com Story Points.

Variaveis de ambiente esperadas:
  JIRA_BASE_URL            ex.: https://afya.atlassian.net
  JIRA_USER_EMAIL          e-mail da conta de servico do Jira
  JIRA_API_TOKEN           token de API
  JIRA_SCOPES_FILE         caminho do JSON com mapping de SP e escopos
                           (default: .github/jira-scopes.json)
  JIRA_STORY_POINTS_FIELD  id(s) do customfield de Story Points, separados por virgula
                           (ex.: customfield_11030,customfield_10008). Avaliados em
                           ordem e o primeiro com valor nao-nulo e usado. Util quando
                           a instancia tem multiplos fields homonimos.
  JIRA_STATUS_IN_PROGRESS  nome do status (default: "In Progress")
  DRY_RUN                  se "true", apenas loga sem atualizar
"""

from __future__ import annotations

import json
import os
import sys
from base64 import b64encode
from datetime import date, timedelta
from urllib import error, parse, request


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        auth = b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Jira API {method} {path} falhou ({exc.code}): {detail}"
            ) from exc

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        issues: list[dict] = []
        next_token: str | None = None
        while True:
            payload: dict = {"jql": jql, "fields": fields, "maxResults": 100}
            if next_token:
                payload["nextPageToken"] = next_token
            result = self._request("POST", "/rest/api/3/search/jql", payload)
            issues.extend(result.get("issues", []))
            next_token = result.get("nextPageToken")
            if not next_token or result.get("isLast", True):
                break
        return issues

    def set_due_date(self, issue_key: str, due_date: str) -> None:
        self._request(
            "PUT",
            f"/rest/api/3/issue/{parse.quote(issue_key)}",
            {"fields": {"duedate": due_date}},
        )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Variavel de ambiente obrigatoria ausente: {name}")
    return value


def escape_jql_literal(value: str) -> str:
    """Escapa string para uso entre aspas duplas em JQL."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_jql(scopes: list[dict], status: str) -> str:
    if not scopes:
        raise ValueError("Nenhum escopo configurado em 'scopes'.")

    scope_clauses: list[str] = []
    for idx, scope in enumerate(scopes):
        project = scope.get("project")
        if not project:
            raise ValueError(f"Scope #{idx} sem 'project'.")
        parts = [f'project = "{escape_jql_literal(project)}"']
        for field_name, value in (scope.get("filters") or {}).items():
            parts.append(
                f'"{escape_jql_literal(field_name)}" = '
                f'"{escape_jql_literal(str(value))}"'
            )
        scope_clauses.append("(" + " AND ".join(parts) + ")")

    scopes_jql = " OR ".join(scope_clauses)
    common = (
        f'status = "{escape_jql_literal(status)}" '
        f'AND duedate is EMPTY '
        f'AND "Story Points" is not EMPTY'
    )
    return f"({scopes_jql}) AND {common}"


def normalize_sp_map(raw: dict) -> dict[int, int]:
    out: dict[int, int] = {}
    for key, value in raw.items():
        try:
            sp_key = int(round(float(key)))
            days = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Mapeamento invalido em story_points_to_business_days: {key}={value}"
            ) from exc
        out[sp_key] = days
    return out


def add_business_days(start: date, n: int) -> date:
    """Adiciona N dias uteis (seg-sex) a partir de 'start'."""
    current = start
    remaining = n
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def load_config(path: str) -> tuple[list[dict], dict[int, int]]:
    if not os.path.isfile(path):
        sys.exit(f"Arquivo de configuracao nao encontrado: {path}")
    with open(path, encoding="utf-8") as fp:
        config = json.load(fp)

    scopes = config.get("scopes") or []
    raw_map = config.get("story_points_to_business_days") or {}
    if not raw_map:
        sys.exit("Campo 'story_points_to_business_days' ausente ou vazio.")
    return scopes, normalize_sp_map(raw_map)


def main() -> int:
    base_url = required_env("JIRA_BASE_URL")
    email = required_env("JIRA_USER_EMAIL")
    token = required_env("JIRA_API_TOKEN")
    sp_fields_raw = required_env("JIRA_STORY_POINTS_FIELD")
    sp_field_candidates = [f.strip() for f in sp_fields_raw.split(",") if f.strip()]
    scopes_file = os.environ.get("JIRA_SCOPES_FILE", ".github/jira-scopes.json")
    status_name = os.environ.get("JIRA_STATUS_IN_PROGRESS", "In Progress")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    scopes, sp_to_days = load_config(scopes_file)
    jql = build_jql(scopes, status_name)

    client = JiraClient(base_url, email, token)
    print(f"[info] JQL: {jql}")
    print(f"[info] Story Points fields (em ordem): {sp_field_candidates}")
    print(f"[info] Mapeamento SP -> dias uteis: {sp_to_days}")
    issues = client.search(jql, ["summary", "subtasks", *sp_field_candidates])
    print(f"[info] {len(issues)} card(s) encontrado(s).")

    today = date.today()
    updated = skipped = failed = 0

    # Fase 1: calcular candidato por SP para cada issue elegivel
    candidates: dict[str, dict] = {}
    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        subtask_refs = fields.get("subtasks") or []

        raw_sp = None
        sp_source = None
        for candidate in sp_field_candidates:
            value = fields.get(candidate)
            if value is not None:
                raw_sp = value
                sp_source = candidate
                break

        if raw_sp is None:
            tried = ", ".join(sp_field_candidates)
            print(f"[skip] {key}: nenhum field com SP populado (tentados: {tried}).")
            skipped += 1
            continue

        try:
            sp_key = int(round(float(raw_sp)))
        except (TypeError, ValueError):
            print(f"[skip] {key}: Story Points invalido ({raw_sp!r}).")
            skipped += 1
            continue

        days = sp_to_days.get(sp_key)
        if days is None:
            print(
                f"[skip] {key}: SP={sp_key} nao mapeado em "
                f"story_points_to_business_days."
            )
            skipped += 1
            continue

        candidates[key] = {
            "summary": summary,
            "subtask_keys": [s["key"] for s in subtask_refs],
            "sp_key": sp_key,
            "sp_source": sp_source,
            "days": days,
            "sp_due_date": add_business_days(today, days).isoformat(),
        }

    # Fase 2: buscar duedates atuais das subtarefas que NAO estao em candidates
    external_keys: set[str] = set()
    for info in candidates.values():
        for sk in info["subtask_keys"]:
            if sk not in candidates:
                external_keys.add(sk)

    external_duedates: dict[str, str | None] = {}
    if external_keys:
        keys_jql = ", ".join(f'"{k}"' for k in sorted(external_keys))
        sub_jql = f"key in ({keys_jql})"
        print(f"[info] Buscando duedate de {len(external_keys)} subtarefa(s) externa(s)...")
        for sub in client.search(sub_jql, ["duedate"]):
            external_duedates[sub["key"]] = sub.get("fields", {}).get("duedate")

    # Fase 3: aplicar override quando houver subtarefas com duedate
    for key, info in candidates.items():
        subtask_dates: list[tuple[str, str]] = []  # (subtask_key, duedate)
        for sk in info["subtask_keys"]:
            if sk in candidates:
                subtask_dates.append((sk, candidates[sk]["sp_due_date"]))
            elif external_duedates.get(sk):
                subtask_dates.append((sk, external_duedates[sk]))

        if subtask_dates:
            # ISO YYYY-MM-DD ordena lexicograficamente == cronologicamente
            latest_key, latest_date = max(subtask_dates, key=lambda x: x[1])
            final_date = latest_date
            source_desc = (
                f"max de {len(subtask_dates)} subtarefa(s); maior: "
                f"{latest_key}={latest_date}"
            )
        else:
            final_date = info["sp_due_date"]
            source_desc = (
                f"SP={info['sp_key']} ({info['sp_source']}) "
                f"+{info['days']}d uteis"
            )

        prefix = "[dry-run]" if dry_run else "[update]"
        print(f"{prefix} {key} -> {final_date} | {source_desc} | {info['summary']}")

        if dry_run:
            continue

        try:
            client.set_due_date(key, final_date)
            updated += 1
        except RuntimeError as exc:
            print(f"[error] Falha ao atualizar {key}: {exc}")
            failed += 1

    print(
        f"[done] atualizados={updated} ignorados={skipped} "
        f"falhas={failed} dry_run={dry_run}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
