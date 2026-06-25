"""
Telegram alerting for the weather-anomaly pipeline.

The third tool (send_telegram_alert) uses this to push a notification when
anomalies are detected. Deliberately built on the same stdlib ``urllib`` +
``certifi`` idiom as the rest of the server (see ``weather_client.py``) so it adds
**no new dependency**.

Configuration is via two env vars, read at call time (12-factor, Cloud Run /
Secret Manager friendly):

    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat/channel id to deliver to

When either is missing the sender is a safe no-op that reports ``not configured``
rather than raising — and the token value is never logged or returned.

Kept free of MCP imports so it is unit-testable on its own.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("weather_digest.telegram")

try:
    import certifi

    _SSL_CTX: ssl.SSLContext | None = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - environment-dependent
    _SSL_CTX = None

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_HTTP_TIMEOUT_S = 6


def is_configured() -> bool:
    """True when both the bot token and chat id are present in the environment."""
    return bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )


def format_alert(report: dict) -> str:
    """Render an anomaly report (from detect_weather_anomalies) into alert text."""
    city = report.get("city", "unknown")
    period = report.get("period", "")
    lines = [f"⚠️ Weather alert for {city} (last {period})"]
    for item in report.get("anomalies", []):
        sev = str(item.get("severity", "")).upper()
        lines.append(f"• [{sev}] {item.get('type')}: {item.get('detail')}")
    lines.append("")
    lines.append(report.get("summary", ""))
    return "\n".join(line for line in lines if line is not None)


def format_all_clear(report: dict) -> str:
    """Render the reassuring 'no anomalies' message (notify_when_clear=True)."""
    city = report.get("city", "unknown")
    period = report.get("period", "")
    return f"✅ All clear for {city} (last {period}) — no unusual weather detected."


def send_message(text: str) -> dict:
    """POST ``text`` to the configured Telegram chat.

    Returns a small result dict (never raises): ``{"ok": True}`` on success,
    ``{"ok": False, "reason": ...}`` when unconfigured or on any network/API
    failure. The bot token is never included in the result or the logs.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"ok": False, "reason": "not configured"}

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        _API_URL.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT_S, context=_SSL_CTX) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            logger.info("telegram alert delivered to chat %s", chat_id)
            return {"ok": True}
        # Telegram replied but rejected the send (e.g. bad chat_id). Don't leak token.
        return {"ok": False, "reason": f"telegram API error: {body.get('description', 'unknown')}"}
    except (URLError, OSError, ValueError) as exc:
        logger.warning("telegram send failed: %s", exc)
        return {"ok": False, "reason": f"telegram error: {exc}"}
