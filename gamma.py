"""
gamma.py — Polymarket Gamma API client
=======================================
Fetches active markets with current prices, volume, and liquidity.
Handles pagination and normalises the response into clean dicts.
"""

import logging
import time
import requests
from typing import Iterator

log = logging.getLogger("scanner.gamma")

GAMMA_BASE   = "https://gamma-api.polymarket.com"
PAGE_SIZE    = 200     # max markets per request
RATE_LIMIT_S = 0.5     # seconds between pages
REQUEST_TIMEOUT = 15


# ── Market fetcher ────────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, retries: int = 3) -> dict | list:
    url = f"{GAMMA_BASE}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            log.warning(f"HTTP {r.status_code} on {url} (attempt {attempt+1}): {e}")
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))   # back off on rate limit
        except requests.RequestException as e:
            log.warning(f"Request error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
    log.error(f"All retries failed for {url}")
    return []


def _parse_market(m: dict) -> dict | None:
    """
    Extract the fields we care about from a raw Gamma API market object.
    Returns None if the market is unusable (no price, already closed, etc).
    """
    try:
        # outcomePrices can arrive as a real list ["0.38","0.62"]
        # OR as a JSON string "[\"0.38\",\"0.62\"]" — handle both
        outcome_prices = m.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            import json as _json
            outcome_prices = _json.loads(outcome_prices)
        if not outcome_prices:
            return None

        yes_price = float(outcome_prices[0]) * 100    # convert to cents

        # Some markets resolve near 0/100 — skip fully resolved ones
        if yes_price < 1 or yes_price > 99:
            return None

        # clobTokenIds: [yes_token, no_token] — may also arrive as a JSON string
        token_ids = m.get("clobTokenIds", [])
        if isinstance(token_ids, str):
            import json as _json
            token_ids = _json.loads(token_ids)
        token_id_yes = token_ids[0] if token_ids else ""

        slug = m.get("slug", "")
        url  = f"https://polymarket.com/event/{slug}" if slug else ""

        # volume field was renamed from volume24hr to volume in the Gamma API
        volume = float(m.get("volume24hr") or m.get("volume") or 0)

        return {
            "condition_id":  m.get("conditionId", ""),
            "question":      m.get("question", ""),
            "slug":          slug,
            "url":           url,
            "token_id_yes":  token_id_yes,
            "yes_price":     round(yes_price, 2),
            "volume_24h":    volume,
            "liquidity":     float(m.get("liquidity", 0) or 0),
            "end_date":      m.get("endDate", ""),
            "category":      _infer_category(m.get("question", "")),
        }
    except Exception as e:
        log.warning(f"Skipping market {m.get('conditionId','?')[:16]}: {type(e).__name__}: {e}")
        return None


def _infer_category(question: str) -> str:
    """
    Rough category inference from question text.
    Used to apply the right source-reliability weights later.
    """
    q = question.lower()
    if any(w in q for w in ["bitcoin", "btc", "eth", "crypto", "token", "defi", "blockchain"]):
        return "crypto"
    if any(w in q for w in ["fed", "inflation", "gdp", "rate", "recession", "cpi", "fomc"]):
        return "macro"
    if any(w in q for w in ["fda", "drug", "trial", "cancer", "vaccine", "clinical"]):
        return "science"
    if any(w in q for w in ["nba", "nfl", "nhl", "mlb", "world cup", "premier league",
                             "championship", "playoff", "oscar", "grammy"]):
        return "sports"
    return "politics"   # default — largest category


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_all_active_markets() -> Iterator[dict]:
    """
    Generator — yields parsed market dicts for every active market.
    Handles pagination automatically.
    """
    offset = 0
    total_fetched = 0

    while True:
        params = {
            "active":    "true",
            "closed":    "false",
            "limit":     PAGE_SIZE,
            "offset":    offset,
            "order":     "volume",
            "ascending": "false",   # highest volume first
        }
        raw = _get("/markets", params)

        if not raw:
            break

        batch = [_parse_market(m) for m in raw]
        batch = [m for m in batch if m]    # filter None

        for market in batch:
            yield market
            total_fetched += 1

        log.info(f"Fetched {total_fetched} markets so far (offset={offset})…")

        # Gamma API returns fewer than PAGE_SIZE when we've hit the end
        if len(raw) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(RATE_LIMIT_S)

    log.info(f"Total active markets fetched: {total_fetched}")


def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch a single market by slug — useful for manual lookups."""
    raw = _get("/markets", {"slug": slug})
    if isinstance(raw, list) and raw:
        return _parse_market(raw[0])
    return None
