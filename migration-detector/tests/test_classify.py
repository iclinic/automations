import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from classify import (
    apply_confidence_threshold,
    build_context_block,
    build_slack_text,
    build_user_prompt,
    make_fallback_result,
    parse_ai_response,
    read_migration_files,
    resolve_credentials,
    write_github_outputs,
    SEVERITY_META,
)

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


def _make_ai_response(result: dict) -> MagicMock:
    """Cria um mock de resposta da OpenAI a partir de um dict."""
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = json.dumps(result)
    return mock_resp


# ---------------------------------------------------------------------------
# resolve_credentials
# ---------------------------------------------------------------------------


class TestResolveCredentials:
    def test_uses_ai_api_key_when_provided(self):
        key, using_gh = resolve_credentials("sk-external", "ghp-token")
        assert key == "sk-external"
        assert using_gh is False

    def test_falls_back_to_github_token_when_ai_key_empty(self):
        key, using_gh = resolve_credentials("", "ghp-token")
        assert key == "ghp-token"
        assert using_gh is True

    def test_both_empty_returns_empty_key(self):
        key, using_gh = resolve_credentials("", "")
        assert key == ""
        assert using_gh is True


# ---------------------------------------------------------------------------
# read_migration_files
# ---------------------------------------------------------------------------


class TestReadMigrationFiles:
    def test_reads_existing_file(self, tmp_path):
        f = tmp_path / "migration.sql"
        f.write_text("ALTER TABLE t ADD COLUMN x INT;")
        result = read_migration_files([str(f)])
        assert str(f) in result
        assert "ALTER TABLE" in result[str(f)]

    def test_truncates_to_max_bytes(self, tmp_path):
        f = tmp_path / "large.sql"
        f.write_text("x" * 10_000)
        result = read_migration_files([str(f)])
        assert len(result[str(f)]) == 6000

    def test_missing_file_returns_error_message(self):
        result = read_migration_files(["/nonexistent/file.sql"])
        assert "[Erro ao ler arquivo:" in result["/nonexistent/file.sql"]

    def test_empty_list_returns_empty_dict(self):
        assert read_migration_files([]) == {}

    def test_multiple_files(self, tmp_path):
        a = tmp_path / "a.sql"
        b = tmp_path / "b.sql"
        a.write_text("content-a")
        b.write_text("content-b")
        result = read_migration_files([str(a), str(b)])
        assert result[str(a)] == "content-a"
        assert result[str(b)] == "content-b"


# ---------------------------------------------------------------------------
# build_context_block
# ---------------------------------------------------------------------------


class TestBuildContextBlock:
    def test_single_file(self):
        block = build_context_block({"path/file.sql": "SELECT 1"})
        assert "=== path/file.sql ===" in block
        assert "SELECT 1" in block

    def test_multiple_files_separated_by_blank_line(self):
        block = build_context_block({"a.sql": "AAA", "b.sql": "BBB"})
        assert "=== a.sql ===" in block
        assert "=== b.sql ===" in block
        assert "\n\n" in block

    def test_empty_dict_returns_empty_string(self):
        assert build_context_block({}) == ""


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_contains_pr_metadata(self):
        prompt = build_user_prompt("ctx", "42", "Add index", "org/repo")
        assert "PR #42" in prompt
        assert "Add index" in prompt
        assert "org/repo" in prompt

    def test_contains_context_block(self):
        prompt = build_user_prompt("MY_CONTEXT", "1", "title", "repo")
        assert "MY_CONTEXT" in prompt


# ---------------------------------------------------------------------------
# parse_ai_response
# ---------------------------------------------------------------------------


class TestParseAiResponse:
    def test_plain_json(self):
        raw = json.dumps({"has_db_change": True})
        assert parse_ai_response(raw) == {"has_db_change": True}

    def test_strips_markdown_code_block(self):
        raw = "```\n" + json.dumps({"key": "val"}) + "\n```"
        assert parse_ai_response(raw) == {"key": "val"}

    def test_strips_markdown_json_block(self):
        raw = "```json\n" + json.dumps({"key": "val"}) + "\n```"
        assert parse_ai_response(raw) == {"key": "val"}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_ai_response("not-json")


# ---------------------------------------------------------------------------
# make_fallback_result
# ---------------------------------------------------------------------------


class TestMakeFallbackResult:
    def test_structure(self):
        result = make_fallback_result(["a.sql", "b.sql"])
        assert result["has_db_change"] is True
        assert result["highest_severity"] == "controlled"
        assert result["confidence"] == 0.0
        assert len(result["items"]) == 2

    def test_each_item_has_controlled_severity(self):
        result = make_fallback_result(["x.sql"])
        assert result["items"][0]["severity"] == "controlled"
        assert result["items"][0]["file"] == "x.sql"

    def test_empty_files_list(self):
        result = make_fallback_result([])
        assert result["items"] == []


# ---------------------------------------------------------------------------
# apply_confidence_threshold
# ---------------------------------------------------------------------------


class TestApplyConfidenceThreshold:
    def test_safe_below_threshold_promoted_to_controlled(self):
        result = {"highest_severity": "safe", "confidence": 0.5}
        out = apply_confidence_threshold(result, min_conf=0.70)
        assert out["highest_severity"] == "controlled"

    def test_safe_above_threshold_unchanged(self):
        result = {"highest_severity": "safe", "confidence": 0.9}
        out = apply_confidence_threshold(result, min_conf=0.70)
        assert out["highest_severity"] == "safe"

    def test_breaking_below_threshold_not_changed(self):
        result = {"highest_severity": "breaking", "confidence": 0.3}
        out = apply_confidence_threshold(result, min_conf=0.70)
        assert out["highest_severity"] == "breaking"

    def test_controlled_below_threshold_not_changed(self):
        result = {"highest_severity": "controlled", "confidence": 0.3}
        out = apply_confidence_threshold(result, min_conf=0.70)
        assert out["highest_severity"] == "controlled"

    def test_original_dict_not_mutated(self):
        result = {"highest_severity": "safe", "confidence": 0.5}
        out = apply_confidence_threshold(result, min_conf=0.70)
        assert result["highest_severity"] == "safe"  # original intacto
        assert out is not result

    def test_missing_confidence_defaults_to_1(self):
        result = {"highest_severity": "safe"}
        out = apply_confidence_threshold(result, min_conf=0.70)
        # confiança = 1.0 >= 0.70  → sem promoção
        assert out["highest_severity"] == "safe"


# ---------------------------------------------------------------------------
# build_slack_text
# ---------------------------------------------------------------------------


class TestBuildSlackText:
    def test_breaking_uses_red_emoji(self):
        result = {**SAMPLE_RESULT, "highest_severity": "breaking"}
        text = build_slack_text(result, "http://pr", "Title", "99", "author")
        assert "🔴" in text

    def test_safe_uses_green_emoji(self):
        result = {**SAMPLE_RESULT, "highest_severity": "safe"}
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "🟢" in text

    def test_controlled_uses_yellow_emoji(self):
        result = {**SAMPLE_RESULT, "highest_severity": "controlled"}
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "🟡" in text

    def test_unknown_severity_uses_default_emoji(self):
        result = {**SAMPLE_RESULT, "highest_severity": "unknown_sev"}
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "⚪" in text

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
            "items": [{"severity": "breaking", "reason": "Remoção do campo x"}],
        }
        text = build_slack_text(result, "http://pr", "Title", "1", "author")
        assert "Remoção do campo x" in text

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
