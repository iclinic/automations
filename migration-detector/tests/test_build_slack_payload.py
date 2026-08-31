import json
import pathlib
import subprocess
import sys

import pytest

from build_slack_payload import (
    DEFAULT_COLOR,
    COLOR_MAP,
    build_payload,
    main,
)
from detect.severity import SEVERITY_META, Severity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ATTACHMENT(payload):
    return payload["attachments"][0]


# ---------------------------------------------------------------------------
# build_payload — estrutura base
# ---------------------------------------------------------------------------


class TestPayloadStructure:
    def test_username(self):
        payload = build_payload("msg", "", "safe")
        assert payload["username"] == "Migration Detector"

    def test_icon_emoji(self):
        payload = build_payload("msg", "", "safe")
        assert payload["icon_emoji"] == ":floppy_disk:"

    def test_has_one_attachment(self):
        payload = build_payload("msg", "", "safe")
        assert len(payload["attachments"]) == 1

    def test_attachment_text(self):
        payload = build_payload("hello world", "", "safe")
        assert ATTACHMENT(payload)["text"] == "hello world"

    def test_attachment_mrkdwn_in(self):
        payload = build_payload("msg", "", "safe")
        assert ATTACHMENT(payload)["mrkdwn_in"] == ["text"]

    def test_attachment_footer(self):
        payload = build_payload("msg", "", "safe")
        assert ATTACHMENT(payload)["footer"] == "Migration Detector · iclinic/automations"

    def test_attachment_footer_icon(self):
        payload = build_payload("msg", "", "safe")
        assert ATTACHMENT(payload)["footer_icon"] == (
            "https://github.githubassets.com/favicons/favicon.png"
        )


# ---------------------------------------------------------------------------
# build_payload — mapeamento de severidade para cor
# ---------------------------------------------------------------------------


class TestSeverityColor:
    @pytest.mark.parametrize("severity,expected_color", COLOR_MAP.items())
    def test_known_severities(self, severity, expected_color):
        payload = build_payload("msg", "", severity)
        assert ATTACHMENT(payload)["color"] == expected_color

    def test_color_map_covers_the_whole_vocabulary(self):
        # A tabela de antes só tinha safe/controlled/breaking. `none` e
        # `unknown` caíam no cinza do default, que é a cor de "nada demais".
        assert set(COLOR_MAP) == {s.value for s in Severity}

    def test_color_map_is_the_shared_table_and_not_a_second_copy(self):
        assert COLOR_MAP == {s.value: meta.color for s, meta in SEVERITY_META.items()}

    @pytest.mark.parametrize("severity", [s.value for s in Severity])
    def test_no_severity_in_the_vocabulary_falls_back_to_the_default_color(self, severity):
        # O teste que existia aqui afirmava que `unknown` recebia DEFAULT_COLOR,
        # que é exatamente o bug: a asserção era igual ao default e passaria com
        # ou sem a feature.
        assert ATTACHMENT(build_payload("msg", "", severity))["color"] != DEFAULT_COLOR

    def test_unknown_has_the_color_of_its_row(self):
        payload = build_payload("msg", "", Severity.UNKNOWN.value)
        assert ATTACHMENT(payload)["color"] == SEVERITY_META[Severity.UNKNOWN].color

    def test_unknown_is_not_painted_like_a_benign_severity(self):
        unknown = ATTACHMENT(build_payload("m", "", "unknown"))["color"]
        assert unknown != ATTACHMENT(build_payload("m", "", "safe"))["color"]
        assert unknown != ATTACHMENT(build_payload("m", "", "controlled"))["color"]
        assert unknown != ATTACHMENT(build_payload("m", "", "none"))["color"]

    def test_empty_severity_uses_default_color(self):
        payload = build_payload("msg", "", "")
        assert ATTACHMENT(payload)["color"] == DEFAULT_COLOR

    def test_severity_outside_the_vocabulary_uses_default_color(self):
        payload = build_payload("msg", "", "catastrophic")
        assert ATTACHMENT(payload)["color"] == DEFAULT_COLOR


# ---------------------------------------------------------------------------
# build_payload — campo channel
# ---------------------------------------------------------------------------


class TestChannel:
    def test_channel_included_when_provided(self):
        payload = build_payload("msg", "#data-alerts", "safe")
        assert payload["channel"] == "#data-alerts"

    def test_channel_omitted_when_empty_string(self):
        payload = build_payload("msg", "", "safe")
        assert "channel" not in payload

    def test_channel_omitted_when_whitespace_only(self):
        # main() aplica .strip() antes de chamar build_payload;
        # build_payload recebe string vazia após strip, portanto omite channel.
        payload = build_payload("msg", "   ".strip(), "safe")
        assert "channel" not in payload


# ---------------------------------------------------------------------------
# build_payload — texto
# ---------------------------------------------------------------------------


class TestText:
    def test_empty_text(self):
        payload = build_payload("", "", "safe")
        assert ATTACHMENT(payload)["text"] == ""

    def test_text_with_slack_markdown(self):
        text = "*bold* and _italic_ and <https://example.com|link>"
        payload = build_payload(text, "", "safe")
        assert ATTACHMENT(payload)["text"] == text

    def test_text_with_special_characters(self):
        text = 'DROP TABLE users; -- "dangerous"'
        payload = build_payload(text, "", "breaking")
        assert ATTACHMENT(payload)["text"] == text


# ---------------------------------------------------------------------------
# main() — leitura de variáveis de ambiente
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_reads_env_and_prints_json(self, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_TEXT", "migration detected")
        monkeypatch.setenv("SLACK_CHANNEL", "#data")
        monkeypatch.setenv("HIGHEST_SEVERITY", "breaking")

        main()

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["channel"] == "#data"
        assert ATTACHMENT(payload)["text"] == "migration detected"
        assert ATTACHMENT(payload)["color"] == COLOR_MAP["breaking"]

    def test_main_defaults_when_env_absent(self, monkeypatch, capsys):
        monkeypatch.delenv("SLACK_TEXT", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL", raising=False)
        monkeypatch.delenv("HIGHEST_SEVERITY", raising=False)

        main()

        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert ATTACHMENT(payload)["text"] == ""
        assert "channel" not in payload
        # Sem HIGHEST_SEVERITY, `main()` lê "none" — que agora tem linha própria.
        assert ATTACHMENT(payload)["color"] == COLOR_MAP["none"]
        assert ATTACHMENT(payload)["color"] != DEFAULT_COLOR

    def test_main_strips_channel_whitespace(self, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_TEXT", "")
        monkeypatch.setenv("SLACK_CHANNEL", "  #data  ")
        monkeypatch.setenv("HIGHEST_SEVERITY", "safe")

        main()

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["channel"] == "#data"

    def test_main_output_is_valid_json(self, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_TEXT", "any")
        monkeypatch.setenv("SLACK_CHANNEL", "")
        monkeypatch.setenv("HIGHEST_SEVERITY", "controlled")

        main()

        captured = capsys.readouterr()
        # não deve lançar exceção
        parsed = json.loads(captured.out)
        assert isinstance(parsed, dict)


class TestRunsFromTheConsumerWorkspace:
    def test_script_imports_the_shared_table_from_a_foreign_cwd(self, tmp_path):
        # O step roda `python3 "$GITHUB_ACTION_PATH/build_slack_payload.py"` com
        # o cwd no repositório consumidor. Se `detect/` deixar de resolver daí,
        # o Slack para de receber mensagem e só o job vê o erro.
        script = pathlib.Path(__file__).resolve().parent.parent / "build_slack_payload.py"
        process = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            env={"SLACK_TEXT": "oi", "SLACK_CHANNEL": "#data", "HIGHEST_SEVERITY": "unknown"},
            timeout=60,
        )
        assert process.returncode == 0, process.stderr
        payload = json.loads(process.stdout)
        assert ATTACHMENT(payload)["color"] == COLOR_MAP["unknown"]
        assert ATTACHMENT(payload)["color"] != DEFAULT_COLOR
