import json
import os

COLOR_MAP = {"safe": "good", "controlled": "warning", "breaking": "danger"}
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
