"""
news.py — free headline retrieval for fade qualification (Module E, v2.1).
==========================================================================
Google News RSS, stdlib XML parsing, no key, no cost. Called ONLY when a
pending fade reaches its decision scan (a handful of times per day at most),
never in the hot per-market loop. Everything fails open: no headlines is a
degraded-but-valid state — the operator alert just ships without evidence.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

from . import config as C
from .strategies.correlated_lag import _keywords   # same extractor, one source of truth

log = logging.getLogger("pmfade.news")

RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_headlines(query: str, max_n: int | None = None,
                    window_hrs: float | None = None) -> list[dict]:
    """Recent headlines for a free-text query.
    Returns [{title, source, published, url}], newest first. [] on any failure."""
    max_n = max_n or C.NEWS_MAX_HEADLINES
    window_hrs = window_hrs or C.NEWS_WINDOW_HRS
    try:
        r = requests.get(RSS_URL.format(q=quote_plus(query)), timeout=12,
                         headers={"User-Agent": "pmfade/2.1"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning("headline fetch failed for %r: %s", query[:40], e)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hrs)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        pub_raw = item.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub_raw)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:
            pub = None
        if pub and pub < cutoff:
            continue
        src = item.find("{https://news.google.com/rss}source")
        if src is None:
            src = item.find("source")
        out.append({"title": title,
                    "source": (src.text or "").strip() if src is not None else "",
                    "published": pub.isoformat() if pub else "",
                    "url": (item.findtext("link") or "").strip()})
    out.sort(key=lambda h: h["published"], reverse=True)
    return out[:max_n]


def fetch_for_market(question: str) -> list[dict]:
    """Headlines for a market question — keyword query, not the raw question."""
    if C.NEWS_FEED != "google_rss":
        return []
    kws = sorted(_keywords(question, max_kw=5))
    if len(kws) < 2:
        return []
    return fetch_headlines(" ".join(kws))


def headlines_block(headlines: list[dict]) -> str:
    """Render for the operator-check Telegram block."""
    if not headlines:
        return "No recent headlines found (last "\
               f"{C.NEWS_WINDOW_HRS:.0f}h) — judge from your own sources."
    lines = ["Recent headlines:"]
    for h in headlines:
        when = h["published"][5:16].replace("T", " ") if h["published"] else "?"
        src = f" — {h['source']}" if h["source"] else ""
        lines.append(f"• [{when}] {h['title'][:90]}{src}")
    return "\n".join(lines)
