"""
alerts.py — Telegram push for high-score signals + healthcheck heartbeat.
Both are best-effort and never raise into the scan loop.
"""

from __future__ import annotations

import os
import logging
import requests

log = logging.getLogger("pmfade.alerts")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT_ID", "")

_EMOJI = {"news_fade": "📰", "settlement_lag": "⏳",
          "longshot_bias": "🎯", "correlated_lag": "🔗"}


def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        log.info("[telegram not configured]\n%s", text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram failed: %s", e)
        return False


def format_signal(row) -> str:
    emoji = _EMOJI.get(row["strategy_id"], "•")
    q = row["question"][:90] + ("…" if len(row["question"] or "") > 90 else "")
    url_tag = f'\n<a href="{row["url"]}">Open on Polymarket</a>' if row["url"] else ""
    return (
        f"{emoji} <b>{row['strategy_id']}</b>  ·  score {row['score']:.0f}\n"
        f"<i>{q}</i>\n\n"
        f"Buy <b>{row['side']}</b> @ ~{row['entry_price']:.1f}¢"
        f"{url_tag}"
    )


def heartbeat(url: str, fail: bool = False) -> None:
    """Ping healthchecks.io so a stalled collector is noticed within minutes."""
    if not url:
        return
    try:
        requests.get(url + ("/fail" if fail else ""), timeout=10)
    except Exception as e:
        log.warning("Heartbeat ping failed: %s", e)
