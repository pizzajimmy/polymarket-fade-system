"""
markets.py — universe fetch, categorization, and resolved-state lookup
======================================================================
Reuses the existing, battle-tested gamma.py fetcher (it already handles the
Gamma API's outcomePrices/clobTokenIds string-vs-list quirks and pagination),
but RE-categorizes every market with an improved classifier.

Why re-categorize: in the production log 87% of markets fell into the default
"politics" bucket — stock markets ("NVDA close above $180") landed in politics,
a hockey market ("Oilers win the Pacific Division") in macro. Category gates
strategy behavior (crypto penalty, fixture/weather exclusion), so it has to be
roughly right. We also add `weather` and `stocks` buckets and a fixture flag,
because weather/fixture markets are resolution-convergence traps, not fades.
"""

from __future__ import annotations

import re
import logging
import requests
from typing import Iterator, Optional

# reuse the existing robust fetcher/parser
from gamma import fetch_all_active_markets as _gamma_fetch, GAMMA_BASE, _get

log = logging.getLogger("pmfade.markets")


# ── Fixture / weather detection (resolution-convergence traps) ─────────────────

FIXTURE_KEYWORDS = [
    " vs ", " vs. ", "o/u ", "over/under", "spread:", "moneyline",
    "both teams to score", "first half", "map 1", "map 2", "map 3",
    "odd/even", "correct score", "exact score", "next goal", "anytime goalscorer",
    "to score", "bo3", "bo5", "set winner", "game winner", "anytime",
    "total corners", "total kills", "shots on target", "to win the match",
]
# player props like "Beier: 2+ shots", "Doncic: 30+ points"
_PROP_RE = re.compile(r":\s*\d+\+?\s*(shots?|goals?|assists?|points?|rebounds?|"
                      r"saves?|tackles?|kills?|corners?|threes?|passes?)\b")
WEATHER_KEYWORDS = [
    "highest temperature", "lowest temperature", "temperature in",
    "rainfall", "snowfall", "will it rain", "inches of snow", "high temp",
]


def is_fixture(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in FIXTURE_KEYWORDS) or bool(_PROP_RE.search(q))


def is_weather(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in WEATHER_KEYWORDS)


# ── Category inference (ordered: most specific first) ──────────────────────────

_CRYPTO = ["bitcoin", "btc", "ethereum", " eth ", "crypto", "solana", " sol ",
           "xrp", "dogecoin", "memecoin", "defi", "blockchain", "coinbase",
           "binance", "token", "stablecoin", "altcoin", "fdv", "airdrop"]
_STOCKS = ["nvda", "nvidia", "tesla", "tsla", "apple", "aapl", "google", "googl",
           "amazon", "amzn", "microsoft", "msft", "meta ", "s&p 500", "nasdaq",
           "dow jones", "stock", "share price", "close above", "close below",
           "all-time high", "ipo"]
_MACRO  = ["fed ", "fomc", "inflation", "cpi", "pce", "gdp", "interest rate",
           "rate cut", "rate hike", "recession", "unemployment", "jobs report",
           "tariff", "treasury", "central bank", "crude", " oil ", "wti", "brent",
           "opec", "natural gas", "barrel"]
_SCIENCE = ["fda", "clinical trial", "phase 3", "phase iii", "vaccine", "drug ",
            "cancer", "biotech", "approval", "nasa", "spacex launch"]
_SPORTS = ["nba", "nfl", "nhl", "mlb", "nascar", "mls", "pga", "ufc", "wwe",
           "world cup", "premier league", "la liga", "bundesliga", "serie a",
           "champions league", "europa league", "playoff", "super bowl",
           "world series", "stanley cup", "march madness", "division",
           "eastern conference", "western conference",
           "nl east", "nl west", "nl central", "al east", "al west", "al central",
           "pennant", "conference final", "mvp", "top scorer", "leading scorer", "draft pick",
           "esports", "valorant", "league of legends", "dota", "counter-strike",
           "oscar", "grammy", "emmy", "golden globe", "box office", "netflix top",
           # roster/award markets that read as politics without these (live
           # misclassifications 2026-07: Ballon d'Or, "play for the Chiefs")
           "ballon d'or", "play for the", "signs with", "traded to", "next club",
           "golden boot", "manager of the", "player of the"]


def infer_category(question: str) -> str:
    q = (question or "").lower()
    if is_weather(question):
        return "weather"
    if any(k in q for k in _CRYPTO):
        return "crypto"
    if any(k in q for k in _STOCKS):
        return "stocks"
    if any(k in q for k in _SCIENCE):
        return "science"
    if any(k in q for k in _MACRO):
        return "macro"
    if is_fixture(question) or any(k in q for k in _SPORTS):
        return "sports"
    return "politics"


# ── Universe fetch ─────────────────────────────────────────────────────────────

def fetch_universe() -> Iterator[dict]:
    """Yield parsed active markets with improved categories."""
    for m in _gamma_fetch():
        m["category"] = infer_category(m.get("question", ""))
        yield m


# ── Resolved-state lookup (for the resolution catcher) ─────────────────────────

def fetch_resolved_price(slug: str) -> Optional[float]:
    """
    Look up a (possibly closed) market by slug and return its resolved YES price
    in cents (≈100 if YES won, ≈0 if NO won), or None if not yet resolved/unknown.
    Unlike fetch_universe this does NOT filter out resolved markets.
    """
    if not slug:
        return None
    try:
        raw = _get("/markets", {"slug": slug})
        if not raw:
            return None
        m = raw[0] if isinstance(raw, list) else raw
        closed = m.get("closed") in (True, "true", "True", 1)
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)
        if not prices:
            return None
        yes = float(prices[0]) * 100
        if closed or yes >= 99.5 or yes <= 0.5:
            return round(yes, 1)
        return None
    except Exception as e:
        log.warning("resolved lookup failed for %s: %s", slug, e)
        return None
