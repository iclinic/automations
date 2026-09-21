import ast
import json
import os
import pathlib
import subprocess
import textwrap
import sys

import pytest

import classify
from classify import (
    analyze,
    build_slack_text,
    confidence_for,
    severity_of,
    write_github_outputs,
)
from detect.severity import SEVERITY_META, Severity, UnknownSeverity, max_severity
from detect.severity import presentation
from detect.sql import classify_sql

ACTION_DIR = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_RESULT = {
    "has_db_change": True,
    "highest_severity": "breaking",
    "confidence": 0.95,
    "items": [
        {"file": "db/migration.py", "severity": "breaking", "reason": "Remoção do campo qty"},
    ],
}


# ---------------------------------------------------------------------------
# O fallback silencioso não existe mais
# ---------------------------------------------------------------------------


class TestSilentFallbackIsGone:
    @pytest.mark.parametrize(
        "name",
        (
            "make_fallback_result",
            "apply_confidence_threshold",
            # QQ-2160: o prompt e a chamada de API saíram junto com o provedor.
            "SYSTEM_PROMPT",
            "build_user_prompt",
            "build_context_block",
            "call_ai",
            "parse_ai_response",
            "resolve_credentials",
            "read_migration_files",
            # Orçamento de prompt. Cortava 22 das 591 migrações do repo Django
            # no meio e o classificador lia meio arquivo achando que era o todo.
            "MAX_FILE_BYTES",
        ),
    )
    def test_function_no_longer_exists(self, name):
        # `make_fallback_result` devolvia has_db_change=True, controlled e
        # confidence 0.0 para qualquer falha, com o step em success.
        # `apply_confidence_threshold` promovia safe→controlled por limiar, que
        # num classificador determinístico não quer dizer nada.
        assert not hasattr(classify, name)

    def test_the_guard_would_catch_a_name_that_is_still_there(self):
        # Mutante pareado: sem ele o teste acima passa contra qualquer nome,
        # inclusive um que nunca existiu — não distingue "removido" de
        # "escrito errado".
        assert hasattr(classify, "build_slack_text")

    def test_the_module_no_longer_imports_openai(self):
        # Pela AST. O `pip install` saiu do `action.yml`, então um import que
        # sobrasse aqui derrubaria o step no runner, onde não há SDK instalado
        # — e passaria despercebido no venv de teste, que ainda tem o pacote.
        tree = ast.parse((ACTION_DIR / "classify.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        assert "openai" not in imported, sorted(imported)

    def test_no_module_under_the_action_imports_openai(self):
        # O guarda acima só olha `classify.py`. `detect/` é biblioteca padrão e
        # tem que continuar sendo.
        offenders = []
        for path in sorted(ACTION_DIR.glob("*.py")) + sorted(ACTION_DIR.glob("detect/*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module.split(".")[0]]
                if "openai" in names:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == []

    def test_no_generic_except_survives_in_the_module(self):
        # Pela AST, e não por substring: o comentário que explica o `except
        # Exception` removido não pode fazer o guarda passar nem falhar.
        tree = ast.parse((ACTION_DIR / "classify.py").read_text(encoding="utf-8"))
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        generic = [
            h
            for h in handlers
            if h.type is None
            or (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException"))
        ]
        assert generic == [], [h.lineno for h in generic]

    def test_the_guard_would_catch_a_generic_except(self, tmp_path):
        # Mutante pareado: sem ele o teste acima passa num módulo vazio.
        module = tmp_path / "mutante.py"
        module.write_text("try:\n    pass\nexcept Exception:\n    pass\n")
        tree = ast.parse(module.read_text())
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        assert [
            h
            for h in handlers
            if h.type is None
            or (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException"))
        ]


# ---------------------------------------------------------------------------
# severity_of
# ---------------------------------------------------------------------------


class TestSeverityOf:
    @pytest.mark.parametrize("severity", list(Severity))
    def test_accepts_every_severity_of_the_vocabulary(self, severity):
        assert severity_of({"highest_severity": severity.value}) is severity

    def test_missing_key_raises_instead_of_defaulting_to_none(self):
        # `result.get("highest_severity", "none")` anunciava "sem alteração de
        # banco" para um resultado que não classificou nada.
        with pytest.raises(UnknownSeverity):
            severity_of({"items": []})

    def test_severity_outside_the_vocabulary_raises(self):
        with pytest.raises(UnknownSeverity):
            severity_of({"highest_severity": "catastrophic"})

    def test_error_message_names_the_value_and_the_valid_ones(self):
        with pytest.raises(UnknownSeverity) as excinfo:
            severity_of({"highest_severity": "muito_grave"})
        message = str(excinfo.value)
        assert "'muito_grave'" in message
        assert "unknown" in message

    def test_the_whole_vocabulary_speaks_one_exception_type(self):
        # Eram três tipos para a mesma frase: ValueError de `Severity(...)`,
        # KeyError da tabela e SystemExit daqui. Quem capturasse um não pegava
        # os outros.
        for call in (
            lambda: severity_of({"highest_severity": "catastrophic"}),
            lambda: confidence_for("catastrophic"),
            lambda: presentation("catastrophic"),
            lambda: build_slack_text(
                {"highest_severity": "catastrophic"}, "u", "t", "1", "a"
            ),
        ):
            with pytest.raises(UnknownSeverity):
                call()


# ---------------------------------------------------------------------------
# confidence_for
# ---------------------------------------------------------------------------


class TestConfidenceFor:
    @pytest.mark.parametrize(
        "severity", [s for s in Severity if s is not Severity.UNKNOWN]
    )
    def test_resolved_severities_are_pinned_to_one(self, severity):
        assert confidence_for(severity) == 1.0

    def test_unknown_is_the_only_zero(self):
        assert confidence_for(Severity.UNKNOWN) == 0.0

    def test_accepts_the_plain_string(self):
        assert confidence_for("unknown") == 0.0
        assert confidence_for("breaking") == 1.0

    def test_severity_outside_the_vocabulary_raises(self):
        with pytest.raises(UnknownSeverity):
            confidence_for("catastrophic")

    def test_the_zero_is_reachable_only_through_unknown(self):
        zeros = [s for s in Severity if confidence_for(s) == 0.0]
        assert zeros == [Severity.UNKNOWN]


# ---------------------------------------------------------------------------
# build_slack_text
# ---------------------------------------------------------------------------


class TestBuildSlackText:
    @pytest.mark.parametrize("severity", list(Severity))
    def test_every_severity_renders_its_own_headline(self, severity):
        # A frase é montada iterando a tabela, não conferida contra uma lista
        # escrita à mão: linha nova na tabela sem frase própria cai aqui.
        meta = SEVERITY_META[severity]
        result = {**SAMPLE_RESULT, "highest_severity": severity.value}
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert text.startswith(f"{meta.emoji} *{meta.label}* {meta.headline}\n")

    def test_severity_outside_the_vocabulary_raises_instead_of_a_white_ball(self):
        # O teste que existia aqui passava "unknown_sev" e afirmava que saía ⚪,
        # a mesma bolinha de "sem alteração de banco".
        result = {**SAMPLE_RESULT, "highest_severity": "unknown_sev"}
        with pytest.raises(UnknownSeverity):
            build_slack_text(result, "http://pr", "Title", "1", "author")

    def test_unknown_does_not_borrow_the_headline_of_a_real_classification(self):
        unknown = build_slack_text(
            {**SAMPLE_RESULT, "highest_severity": "unknown"}, "http://pr", "T", "1", "a"
        )
        for severity in ("safe", "controlled", "breaking"):
            other = build_slack_text(
                {**SAMPLE_RESULT, "highest_severity": severity}, "http://pr", "T", "1", "a"
            )
            assert unknown.splitlines()[0] != other.splitlines()[0]

    def test_unknown_names_the_file_and_the_operation_it_did_not_understand(self):
        result = {
            "highest_severity": "unknown",
            "items": [
                {
                    "file": "app/migrations/0042_auto.py",
                    "severity": "unknown",
                    "operation": "RunPython",
                    "reason": "Operação `RunPython` fora da tabela do classificador — "
                              "precisa de revisão manual.",
                }
            ],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        # Fragmento renderizado, e não dois `in` soltos: trocar arquivo por
        # operação na linha continuaria satisfazendo duas asserções separadas.
        assert (
            "• `app/migrations/0042_auto.py` — `RunPython`: "
            "Operação `RunPython` fora da tabela do classificador — precisa de revisão manual."
        ) in text

    def test_item_without_file_or_operation_says_so_instead_of_empty_backticks(self):
        result = {
            "highest_severity": "unknown",
            "items": [{"severity": "unknown", "reason": "Não deu para ler."}],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "• (nome não identificado) — (nome não identificado): Não deu para ler." in text
        assert "``" not in text

    def test_contains_pr_number_and_url(self):
        text = build_slack_text(SAMPLE_RESULT, "http://example.com/pr/5", "My PR", "5", "dev")
        assert "#5" in text
        assert "http://example.com/pr/5" in text

    def test_contains_pr_author(self):
        text = build_slack_text(SAMPLE_RESULT, "http://pr", "Title", "1", "jdoe")
        assert "@jdoe" in text

    def test_reasons_included_in_text(self):
        result = {
            "highest_severity": "breaking",
            "items": [
                {
                    "file": "db/0003_drop.sql",
                    "severity": "breaking",
                    "operation": "DROP COLUMN",
                    "reason": "Remoção do campo x",
                }
            ],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "• `db/0003_drop.sql` — `DROP COLUMN`: Remoção do campo x" in text

    def test_one_line_per_item(self):
        result = {
            "highest_severity": "breaking",
            "items": [
                {"file": "a.sql", "severity": "breaking", "operation": "DROP COLUMN",
                 "reason": "Primeira."},
                {"file": "b.sql", "severity": "safe", "operation": "ADD COLUMN",
                 "reason": "Segunda."},
            ],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "• `a.sql` — `DROP COLUMN`: Primeira." in text
        assert "• `b.sql` — `ADD COLUMN`: Segunda." in text

    def test_none_severity_items_excluded_from_reasons(self):
        result = {
            "highest_severity": "safe",
            "items": [{"severity": "none", "reason": "Sem mudança"}],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "Sem mudança" not in text
        assert "Alteração de banco detectada." in text

    def test_mo_key_no_longer_supported(self):
        result = {
            "highest_severity": "controlled",
            "MO": [{"severity": "controlled", "reason": "Nullable adicionado"}],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "Nullable adicionado" not in text
        assert "Alteração de banco detectada." in text

    def test_empty_items_uses_default_description(self):
        result = {"highest_severity": "controlled", "items": []}
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "Alteração de banco detectada." in text

    def test_none_does_not_announce_a_change_it_just_said_there_was_not(self):
        """O cabeçalho ⚪ diz "Sem alteração de banco" e a linha seguinte dizia
        "Alteração de banco detectada." — a mensagem se contradizia em duas
        linhas."""
        result = {
            "highest_severity": "none",
            "items": [
                {"file": "a.sql", "severity": "none", "reason": "Controle de transação."},
                {"file": "a.sql", "severity": "none", "reason": "Ajuste de sessão."},
                {"file": "a.sql", "severity": "none", "reason": "Comentário de metadado."},
                {"file": "b.sql", "severity": "none", "reason": "Statement vazio."},
            ],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "Alteração de banco detectada." not in text
        # Dois arquivos, quatro operações. A linha fala de arquivo, então conta
        # arquivo: um `.sql` com quatro statements não são quatro arquivos.
        assert "2 arquivo(s)" in text


class TestTheSlackTextHasABudget:
    """Uma linha por finding, sem corte, estoura o attachment do Slack.

    A `main` tinha dois limites implícitos — `MAX_FILE_BYTES` na leitura e
    `max_tokens` na resposta do modelo — e os dois saíram junto com o provedor,
    sem substituto. 25 findings com razão de tamanho realista dão mais de 4.500
    caracteres, e o que cai fora do corte do Slack pode ser justamente o
    `breaking`.
    """

    @staticmethod
    def _result(severities):
        return {
            "highest_severity": max_severity(severities).value,
            "items": [
                {
                    "file": f"db/migration_{i:03}.py",
                    "severity": severity,
                    "operation": "DROP COLUMN",
                    "reason": "Coluna `x` removida de `t` — quem lê essa coluna quebra.",
                }
                for i, severity in enumerate(severities)
            ],
        }

    def test_a_long_list_is_cut_and_says_how_much_was_left_out(self):
        result = self._result(["breaking"] * 40)
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        assert text.count("• ") < 40
        assert "ver `analysis_json`" in text

    def test_a_short_list_is_not_cut_and_says_nothing_about_it(self):
        result = self._result(["breaking"] * 3)
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        assert text.count("• ") == 3
        assert "analysis_json" not in text

    def test_the_cut_never_drops_a_breaking_while_keeping_a_safe(self):
        # O `breaking` é o último item da lista: na ordem do arquivo ele seria
        # o primeiro a cair.
        result = self._result(["safe"] * 40 + ["breaking"])
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        assert "migration_040.py" in text

    def test_the_hardest_severity_comes_first(self):
        result = self._result(["safe", "breaking", "controlled"])
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        bullets = [line for line in text.splitlines() if line.startswith("• ")]
        assert "migration_001.py" in bullets[0]
        assert "migration_002.py" in bullets[1]
        assert "migration_000.py" in bullets[2]

    def test_the_description_stays_within_the_budget(self):
        result = self._result(["breaking"] * 200)
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        assert len(text) < 3000


class TestTheGlobMissIsNotSilence:
    """Migração no diff que não casa glob nenhum não pode terminar verde.

    A ADR 0001 põe esta regra na lista do que entra junto e não é negociável:
    "arquivo no diff com cara de migração (...) que não case nenhum padrão de
    `migration_paths` gera `::warning::` e entra na mensagem do Slack".

    A severidade é `unknown` porque é literalmente o que aconteceu: existe um
    arquivo com cara de migração que o classificador não leu. Dizer `none` seria
    o mesmo silêncio que a Evidência 4 da ADR mede em quatro anos — "o PR não
    tem migração" e "o glob não pegou a migração" são indistinguíveis para quem
    lê o check verde.
    """

    def test_an_unmatched_file_is_unknown_and_not_none(self):
        result = analyze([], ["db/migrate/20260904_add_column.rb"])
        assert result["highest_severity"] == "unknown"
        assert result["has_db_change"] is True

    def test_the_item_names_the_file_the_glob_did_not_catch(self):
        result = analyze([], ["db/migrate/20260904_add_column.rb"])
        assert [item["file"] for item in result["items"]] == [
            "db/migrate/20260904_add_column.rb"
        ]
        assert "migration_paths" in result["items"][0]["reason"]

    def test_an_unmatched_file_outranks_a_classified_safe(self, tmp_path):
        safe = tmp_path / "0001_add.sql"
        safe.write_text("ALTER TABLE ledger ADD COLUMN memo varchar(8) NULL;", encoding="utf-8")
        result = analyze([str(safe)], ["db/migrate/20260904_add_column.rb"])
        assert result["highest_severity"] == "unknown"

    def test_an_unmatched_file_never_hides_a_breaking(self, tmp_path):
        drop = tmp_path / "0002_drop.sql"
        drop.write_text("ALTER TABLE ledger DROP COLUMN memo;", encoding="utf-8")
        result = analyze([str(drop)], ["db/migrate/20260904_add_column.rb"])
        assert result["highest_severity"] == "breaking"

    def test_the_unmatched_file_reaches_the_slack_message(self):
        result = analyze([], ["db/migrate/20260904_add_column.rb"])
        text = build_slack_text(result, "http://pr", "T", "1", "a")
        assert "db/migrate/20260904_add_column.rb" in text

    def test_the_step_runs_with_no_matched_file_when_something_was_missed(self, tmp_path):
        process, outputs = _run_step(
            tmp_path,
            migration_files="",
            unmatched_files="db/migrate/20260904_add_column.rb",
        )
        assert process.returncode == 0, process.stderr
        assert "highest_severity=unknown" in outputs

    def test_the_step_still_falls_when_nothing_at_all_arrives(self, tmp_path):
        process, outputs = _run_step(tmp_path, migration_files="", unmatched_files="")
        assert process.returncode != 0
        assert outputs == ""


def _collect_script() -> str:
    """O corpo do step `Collect`, recortado do `action.yml`.

    Recorte por texto e não por `yaml.safe_load` para não pendurar a suíte numa
    dependência a mais — `requirements.txt` está vazio de propósito, e
    `_action_inputs` já lê este arquivo assim.
    """
    lines = (ACTION_DIR / "action.yml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("- name: Collect |"))
    body = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    end = next(
        (i for i in range(body + 1, len(lines)) if lines[i].strip().startswith("- name:")),
        len(lines),
    )
    return textwrap.dedent("\n".join(lines[body + 1 : end]))


def _run_collect(tmp_path, *, changed: str, webhook: bool = True, paths: str | None = None):
    """Roda o step `Collect` com um `git` de mentira no PATH.

    O que este step decide não é testável por leitura: quem sai por onde, com
    qual código de saída e com qual output publicado depende de sete caminhos
    de `bash`. Um teste que procura `--diff-filter=d` no texto do arquivo
    confere que a linha existe, não que ela decide alguma coisa.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "diff" ]]; then printf \'%s\' "$FAKE_DIFF"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    git.chmod(0o755)

    script = tmp_path / "collect.sh"
    script.write_text(_collect_script(), encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("")

    process = subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "GITHUB_OUTPUT": str(output),
            "BASE_SHA": "a",
            "HEAD_SHA": "b",
            "MIGRATION_PATHS": paths or _action_inputs()["migration_paths"].split("default: '")[1].split("'")[0],
            "IGNORE_TERMS": "dump",
            "HAS_SLACK_WEBHOOK": "true" if webhook else "false",
            "FAKE_DIFF": changed,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    outputs = dict(
        line.split("=", 1)
        for line in output.read_text().splitlines()
        if "=" in line
    )
    return process, outputs


class TestCollectDecidesWhoGetsThrough:
    """O step `Collect` executado, não lido.

    Os três apontamentos que mexeram neste step — a guarda do webhook cedo
    demais, o arquivo removido derrubando o classificador e o glob-miss
    silencioso — são todos sobre qual caminho o `bash` toma, e nenhum deles
    aparece numa asserção de texto.
    """

    def test_a_migration_that_matches_a_glob_goes_to_the_classifier(self, tmp_path):
        process, outputs = _run_collect(
            tmp_path, changed="django/app/x/migrations/0002_add.py\nREADME.md"
        )
        assert process.returncode == 0
        assert outputs["has_files"] == "true"
        assert outputs["migration_files"] == "django/app/x/migrations/0002_add.py"

    def test_a_migration_no_glob_catches_becomes_a_warning_and_an_output(self, tmp_path):
        process, outputs = _run_collect(
            tmp_path, changed="db/migrate/20260904_add_column.rb\nREADME.md"
        )
        assert process.returncode == 0
        assert outputs["has_files"] == "false"
        assert outputs["unmatched_files"] == "db/migrate/20260904_add_column.rb"
        assert "::warning::db/migrate/20260904_add_column.rb" in process.stdout

    def test_a_pr_without_migration_says_nothing(self, tmp_path):
        process, outputs = _run_collect(tmp_path, changed="README.md")
        assert process.returncode == 0
        assert outputs == {"has_files": "false"}
        assert "::warning::" not in process.stdout

    def test_without_a_webhook_a_pr_without_migration_still_passes(self, tmp_path):
        """A guarda era a primeira coisa do step, antes do `git diff`: um
        repositório sem a variable configurada tinha *todo* PR vermelho,
        inclusive o de um `README.md`."""
        process, outputs = _run_collect(tmp_path, changed="README.md", webhook=False)
        assert process.returncode == 0
        assert outputs["has_files"] == "false"
        assert "::warning::slack_webhook_url" in process.stdout

    def test_without_a_webhook_a_pr_with_migration_fails(self, tmp_path):
        """O outro lado da mesma guarda: classificar sem ter para onde avisar é
        o silêncio que fez a falha da API de IA rodar quatro semanas."""
        process, _ = _run_collect(
            tmp_path, changed="django/app/x/migrations/0002_add.py", webhook=False
        )
        assert process.returncode == 1
        assert "::error::slack_webhook_url" in process.stderr + process.stdout

    def test_without_a_webhook_a_glob_miss_also_fails(self, tmp_path):
        process, _ = _run_collect(
            tmp_path, changed="db/migrate/20260904_add_column.rb", webhook=False
        )
        assert process.returncode == 1

    def test_a_file_the_ignore_term_ate_leaves_a_warning(self, tmp_path):
        process, outputs = _run_collect(tmp_path, changed="sql/backup_dump.sql")
        assert process.returncode == 0
        assert outputs["has_files"] == "false"
        assert "ignore_name_contains" in process.stdout

    def test_an_empty_diff_publishes_has_files_false(self, tmp_path):
        process, outputs = _run_collect(tmp_path, changed="")
        assert process.returncode == 0
        assert outputs == {"has_files": "false"}


# ---------------------------------------------------------------------------
# write_github_outputs
# ---------------------------------------------------------------------------


class TestWriteGithubOutputs:
    def _read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def test_writes_single_line_values(self, tmp_path):
        out = tmp_path / "output"
        result = {"has_db_change": True, "highest_severity": "breaking"}
        write_github_outputs(str(out), result, 0.95, "simple text")
        content = self._read(str(out))
        assert "has_db_change=true\n" in content
        assert "highest_severity=breaking\n" in content
        assert "confidence=0.95\n" in content

    def test_multiline_slack_text_uses_heredoc(self, tmp_path):
        out = tmp_path / "output"
        result = {"has_db_change": True, "highest_severity": "safe"}
        write_github_outputs(str(out), result, 0.9, "line1\nline2")
        content = self._read(str(out))
        assert "slack_text<<MIGRATION_DETECTOR_EOF" in content
        assert "line1\nline2" in content

    def test_analysis_json_is_valid_json(self, tmp_path):
        out = tmp_path / "output"
        result = {"has_db_change": False, "highest_severity": "none", "items": []}
        write_github_outputs(str(out), result, 1.0, "text")
        content = self._read(str(out))
        json_line = next(l for l in content.splitlines() if l.startswith("analysis_json="))
        json.loads(json_line.removeprefix("analysis_json="))  # não deve lançar

    def test_appends_to_existing_file(self, tmp_path):
        out = tmp_path / "output"
        out.write_text("existing=1\n")
        result = {"has_db_change": False, "highest_severity": "none"}
        write_github_outputs(str(out), result, 1.0, "text")
        content = self._read(str(out))
        assert content.startswith("existing=1\n")

    @pytest.mark.parametrize("missing", ("has_db_change", "highest_severity"))
    def test_result_without_a_classification_key_raises(self, tmp_path, missing):
        # Os `.get()` com default que estavam aqui escreviam `false` e `none`
        # para um resultado que não tinha classificado nada.
        out = tmp_path / "output"
        result = {"has_db_change": True, "highest_severity": "breaking"}
        del result[missing]
        with pytest.raises(KeyError):
            write_github_outputs(str(out), result, 1.0, "text")

    def test_unknown_is_written_out_as_unknown(self, tmp_path):
        out = tmp_path / "output"
        result = {"has_db_change": True, "highest_severity": "unknown", "items": []}
        write_github_outputs(str(out), result, 0.0, "text")
        content = self._read(str(out))
        assert "highest_severity=unknown\n" in content
        assert "confidence=0.0\n" in content


# ---------------------------------------------------------------------------
# Nada do que sai para o Slack pode carregar valor de migração
# ---------------------------------------------------------------------------


def _result_from_findings(path: str, findings) -> dict:
    """Monta o resultado como o classificador determinístico vai montar."""
    return {
        "has_db_change": True,
        "highest_severity": max_severity(f.severity for f in findings).value,
        "items": [
            {
                "file": path,
                "severity": f.severity.value,
                "operation": f.operation,
                "reason": f.reason,
            }
            for f in findings
        ],
    }


class TestSlackTextNeverEchoesMigrationData:
    def test_unrecognised_insert_arrives_without_the_subject_data(self):
        # Migração de dados no consumidor Django/MySQL carrega CPF. Este vazamento já foi
        # encontrado e corrigido duas vezes dentro de `detect/`; a camada que
        # renderiza a mensagem não pode reintroduzi-lo.
        sql = "INSERT INTO subjects (name, cpf) VALUES ('Maria Silva', '12345678900');"
        findings = classify_sql(sql)
        assert findings
        text = build_slack_text(
            _result_from_findings("db/0007_backfill.sql", findings),
            "http://pr",
            "Backfill",
            "7",
            "dev",
        )
        assert "12345678900" not in text
        assert "Maria Silva" not in text

    def test_but_it_does_say_which_file_and_which_operation(self):
        sql = "INSERT INTO subjects (name, cpf) VALUES ('Maria Silva', '12345678900');"
        findings = classify_sql(sql)
        assert findings
        text = build_slack_text(
            _result_from_findings("db/0007_backfill.sql", findings),
            "http://pr",
            "Backfill",
            "7",
            "dev",
        )
        assert text.startswith("🟠 *Não classificado* — o classificador não entendeu")
        assert "• `db/0007_backfill.sql` — `INSERT`: " in text

    def test_bare_value_in_a_procedure_call_does_not_reach_slack_either(self):
        findings = classify_sql("CALL migrate_subject(12345678900);")
        assert findings
        text = build_slack_text(
            _result_from_findings("db/0008_call.sql", findings), "http://pr", "T", "8", "dev"
        )
        assert "12345678900" not in text
        assert "• `db/0008_call.sql` — `CALL`: " in text

    def test_a_real_drop_and_a_real_add_do_not_get_the_same_colour(self):
        # O sintoma que abriu esta entrega: DROP COLUMN e ADD COLUMN opcional
        # chegavam ao Slack com a mesma cor amarela.
        drop = classify_sql("ALTER TABLE orders DROP COLUMN qty;")
        add = classify_sql("ALTER TABLE orders ADD COLUMN note VARCHAR(50) NULL;")
        assert drop and add
        drop_text = build_slack_text(
            _result_from_findings("a.sql", drop), "http://pr", "T", "1", "dev"
        )
        add_text = build_slack_text(
            _result_from_findings("b.sql", add), "http://pr", "T", "2", "dev"
        )
        assert drop_text.splitlines()[0] != add_text.splitlines()[0]
        assert drop_text.startswith("🔴 *Breaking Change*")
        assert add_text.startswith("🟢 *Safe Change*")



# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_every_file_received_appears_in_items(self, tmp_path):
        # A regra que o prompt pedia ao modelo e que agora é código. Um arquivo
        # que some de `items` é indistinguível de um arquivo sem mudança de
        # banco, e essa confusão é a forma original do bug desta entrega.
        drop = tmp_path / "0001_drop.sql"
        drop.write_text("ALTER TABLE orders DROP COLUMN qty;")
        empty = tmp_path / "__init__.py"
        empty.write_text("")

        result = analyze([str(drop), str(empty)])

        assert {item["file"] for item in result["items"]} == {str(drop), str(empty)}

    def test_a_file_with_nothing_to_report_is_listed_as_none(self, tmp_path):
        empty = tmp_path / "__init__.py"
        empty.write_text("")
        result = analyze([str(empty)])
        assert result["items"] == [
            {
                "file": str(empty),
                "severity": "none",
                "operation": "",
                "reason": classify.NOTHING_TO_REPORT,
            }
        ]
        assert result["highest_severity"] == "none"
        assert result["has_db_change"] is False

    def test_highest_severity_is_the_worst_across_files(self, tmp_path):
        safe = tmp_path / "0001_add.sql"
        safe.write_text("ALTER TABLE orders ADD COLUMN note VARCHAR(50) NULL;")
        drop = tmp_path / "0002_drop.sql"
        drop.write_text("ALTER TABLE orders DROP COLUMN qty;")

        result = analyze([str(safe), str(drop)])

        assert result["highest_severity"] == "breaking"
        assert result["has_db_change"] is True
        # Pareado: a agregação não pode ter engolido o arquivo mais brando.
        assert {item["severity"] for item in result["items"]} == {"safe", "breaking"}

    def test_the_order_of_the_files_does_not_change_the_severity(self, tmp_path):
        safe = tmp_path / "0001_add.sql"
        safe.write_text("ALTER TABLE orders ADD COLUMN note VARCHAR(50) NULL;")
        drop = tmp_path / "0002_drop.sql"
        drop.write_text("ALTER TABLE orders DROP COLUMN qty;")

        forward = analyze([str(safe), str(drop)])["highest_severity"]
        backward = analyze([str(drop), str(safe)])["highest_severity"]
        assert forward == backward == "breaking"

    def test_every_item_carries_the_file_it_came_from(self, tmp_path):
        # `Finding` não tem caminho: parear finding com arquivo é trabalho
        # daqui. Dois arquivos com o mesmo statement provam que o pareamento
        # não é por acaso.
        a = tmp_path / "a.sql"
        a.write_text("ALTER TABLE orders DROP COLUMN qty;")
        b = tmp_path / "b.sql"
        b.write_text("ALTER TABLE clients DROP COLUMN cpf;")

        items = analyze([str(a), str(b)])["items"]

        by_file = {item["file"]: item["reason"] for item in items}
        assert "`orders`" in by_file[str(a)]
        assert "`clients`" in by_file[str(b)]

    def test_a_read_error_is_not_swallowed(self, tmp_path):
        with pytest.raises(OSError):
            analyze([str(tmp_path / "sumiu.sql")])

    def test_a_file_no_parser_reads_becomes_unknown_and_not_silence(self, tmp_path):
        # Um arquivo que casou `migration_paths` mas não tem parser não pode
        # sair como lista vazia, que a jusante lê como "nada a reportar".
        odd = tmp_path / "0001_migrate.rb"
        odd.write_text("puts 'oi'")
        result = analyze([str(odd)])
        assert result["highest_severity"] == "unknown"
        assert result["items"][0]["file"] == str(odd)


# ---------------------------------------------------------------------------
# O step não termina verde sem ter classificado
#
# Aqui roda o `classify.py` de verdade, em subprocesso, como o step do
# action.yml roda: sem mock nenhum, com GITHUB_OUTPUT em arquivo e com o código
# de saída do processo como asserção.
#
# O que mudou em QQ-2160: não há mais provedor a simular. O servidor HTTP local
# que respondia o protocolo da OpenAI saiu, e com ele o `set_content` que
# ditava a classificação. O gatilho de cada caso passou a ser o **conteúdo do
# arquivo de migração**, que é o que o classificador determinístico lê. As
# asserções — código de saída, outputs vazios, o que aparece no stderr — são as
# mesmas: é elas que provam que o step não termina verde sem ter classificado.
#
# Quatro dos sete casos existiam para falhas do provedor (resposta não-JSON,
# resposta sem severidade, severidade inventada, provedor fora do ar). Sem
# provedor, esses modos de falha não somem: eles mudam de lugar. O que os
# substitui é o mesmo perigo no caminho novo — dispatch que não decide,
# agregação sem severidade, severidade fora do vocabulário e o step chamado sem
# arquivo nenhum para classificar.
# ---------------------------------------------------------------------------


def _run_step(tmp_path, *, migration_files: str, unmatched_files: str = ""):
    """Executa o step como o action.yml executa. Devolve (processo, outputs)."""
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "MIGRATION_FILES": migration_files,
        "UNMATCHED_FILES": unmatched_files,
        "PR_URL": "http://example.com/pr/7",
        "PR_TITLE": "Backfill",
        "PR_NUMBER": "7",
        "PR_AUTHOR": "dev",
    }
    process = subprocess.run(
        [sys.executable, str(ACTION_DIR / "classify.py")],
        # De fora do diretório da action, como o step roda: o cwd do runner é o
        # workspace do repositório consumidor, não o `$GITHUB_ACTION_PATH`.
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return process, output.read_text()


def _run_step_with_analyze(tmp_path, result_literal: str):
    """Roda o `main()` de verdade com `analyze` trocado por um resultado fixo.

    Serve aos casos que precisam de um resultado malformado. `classify.py` roda
    como `__main__` no step, então trocar o atributo num `import classify` de
    fora não alcança o módulo que está executando: o driver importa o módulo e
    chama o `main()` dele, que é o mesmo `main()` do step.
    """
    output = tmp_path / "github_output"
    output.write_text("")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ACTION_DIR)!r})\n"
        "import classify\n"
        f"classify.analyze = lambda files, unmatched=(): {result_literal}\n"
        "classify.main()\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "MIGRATION_FILES": "qualquer.sql",
        "PR_URL": "http://example.com/pr/7",
        "PR_TITLE": "Backfill",
        "PR_NUMBER": "7",
        "PR_AUTHOR": "dev",
    }
    process = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    return process, output.read_text()


class TestStepNeverEndsGreenWithoutClassifying:
    def test_control_a_good_response_does_end_green_with_a_severity(self, tmp_path):
        # Controle positivo. Sem ele os testes abaixo passariam mesmo se o step
        # falhasse por qualquer motivo — inclusive por estar quebrado.
        migration = tmp_path / "0001_add.sql"
        migration.write_text("ALTER TABLE orders ADD COLUMN note VARCHAR(50) NULL;")
        process, outputs = _run_step(tmp_path, migration_files=str(migration))
        assert process.returncode == 0, process.stderr
        assert "highest_severity=safe\n" in outputs
        assert "confidence=1.0\n" in outputs

    def test_unknown_classification_pins_confidence_to_zero(self, tmp_path):
        migration = tmp_path / "0002_weird.sql"
        migration.write_text("CALL migrate_subject(12345678900);")
        process, outputs = _run_step(tmp_path, migration_files=str(migration))
        assert process.returncode == 0, process.stderr
        assert "highest_severity=unknown\n" in outputs
        assert "confidence=0.0\n" in outputs
        assert "🟠 *Não classificado*" in outputs

    def test_a_file_no_parser_reads_does_not_end_green(self, tmp_path):
        # Ocupa o lugar do "resposta não é JSON": o ponto do caminho novo onde
        # o classificador recebe algo que não sabe ler. Antes isso derrubava o
        # step; agora sai `unknown`, que é a resposta honesta e a que chega ao
        # time de dados — mas em hipótese nenhuma pode sair verde.
        migration = tmp_path / "0003_migrate.rb"
        migration.write_text("puts 'oi'")
        process, outputs = _run_step(tmp_path, migration_files=str(migration))
        assert process.returncode == 0, process.stderr
        assert "highest_severity=unknown\n" in outputs
        assert "confidence=0.0\n" in outputs
        assert "highest_severity=none\n" not in outputs
        assert "highest_severity=safe\n" not in outputs

    def test_a_python_file_that_does_not_parse_does_not_end_green(self, tmp_path):
        # O "erro de parsing" do caminho novo: a fonte não é Python válido, e o
        # dispatch não consegue nem escolher entre Django e Alembic.
        migration = tmp_path / "0004_broken.py"
        migration.write_text("class Migration(  :::")
        process, outputs = _run_step(tmp_path, migration_files=str(migration))
        assert process.returncode == 0, process.stderr
        assert "highest_severity=unknown\n" in outputs
        assert "confidence=0.0\n" in outputs
        assert "highest_severity=none\n" not in outputs

    def test_the_step_called_with_no_file_brings_the_step_down(self, tmp_path):
        # Ocupa o lugar do "provedor fora do ar": o step foi chamado para
        # classificar e não tem o que classificar. A coleta disse que havia
        # arquivos; se nenhum chegou aqui, os dois discordam e a única saída
        # possível seria publicar `none` — um verde que ninguém classificou.
        process, outputs = _run_step(tmp_path, migration_files="")
        assert process.returncode != 0
        assert outputs == ""
        assert "MIGRATION_FILES" in process.stderr
        assert "controlled" not in outputs

    def test_unreadable_migration_file_brings_the_step_down(self, tmp_path):
        process, outputs = _run_step(
            tmp_path, migration_files=str(tmp_path / "sumiu.sql")
        )
        assert process.returncode != 0
        assert outputs == ""
        assert "sumiu.sql" in process.stderr

    def test_a_severity_outside_the_vocabulary_brings_the_step_down(self, tmp_path):
        # Ocupa o lugar do "severidade inventada". O gatilho não vem mais de
        # fora, então ele é injetado onde a severidade nasce: um parser que
        # devolvesse severidade fora do vocabulário não pode virar output.
        process, outputs = _run_step_with_analyze(
            tmp_path,
            "{'has_db_change': True, 'highest_severity': 'talvez', 'items': []}",
        )
        assert process.returncode != 0
        assert outputs == ""
        assert "severidade reconhecida" in process.stderr

    def test_a_result_without_a_severity_brings_the_step_down(self, tmp_path):
        # Ocupa o lugar do "resposta sem severidade": a chave que falta. Era o
        # último lugar onde um resultado incompleto virava um alerta verde.
        process, outputs = _run_step_with_analyze(
            tmp_path, "{'has_db_change': True, 'items': []}"
        )
        assert process.returncode != 0
        assert outputs == ""
        assert "severidade reconhecida" in process.stderr

    def test_the_injection_itself_is_not_what_breaks_the_step(self, tmp_path):
        # Controle pareado dos dois acima: com um resultado bem formado, o mesmo
        # driver termina verde. Sem ele, os dois passariam mesmo se o
        # `_run_step_with_analyze` estivesse quebrado por qualquer outro motivo.
        process, outputs = _run_step_with_analyze(
            tmp_path,
            "{'has_db_change': True, 'highest_severity': 'breaking', 'items': []}",
        )
        assert process.returncode == 0, process.stderr
        assert "highest_severity=breaking\n" in outputs

    def test_no_output_is_written_before_the_severity_is_validated(self):
        # O que amarra os dois acima: se algum output fosse escrito antes da
        # validação, `outputs == ""` passaria a ser falso e os dois casos
        # perderiam o dente sem que ninguém percebesse.
        #
        # Por número de linha, e não pela ordem em que `ast.walk` devolve os
        # nós: `walk` é em largura, então ele ordena por profundidade antes de
        # ordenar por posição, e uma reescrita que deixasse `severity_of` mais
        # raso passaria mesmo chamando-o depois.
        source = (ACTION_DIR / "classify.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        lines = {
            node.func.id: node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "severity_of" in lines and "write_github_outputs" in lines
        assert lines["severity_of"] < lines["write_github_outputs"]




# ---------------------------------------------------------------------------
# O contrato do action.yml com os três consumidores
#
# `update-semver-tags-on-release.yml` move o `v3` a cada release e os três
# consumidores fixam `@v3`: release publicada é deploy imediato. O que este
# bloco protege é o que quebraria os três sem ninguém rodar nada antes.
# ---------------------------------------------------------------------------


def _action_inputs() -> dict[str, str]:
    """Nome do input -> o bloco de texto que o declara, do `action.yml`.

    Sem PyYAML: o venv de teste não o tem e a action não precisa dele. Devolver
    o bloco inteiro, e não pares chave/valor, é o que faz `description: >`
    funcionar — o texto da descrição mora nas linhas seguintes, não na mesma.
    """
    text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
    blocks: dict[str, list[str]] = {}
    current = None
    inside = False
    for line in text.splitlines():
        if line.startswith("inputs:"):
            inside = True
            continue
        if inside and line and not line[0].isspace():
            break  # começou o próximo bloco de topo (`outputs:`, `runs:`)
        if not inside:
            continue
        indent = len(line) - len(line.lstrip())
        if line.strip() and indent == 2 and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def _action_run_lines() -> str:
    """As linhas do `action.yml` que não são comentário.

    Um guarda que procura `pip install` no arquivo inteiro casa o comentário que
    explica por que o `pip install` saiu.
    """
    return "\n".join(
        line
        for line in (ACTION_DIR / "action.yml").read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


class TestActionContract:
    def test_the_parser_actually_found_the_inputs(self):
        # Sem isto, todo teste abaixo passa num dicionário vazio.
        inputs = _action_inputs()
        assert len(inputs) >= 9
        assert "slack_webhook_url" in inputs
        assert "DEPRECADO" in inputs["ai_api_key"]

    @pytest.mark.parametrize(
        "name", ("ai_api_url", "ai_api_key", "ai_model", "minimum_confidence")
    )
    def test_the_deprecated_inputs_are_still_declared(self, name):
        # Inertes, mas declarados: um consumidor fora dos três que ainda os
        # passe recebe aviso, não erro de input desconhecido.
        assert name in _action_inputs()

    @pytest.mark.parametrize(
        "name", ("ai_api_url", "ai_api_key", "ai_model", "minimum_confidence")
    )
    def test_the_deprecated_inputs_say_they_are_deprecated(self, name):
        assert "DEPRECADO" in _action_inputs()[name]

    @pytest.mark.parametrize(
        "name", ("ai_api_url", "ai_api_key", "ai_model", "minimum_confidence")
    )
    def test_no_deprecated_input_became_required(self, name):
        assert "required: false" in _action_inputs()[name]

    def test_no_new_required_input(self):
        # O aceite: os três workflows consumidores passam `github_token`,
        # `slack_webhook_url` e `slack_channel` e mais nada. Um input novo
        # obrigatório reprovaria os três na primeira release.
        required = {n for n, b in _action_inputs().items() if "required: true" in b}
        assert required == {"github_token", "slack_webhook_url"}

    def test_the_inputs_the_three_consumers_pass_are_all_declared(self):
        declared = set(_action_inputs())
        assert {"github_token", "slack_webhook_url", "slack_channel"} <= declared

    @pytest.mark.parametrize(
        "step",
        (
            "Setup | Compute requirements hash",
            "Setup | Cache pip",
            "Setup | Install openai SDK",
        ),
    )
    def test_the_pip_steps_are_gone(self, step):
        # O aceite: `pip install` sai do caminho de execução.
        assert step not in (ACTION_DIR / "action.yml").read_text(encoding="utf-8")

    def test_no_pip_install_survives_outside_a_comment(self):
        # O comentário que explica a remoção cita `pip install` de propósito; o
        # que não pode sobrar é um step que o execute.
        assert "pip install" not in _action_run_lines()

    def test_a_pr_that_only_removes_a_migration_does_not_break_the_step(self):
        """`git diff --name-only` lista o arquivo removido, o classificador
        tenta abri-lo e o step morre sem `except` — e o step do Slack nem roda,
        porque step seguinte não roda depois de step falho. O check fica
        vermelho e o time de dados não recebe nada."""
        assert "--diff-filter=d" in _action_run_lines()

    def test_a_migration_outside_the_glob_leaves_a_trace(self):
        """A regra que a ADR põe na lista do que não é negociável: arquivo com
        cara de migração que nenhum glob casou vira `::warning::` e entra na
        mensagem do Slack. Sem ela, "o PR não tem migração" e "o glob não pegou
        a migração" continuam indistinguíveis."""
        run = _action_run_lines()
        assert "unmatched_files" in run
        assert "alembic/versions" in run
        assert "UNMATCHED_FILES" in run

    def test_the_slack_step_runs_for_a_glob_miss_too(self):
        """O aviso que só existe no log do job é o mesmo silêncio de antes."""
        run = _action_run_lines()
        slack = run[run.index("Notify | Post to Slack"):]
        condition = slack[: slack.index("shell:")]
        assert "unmatched" in condition

    def test_setup_python_survives(self):
        # O classificador é Python: este step fica.
        assert "actions/setup-python" in (ACTION_DIR / "action.yml").read_text(encoding="utf-8")

    def test_requirements_declares_no_dependency(self):
        content = (ACTION_DIR / "requirements.txt").read_text(encoding="utf-8")
        packages = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert packages == []

    def test_the_endpoint_of_the_retired_provider_is_gone(self):
        # A URL é configuração de verdade: se ela estiver em qualquer lugar do
        # arquivo, alguém ainda pode apontar para lá.
        text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
        assert "models.inference.ai.azure.com" not in text

    def test_no_step_mentions_github_models(self):
        # O bloco `runs:` é onde um provedor seria configurado ou descrito como
        # primário — era lá que morava o comentário "Provedor primário: GitHub
        # Models API". Nome de step, comentário, `env` e script, todos.
        #
        # A asserção que estava aqui era `"GitHub Models" not in text` sobre o
        # arquivo inteiro, e ela passou a estar errada: a descrição de
        # `github_token` explica, no passado, que ele *era* a credencial da
        # GitHub Models API. Proibir a menção proibia justamente a frase que diz
        # ao consumidor por que o input morreu.
        text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
        runs = text[text.index("\nruns:"):]
        assert "GitHub Models" not in runs

    def test_github_models_survives_only_as_history_in_a_deprecated_input(self):
        # Pareado com o de cima: a menção só é aceitável dentro de um input que
        # se declara morto. Num input vivo, ela voltaria a ser instrução.
        for name, block in _action_inputs().items():
            if "GitHub Models" in block:
                assert "DEPRECADO" in block, name


# ---------------------------------------------------------------------------
# O entrypoint do step
# ---------------------------------------------------------------------------


class TestMainIsTheStep:
    """`main()` — a única função do arquivo que nenhum teste exercitava.

    Ela é o caminho de falha inteiro. O `except Exception` que morava aqui é o
    que deixou a GitHub Models API responder 404 por quatro semanas com o job
    verde; o que substituiu não tem rede, mas continua tendo três jeitos de
    terminar sem classificação, e os três têm que derrubar o step em vez de
    publicar um `none`.
    """

    @staticmethod
    def _env(monkeypatch, **values):
        for key in (
            "MIGRATION_FILES",
            "GITHUB_OUTPUT",
            "PR_URL",
            "PR_TITLE",
            "PR_NUMBER",
            "PR_AUTHOR",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    # --- o caminho feliz, para os outros terem contra o que contrastar ------

    def test_main_writes_every_output_the_action_declares(self, monkeypatch, tmp_path):
        migration = tmp_path / "0001_drop.sql"
        migration.write_text("ALTER TABLE ledger DROP COLUMN memo;", encoding="utf-8")
        output = tmp_path / "gh_output"
        output.write_text("", encoding="utf-8")

        self._env(
            monkeypatch,
            MIGRATION_FILES=str(migration),
            GITHUB_OUTPUT=str(output),
            PR_URL="https://github.com/iclinic/x/pull/7",
            PR_TITLE="drop memo",
            PR_NUMBER="7",
            PR_AUTHOR="alguem",
        )
        classify.main()

        written = output.read_text(encoding="utf-8")
        assert "has_db_change=true" in written
        assert "highest_severity=breaking" in written
        assert "confidence=1.0" in written
        payload = json.loads(
            written.split("analysis_json=", 1)[1].splitlines()[0]
        )
        assert [item["operation"] for item in payload["items"]] == ["DROP COLUMN"]
        assert payload["items"][0]["reason"] == (
            "Coluna `memo` removida de `ledger` — quem lê essa coluna quebra."
        )

    def test_main_runs_without_github_output_and_writes_nothing(
        self, monkeypatch, tmp_path
    ):
        migration = tmp_path / "0001_add.sql"
        migration.write_text("ALTER TABLE ledger ADD COLUMN memo varchar(8) NULL;", encoding="utf-8")
        self._env(monkeypatch, MIGRATION_FILES=str(migration))
        classify.main()  # não escreve output nenhum, e não estoura

    # --- os três jeitos de terminar sem classificação -----------------------

    def test_no_files_brings_the_step_down(self, monkeypatch):
        self._env(monkeypatch, MIGRATION_FILES="   |  |")
        with pytest.raises(SystemExit) as exc:
            classify.main()
        assert "MIGRATION_FILES vazia" in str(exc.value)

    def test_a_file_that_does_not_open_brings_the_step_down(self, monkeypatch, tmp_path):
        """Leitura que falha sobe. Arquivo que não abre não vira classificação."""
        self._env(monkeypatch, MIGRATION_FILES=str(tmp_path / "nao_existe.py"))
        with pytest.raises(FileNotFoundError):
            classify.main()

    def test_a_severity_outside_the_vocabulary_brings_the_step_down(
        self, monkeypatch, tmp_path
    ):
        migration = tmp_path / "0001_add.sql"
        migration.write_text("ALTER TABLE ledger ADD COLUMN memo varchar(8) NULL;", encoding="utf-8")
        self._env(monkeypatch, MIGRATION_FILES=str(migration))

        def broken(files, unmatched=()):
            return {"has_db_change": True, "highest_severity": "amarelo", "items": []}

        monkeypatch.setattr(classify, "analyze", broken)
        with pytest.raises(SystemExit) as exc:
            classify.main()
        assert "sem severidade reconhecida" in str(exc.value)

    # --- parsing quebrado ---------------------------------------------------

    def test_a_migration_that_does_not_parse_is_published_as_unknown(
        self, monkeypatch, tmp_path
    ):
        """Fonte quebrada não estoura o step: ela sai `unknown`, com o motivo.

        É a distinção que a entrega inteira existe para manter. O que derruba o
        step é não conseguir *classificar* — arquivo que não abre, severidade
        fora do vocabulário. Um arquivo que não parseia o classificador
        consegue classificar: ele não sabe o que a migração faz, e dizer isso é
        a resposta certa. Publicar `unknown` põe a migração na frente de um
        humano; estourar aqui derrubaria o PR inteiro por causa de um arquivo.
        """
        migration = tmp_path / "0001_quebrada.py"
        migration.write_text(
            "from django.db import migrations\n\n\nclass Migration(migrations.Migration\n",
            encoding="utf-8",
        )
        output = tmp_path / "gh_output"
        output.write_text("", encoding="utf-8")
        self._env(
            monkeypatch,
            MIGRATION_FILES=str(migration),
            GITHUB_OUTPUT=str(output),
            PR_NUMBER="9",
        )
        classify.main()

        written = output.read_text(encoding="utf-8")
        assert "highest_severity=unknown" in written
        assert "has_db_change=true" in written
        assert "confidence=0.0" in written
        payload = json.loads(written.split("analysis_json=", 1)[1].splitlines()[0])
        assert payload["items"][0]["reason"].startswith("Arquivo não é Python válido")

    def test_the_broken_file_is_named_in_the_slack_text(self, monkeypatch, tmp_path):
        """Um `unknown` que não diz em qual arquivo não serve para quem lê."""
        migration = tmp_path / "0002_quebrada.py"
        migration.write_text("class Migration(migrations.Migration\n", encoding="utf-8")
        output = tmp_path / "gh_output"
        output.write_text("", encoding="utf-8")
        self._env(
            monkeypatch,
            MIGRATION_FILES=str(migration),
            GITHUB_OUTPUT=str(output),
            PR_URL="https://github.com/iclinic/x/pull/9",
            PR_NUMBER="9",
            PR_TITLE="uma migração",
            PR_AUTHOR="alguem",
        )
        classify.main()

        written = output.read_text(encoding="utf-8")
        slack = written.split("slack_text<<MIGRATION_DETECTOR_EOF\n", 1)[1].split(
            "\nMIGRATION_DETECTOR_EOF", 1
        )[0]
        assert "0002_quebrada.py" in slack
        assert "Arquivo não é Python válido" in slack
        assert presentation(Severity.UNKNOWN).label in slack
