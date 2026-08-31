import json
import os

from detect.severity import SEVERITY_META

# A cor do anexo vem da mesma tabela que o emoji e o rótulo da mensagem. Antes
# eram duas tabelas: esta não tinha `none` nem `unknown`, e as duas caíam no
# cinza de `DEFAULT_COLOR` — um `DROP COLUMN` que o classificador não entendeu
# chegava ao Slack com a cor de "nada demais".
COLOR_MAP = {severity.value: meta.color for severity, meta in SEVERITY_META.items()}

# Só sobra para severidade fora da tabela, que o `classify.py` já não deixa
# passar. Nenhuma linha da tabela usa este cinza, então ele nunca disfarça uma
# severidade real de mudança de schema.
DEFAULT_COLOR = "#cccccc"


def build_payload(text: str, channel: str, severity: str) -> dict:
    return {
        "username": "Migration Detector",
        "icon_emoji": ":floppy_disk:",
        **({"channel": channel} if channel else {}),
        "attachments": [
            {
                "color": COLOR_MAP.get(severity, DEFAULT_COLOR),
                "text": text,
                "mrkdwn_in": ["text"],
                "footer": "Migration Detector · iclinic/automations",
                "footer_icon": "https://github.githubassets.com/favicons/favicon.png",
            }
        ],
    }


def main() -> None:
    text = os.environ.get("SLACK_TEXT", "")
    channel = os.environ.get("SLACK_CHANNEL", "").strip()
    severity = os.environ.get("HIGHEST_SEVERITY", "none")

    print(json.dumps(build_payload(text, channel, severity)))


if __name__ == "__main__":
    main()
