"""
alerts.py — Telegram push for high-score signals + healthcheck heartbeat.
Both are best-effort and never raise into the scan loop.
"""

from __future__ import annotations

import os
import json
import logging
import requests

log = logging.getLogger("pmfade.alerts")

_EMOJI = {"news_fade": "📰", "settlement_lag": "⏳",
          "longshot_bias": "🎯", "correlated_lag": "🔗",
          "edge_v2": "🧭", "news_fade_v2": "🕵️", "rate_anchor": "📐"}


def _creds(strategy: str | None = None) -> tuple[str, str]:
    """(token, chat_id) for a strategy: TG_TOKEN_<STRATEGY>/TG_CHAT_ID_<STRATEGY>
    if both set, else the default TG_TOKEN/TG_CHAT_ID bot. Read at call time so a
    single .env drives per-strategy routing (a separate bot = a separate token;
    the chat_id may be reused — each bot has its own DM thread)."""
    if strategy:
        key = strategy.upper()
        tok = os.environ.get(f"TG_TOKEN_{key}", "").strip()
        chat = os.environ.get(f"TG_CHAT_ID_{key}", "").strip()
        if tok and chat:
            return tok, chat
    return os.environ.get("TG_TOKEN", "").strip(), os.environ.get("TG_CHAT_ID", "").strip()


def send_telegram(text: str, strategy: str | None = None) -> bool:
    tok, chat = _creds(strategy)
    if not tok or not chat:
        log.info("[telegram not configured%s]\n%s",
                 f" for {strategy}" if strategy else "", text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram failed (%s): %s", strategy or "default", e)
        return False


def format_signal(row) -> str:
    emoji = _EMOJI.get(row["strategy_id"], "•")
    q = row["question"][:90] + ("…" if len(row["question"] or "") > 90 else "")
    url_tag = f'\n<a href="{row["url"]}">Open on Polymarket</a>' if row["url"] else ""

    # Module E: operator-confirm block (the human is the input-change classifier)
    extra = ""
    try:
        feats = json.loads(row["features"] or "{}")
        if feats.get("operator_check"):
            extra = f"\n\n{feats['operator_check']}"
    except Exception:
        pass

    return (
        f"{emoji} <b>{row['strategy_id']}</b>  ·  score {row['score']:.0f}\n"
        f"<i>{q}</i>\n\n"
        f"Buy <b>{row['side']}</b> @ ~{row['entry_price']:.1f}¢"
        f"{extra}"
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
