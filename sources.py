"""
sources.py — cross-platform price fetcher
==========================================
Fetches prices for the same question from Manifold, Kalshi, and Metaculus.
Used to confirm whether a Polymarket price drop is an overcorrection
(other platforms hold steady) or a correct reprice (they also dropped).

Usage:
  python sources.py "Will the Fed cut rates in June?"
  python sources.py "Will the Fed cut rates" --pm-price 28
  python sources.py "fed rate cut june" --pm-price 28 --json

How it works:
  1. Extracts 3-5 keywords from the question
  2. Searches each platform's API for matching markets
  3. Scores matches by keyword overlap
  4. Returns the best-matching price from each platform
  5. Calculates the gap vs Polymarket (if --pm-price given)
"""

import sys
import json
import re
import time
import argparse
import logging
import requests
from typing import NamedTuple

log = logging.getLogger("sources")

TIMEOUT = 10

# ── Stopwords for keyword extraction ─────────────────────────────────────────

STOPWORDS = {
    "will", "the", "a", "an", "in", "on", "at", "by", "for", "of", "to",
    "be", "is", "was", "are", "were", "have", "has", "had", "do", "does",
    "did", "and", "or", "but", "not", "this", "that", "from", "with",
    "before", "after", "during", "between", "about", "than", "more", "less",
    "before", "happen", "occur", "pass", "win", "lose", "become", "get",
}


def extract_keywords(question: str, max_kw: int = 6) -> list[str]:
    """
    Pull meaningful keywords from a question string.
    Strips stopwords, short words, and punctuation.
    """
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", question).lower().split()
    kws   = [w for w in words if w not in STOPWORDS and len(w) >= 3]
    # Prioritise longer, more distinctive words
    kws.sort(key=len, reverse=True)
    return kws[:max_kw]


def match_score(candidate: str, keywords: list[str]) -> float:
    """
    How well does a candidate question match the keywords?
    Returns 0.0–1.0. A score >= 0.4 is a plausible match.
    """
    if not keywords:
        return 0.0
    c = candidate.lower()
    hits = sum(1 for kw in keywords if kw in c)
    return hits / len(keywords)


# ── Platform result type ──────────────────────────────────────────────────────

class SourceResult(NamedTuple):
    source:       str           # "manifold" | "kalshi" | "metaculus"
    price_cents:  float | None  # 0–100 cents, or None if not found
    question:     str           # matched question text
    url:          str           # direct link to the market
    match_score:  float         # keyword overlap 0–1
    raw_prob:     float | None  # original 0–1 probability


# ── Manifold ──────────────────────────────────────────────────────────────────

def fetch_manifold(keywords: list[str]) -> SourceResult:
    """
    Search Manifold Markets (play money — fear-free Bayesian signal).
    Returns the best-matching binary market.
    """
    query = " ".join(keywords[:4])
    try:
        r = requests.get(
            "https://api.manifold.markets/v0/search-markets",
            params={"term": query, "sort": "liquidity", "limit": 10},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        log.warning(f"Manifold fetch failed: {e}")
        return SourceResult("manifold", None, "", "", 0.0, None)

    best, best_score = None, 0.0
    for m in markets:
        if m.get("outcomeType") != "BINARY":
            continue
        score = match_score(m.get("question", ""), keywords)
        if score > best_score:
            best_score = score
            best = m

    if not best or best_score < 0.35:
        return SourceResult("manifold", None, "", "", best_score, None)

    prob = best.get("probability")
    price = round(prob * 100, 1) if prob is not None else None
    url   = f"https://manifold.markets/{best.get('creatorUsername','')}/{best.get('slug','')}"

    return SourceResult(
        source      = "manifold",
        price_cents = price,
        question    = best.get("question", ""),
        url         = url,
        match_score = best_score,
        raw_prob    = prob,
    )


# ── Kalshi ────────────────────────────────────────────────────────────────────

def fetch_kalshi(keywords: list[str], kalshi_token: str = "") -> SourceResult:
    """
    Search Kalshi (regulated real-money market — attracts professionals).
    Uses the public v2 API. Auth token optional (higher rate limits if provided).
    """
    query = " ".join(keywords[:4])
    headers = {}
    if kalshi_token:
        headers["Authorization"] = f"Bearer {kalshi_token}"

    try:
        r = requests.get(
            "https://trading-api.kalshi.com/trade-api/v2/markets",
            params={"status": "open", "search": query, "limit": 10},
            headers=headers,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Kalshi fetch failed: {e}")
        return SourceResult("kalshi", None, "", "", 0.0, None)

    markets = data.get("markets", [])
    best, best_score = None, 0.0

    for m in markets:
        title = m.get("title", "") or m.get("subtitle", "")
        score = match_score(title, keywords)
        if score > best_score:
            best_score = score
            best = m

    if not best or best_score < 0.35:
        return SourceResult("kalshi", None, "", "", best_score, None)

    # Kalshi yes_price is 0–100 integer (cents)
    yes_price = best.get("yes_price") or best.get("last_price")
    price     = float(yes_price) if yes_price is not None else None
    ticker    = best.get("ticker", "")
    url       = f"https://kalshi.com/markets/{ticker}" if ticker else "https://kalshi.com"
    title     = best.get("title", "") or best.get("subtitle", "")

    return SourceResult(
        source      = "kalshi",
        price_cents = price,
        question    = title,
        url         = url,
        match_score = best_score,
        raw_prob    = price / 100 if price is not None else None,
    )


# ── Metaculus ─────────────────────────────────────────────────────────────────

def fetch_metaculus(keywords: list[str]) -> SourceResult:
    """
    Search Metaculus (superforecaster community — highest weight for science/regulatory).
    Uses the public v2 API. No auth required for read access.
    """
    query = " ".join(keywords[:4])
    try:
        r = requests.get(
            "https://www.metaculus.com/api2/questions/",
            params={
                "search":       query,
                "type":         "forecast",
                "status":       "open",
                "order_by":     "-activity",
                "limit":        10,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"Metaculus fetch failed: {e}")
        return SourceResult("metaculus", None, "", "", 0.0, None)

    results = data.get("results", [])
    best, best_score = None, 0.0

    for q in results:
        title = q.get("title", "")
        score = match_score(title, keywords)
        if score > best_score:
            best_score = score
            best = q

    if not best or best_score < 0.35:
        return SourceResult("metaculus", None, "", "", best_score, None)

    # Community prediction — the crowd's probability
    pred = (best.get("community_prediction") or {}).get("full", {})
    prob  = pred.get("q2")   # median estimate
    price = round(prob * 100, 1) if prob is not None else None
    url   = f"https://www.metaculus.com{best.get('page_url','')}"

    return SourceResult(
        source      = "metaculus",
        price_cents = price,
        question    = best.get("title", ""),
        url         = url,
        match_score = best_score,
        raw_prob    = prob,
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

# Category-specific reliability weights (mirrors the entry scorecard)
CATEGORY_WEIGHTS = {
    "politics": {"manifold": 0.35, "kalshi": 0.28, "metaculus": 0.20},
    "crypto":   {"manifold": 0.15, "kalshi": 0.20, "metaculus": 0.10},
    "macro":    {"manifold": 0.25, "kalshi": 0.40, "metaculus": 0.20},
    "sports":   {"manifold": 0.20, "kalshi": 0.25, "metaculus": 0.15},
    "science":  {"manifold": 0.20, "kalshi": 0.15, "metaculus": 0.45},
}
DEFAULT_WEIGHTS = {"manifold": 0.35, "kalshi": 0.30, "metaculus": 0.20}


def weighted_fair_value(results: list[SourceResult],
                        category: str = "politics") -> float | None:
    """
    Compute a weighted fair value from all source prices.
    Only uses sources where a price was found.
    Renormalises weights to active sources only.
    """
    weights = CATEGORY_WEIGHTS.get(category, DEFAULT_WEIGHTS)
    active  = [(r, weights.get(r.source, 0.1))
               for r in results if r.price_cents is not None]

    if not active:
        return None

    total_w = sum(w for _, w in active)
    fv = sum((w / total_w) * r.price_cents for r, w in active)
    return round(fv, 1)


def gap_interpretation(pm_price: float, source_price: float,
                       source_name: str) -> tuple[str, str]:
    """
    Returns (signal_level, description) for a source vs PM gap.
    signal_level: "strong" | "good" | "marginal" | "neutral" | "negative"
    """
    gap = source_price - pm_price
    if gap >= 15:
        return "strong",   f"+{gap:.1f}pts — strong confirmation of overcorrection"
    elif gap >= 8:
        return "good",     f"+{gap:.1f}pts — good confirmation"
    elif gap >= 3:
        return "marginal", f"+{gap:.1f}pts — marginal signal"
    elif gap >= -3:
        return "neutral",  f"{gap:+.1f}pts — no meaningful gap"
    else:
        return "negative", f"{gap:.1f}pts — source agrees with drop, reconsider thesis"


def fetch_all_sources(question: str,
                      kalshi_token: str = "",
                      category: str = "politics") -> dict:
    """
    Main entry point. Fetch prices from all three sources for a question.
    Returns a comprehensive result dict.
    """
    keywords = extract_keywords(question)
    log.info(f"Searching with keywords: {keywords}")

    results = []

    # Fetch with small delays to avoid rate limits
    results.append(fetch_manifold(keywords))
    time.sleep(0.3)
    results.append(fetch_kalshi(keywords, kalshi_token))
    time.sleep(0.3)
    results.append(fetch_metaculus(keywords))

    return {
        "question":       question,
        "keywords":       keywords,
        "category":       category,
        "results":        results,
        "found_count":    sum(1 for r in results if r.price_cents is not None),
    }


# ── Formatted output ──────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"

SIGNAL_COLORS = {
    "strong":   GREEN,
    "good":     GREEN,
    "marginal": YELLOW,
    "neutral":  "",
    "negative": RED,
}

SOURCE_LABELS = {
    "manifold":  "Manifold    (play money — fear-free)",
    "kalshi":    "Kalshi      (regulated, real money)",
    "metaculus": "Metaculus   (superforecasters)",
}


def print_source_report(fetch_result: dict, pm_price: float | None = None,
                        category: str = "politics"):
    results  = fetch_result["results"]
    keywords = fetch_result["keywords"]

    print(f"\n{BOLD}{'─'*62}{RESET}")
    print(f"{BOLD}Cross-platform price check{RESET}")
    q = fetch_result["question"]
    print(f"{DIM}{q[:70]}{RESET}")
    print(f"{DIM}Keywords: {', '.join(keywords)}{RESET}")
    print(f"{'─'*62}")

    if pm_price is not None:
        print(f"\n  Polymarket   {BOLD}{pm_price}¢{RESET}  ← current price")

    print(f"\n  {'SOURCE':<28}  {'PRICE':>7}  {'MATCH':>6}  GAP / SIGNAL")
    print(f"  {'─'*60}")

    for r in results:
        label    = SOURCE_LABELS.get(r.source, r.source)
        ms_str   = f"{r.match_score:.0%}"

        if r.price_cents is None:
            print(f"  {label:<28}  {'—':>7}  {ms_str:>6}  {DIM}no matching market found{RESET}")
            continue

        price_str = f"{r.price_cents:.1f}¢"

        if pm_price is not None:
            sig, desc = gap_interpretation(pm_price, r.price_cents, r.source)
            col   = SIGNAL_COLORS.get(sig, "")
            gap_s = f"{col}{desc}{RESET}"
        else:
            gap_s = ""

        print(f"  {label:<28}  {BOLD}{price_str:>7}{RESET}  {ms_str:>6}  {gap_s}")

        if r.question:
            print(f"  {DIM}  → matched: \"{r.question[:58]}\"{RESET}")

    # Weighted fair value
    fv = weighted_fair_value(results, category)
    if fv is not None:
        print(f"\n  {BOLD}Weighted fair value ({category}): {fv}¢{RESET}")
        if pm_price is not None:
            edge = fv - pm_price
            if edge >= 10:
                verdict = f"{GREEN}Strong edge — {edge:.1f} pts above current price{RESET}"
            elif edge >= 5:
                verdict = f"{YELLOW}Modest edge — {edge:.1f} pts above current price{RESET}"
            elif edge > 0:
                verdict = f"{DIM}Thin edge — {edge:.1f} pts (check transaction costs){RESET}"
            else:
                verdict = f"{RED}No edge — fair value at or below current price{RESET}"
            print(f"  Edge vs PM:  {verdict}")
            print(f"\n  {DIM}Use {fv}¢ as your fair value input in the Kelly calculator.{RESET}")

    print(f"\n{'─'*62}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch cross-platform prices for a Polymarket question"
    )
    parser.add_argument("question", help="The market question (or keywords)")
    parser.add_argument("--pm-price", "-p", type=float, metavar="CENTS",
                        help="Current Polymarket price in cents (for gap analysis)")
    parser.add_argument("--category", "-c",
                        choices=["politics", "crypto", "macro", "sports", "science"],
                        default="politics",
                        help="Market category (affects source weighting)")
    parser.add_argument("--kalshi-token", metavar="TOKEN",
                        help="Kalshi API token for higher rate limits (optional)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON")
    args = parser.parse_args()

    result = fetch_all_sources(
        question      = args.question,
        kalshi_token  = args.kalshi_token or "",
        category      = args.category,
    )

    if args.json:
        # Convert NamedTuples to dicts for JSON serialisation
        output = {
            "question": result["question"],
            "keywords": result["keywords"],
            "category": result["category"],
            "pm_price": args.pm_price,
            "sources": [
                {
                    "source":      r.source,
                    "price_cents": r.price_cents,
                    "question":    r.question,
                    "url":         r.url,
                    "match_score": r.match_score,
                }
                for r in result["results"]
            ],
            "weighted_fair_value": weighted_fair_value(
                result["results"], args.category
            ),
        }
        print(json.dumps(output, indent=2))
        return

    print_source_report(result, pm_price=args.pm_price, category=args.category)


if __name__ == "__main__":
    main()
