"""Eigener Zusatz (nicht Teil des Original-Skill-Pakets): schickt Order-
Ereignisse per Telegram statt nur auf stdout, damit der isolierte Test-
Container genauso wie das Hauptsystem Bescheid gibt, ohne eine aktive
Claude-Session vorauszusetzen. Nutzt denselben Bot/Chat wie
range_trader_django (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID als Env-Var,
keine DB-Abhaengigkeit hier)."""
import os
import requests


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[Telegram] Fehler: {e}")
        return False
