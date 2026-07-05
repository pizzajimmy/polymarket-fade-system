"""
backfill.py — seed the market-calibration substrate from resolved markets.
==========================================================================
One-off (but resumable and re-runnable) job that walks Gamma's resolved
markets and, for each, samples its implied probability at fixed horizons
before close from CLOB `prices-history`. Output feeds pmfade/market_calib.py.

Design notes (verified against the live API, 2026-07):
  • Gamma offset paging 422s past ~10k rows, so we walk END-DATE MONTH WINDOWS
    (each paged ≤100/page) instead of deep offsets. A saturated window splits
    itself in half recursively.
  • Markets resolve EARLY — a closed market can carry a far-future endDate —
    so windows also span BACKFILL_FUTURE_MO months ahead, and horizon sampling
    anchors to the event's closedTime (fallback: last trade point), never endDate.
  • One `prices-history` call per market (interval=max, fidelity=1440 = daily
    points). The 1h horizon from the handoff is dropped: daily fidelity can't
    resolve it and the published bias there is ~nil.

Usage (VPS, overnight):
  nohup venv/bin/python -m pmfade.backfill > backfill.log 2>&1 &
  python -m pmfade.backfill --status          # progress / cell counts
  python -m pmfade.backfill --months 6 --limit 50   # bounded test run
"""

from __future__ import annotations

import sys
import time
import argparse
import logging
from datetime import date, datetime, timezone

import requests

from . import store, config as C
from .markets import infer_category, is_fixture

# reuse the battle-tested pager/retry (422 already treated as end-of-data)
from gamma import _get as gamma_get

log = logging.getLogger("pmfade.backfill")

CLOB_BASE = "https://clob.polymarket.com"
PAGE = 100
MAX_PAGES_PER_WINDOW = 95          # ~9.5k rows; past this the window splits
MIN_WINDOW_DAYS = 2


# ── prices-history ─────────────────────────────────────────────────────────────

def fetch_price_history(token_id: str) -> list[dict]:
    """Daily price points over the market's whole life. [] on failure."""
    for attempt in range(3):
        try:
            r = requests.get(f"{CLOB_BASE}/prices-history",
                             params={"market": token_id, "interval": "max",
                                     "fidelity": 1440},
                             timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("history", []) or []
        except requests.RequestException as e:
            log.debug("prices-history retry %d for %s…: %s", attempt + 1, token_id[:12], e)
            time.sleep(1 + attempt)
    return []


def parse_close_t(closed_time: str) -> int | None:
    if not closed_time:
        return None
    try:
        dt = datetime.fromisoformat(closed_time.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def sample_horizons(history: list[dict], close_t: int | None) -> list[tuple[str, float, int]]:
    """Nearest point to close_t − horizon, accepted within ±50% of the horizon.
    Returns [(label, yes_price_cents, sampled_t), …]."""
    if not history or close_t is None:
        return []
    pts = [(int(p["t"]), float(p["p"])) for p in history
           if p.get("t") is not None and p.get("p") is not None]
    if not pts:
        return []
    out = []
    for label, hours in C.CALIB_HORIZONS:
        target = close_t - hours * 3600
        tol = 0.5 * hours * 3600
        best = min(pts, key=lambda tp: abs(tp[0] - target))
        if abs(best[0] - target) <= tol and best[0] < close_t:
            out.append((label, round(best[1] * 100, 2), best[0]))
    return out


# ── window walking ─────────────────────────────────────────────────────────────

def _month_add(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, 1)


def month_windows(months_back: int, months_fwd: int) -> list[tuple[str, str]]:
    """Month slices (ISO dates), newest-first, spanning past AND future endDates."""
    base = date.today().replace(day=1)
    starts = [_month_add(base, i) for i in range(-months_back, months_fwd + 1)]
    wins = [(s.isoformat(), _month_add(s, 1).isoformat()) for s in starts]
    wins.reverse()
    return wins


def fetch_window(dmin: str, dmax: str) -> list[dict] | None:
    """All closed markets with endDate in [dmin, dmax). None => saturated (split me)."""
    rows: list[dict] = []
    offset = 0
    pages = 0
    while True:
        raw = gamma_get("/markets", {"closed": "true",
                                     "end_date_min": dmin, "end_date_max": dmax,
                                     "limit": PAGE, "offset": offset})
        if not raw:
            break
        rows.extend(raw)
        pages += 1
        if len(raw) < PAGE:
            break
        if pages >= MAX_PAGES_PER_WINDOW:
            return None
        offset += PAGE
        time.sleep(0.15)
    return rows


def walk_windows(months_back: int, months_fwd: int):
    """Yield raw market dicts across all windows, splitting saturated ones."""
    stack = list(month_windows(months_back, months_fwd))
    while stack:
        dmin, dmax = stack.pop(0)
        rows = fetch_window(dmin, dmax)
        if rows is None:
            a = date.fromisoformat(dmin)
            b = date.fromisoformat(dmax)
            if (b - a).days <= MIN_WINDOW_DAYS:
                log.warning("Window %s..%s saturated at minimum size — taking first pages only", dmin, dmax)
                rows = []
                for off in range(0, MAX_PAGES_PER_WINDOW * PAGE, PAGE):
                    raw = gamma_get("/markets", {"closed": "true", "end_date_min": dmin,
                                                 "end_date_max": dmax, "limit": PAGE,
                                                 "offset": off})
                    if not raw:
                        break
                    rows.extend(raw)
            else:
                mid = a + (b - a) / 2
                log.info("Window %s..%s saturated — splitting at %s", dmin, dmax, mid)
                stack[:0] = [(dmin, mid.isoformat()), (mid.isoformat(), dmax)]
                continue
        if rows:
            log.info("Window %s..%s: %d closed markets", dmin, dmax, len(rows))
        yield from rows


# ── per-market processing ──────────────────────────────────────────────────────

def process_market(raw: dict, min_volume: float) -> str:
    from gamma import _parse_market
    p = _parse_market(raw, allow_resolved=True)
    if not p or not p["condition_id"]:
        return "unparsed"
    q = p["question"]
    cat = infer_category(q)
    if is_fixture(q) or cat in ("sports", "weather"):
        return "excluded_cat"
    if p["volume_total"] < min_volume:
        return "low_volume"
    final_yes = p["yes_price"]
    if 5 < final_yes < 95:               # voided / ambiguous close — no ground truth
        return "ambiguous"
    cid = p["condition_id"]
    if store.hist_has(cid):
        return "already"
    if not p["token_id_yes"]:
        return "no_token"

    hist = fetch_price_history(p["token_id_yes"])
    close_t = parse_close_t(p["closed_time"]) or (int(hist[-1]["t"]) if hist else None)
    samples = sample_horizons(hist, close_t)

    store.hist_upsert_market(cid, q, cat, p["end_date"], p["closed_time"],
                             final_yes, p["volume_total"])
    for label, price_c, t in samples:
        store.hist_record_price(cid, label, price_c, t)
    time.sleep(C.BACKFILL_RATE_S)
    return f"ok:{len(samples)}"


def ingest_resolved_market(parsed: dict, final_yes: float) -> int:
    """Incremental path used by the engine's resolution catcher: append one
    just-resolved market (already parsed) to the hist_* substrate.
    Returns number of horizon samples recorded."""
    cid = parsed.get("condition_id", "")
    if not cid or store.hist_has(cid):
        return 0
    hist = fetch_price_history(parsed.get("token_id_yes", ""))
    close_t = int(hist[-1]["t"]) if hist else None
    samples = sample_horizons(hist, close_t)
    store.hist_upsert_market(cid, parsed.get("question", ""),
                             parsed.get("category", "politics"),
                             parsed.get("end_date", ""), parsed.get("closed_time", ""),
                             final_yes, parsed.get("volume_total", 0) or 0)
    for label, price_c, t in samples:
        store.hist_record_price(cid, label, price_c, t)
    return len(samples)


# ── CLI ────────────────────────────────────────────────────────────────────────

def probe_events_tags() -> None:
    """Log whether /events exposes a tags field (fee-taxonomy follow-up)."""
    try:
        ev = gamma_get("/events", {"limit": 1})
        if isinstance(ev, list) and ev:
            has = "tags" in ev[0]
            log.info("/events tags probe: %s (keys sample: %s)",
                     "PRESENT" if has else "absent", sorted(ev[0].keys())[:12])
    except Exception as e:
        log.warning("/events probe failed: %s", e)


def run(months: int, min_volume: float, limit: int | None) -> dict:
    store.init_db()
    probe_events_tags()
    counters: dict[str, int] = {}
    ok = 0
    start = time.time()
    for raw in walk_windows(months, C.BACKFILL_FUTURE_MO):
        disp = process_market(raw, min_volume)
        key = disp.split(":")[0]
        counters[key] = counters.get(key, 0) + 1
        if disp.startswith("ok"):
            ok += 1
            if ok % 100 == 0:
                log.info("…%d markets ingested (%.0fs)", ok, time.time() - start)
            if limit and ok >= limit:
                log.info("--limit %d reached, stopping", limit)
                break
    summary = {"ingested": ok, "elapsed_s": round(time.time() - start, 1), **counters}
    log.info("Backfill done: %s", summary)
    return summary


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="Seed resolved-market calibration substrate")
    ap.add_argument("--months", type=int, default=C.BACKFILL_MONTHS)
    ap.add_argument("--min-volume", type=float, default=C.BACKFILL_MIN_VOLUME)
    ap.add_argument("--limit", type=int, default=None, help="stop after N ingested (testing)")
    ap.add_argument("--status", action="store_true", help="show substrate counts and exit")
    args = ap.parse_args()

    if args.status:
        store.init_db()
        s = store.hist_stats()
        print(f"hist_markets: {s['markets']:,}   hist_prices: {s['samples']:,}")
        print(f"{'category':<12}{'horizon':<9}{'n':>7}")
        for cat, hz, n in s["cells"]:
            print(f"{cat:<12}{hz:<9}{n:>7,}")
        return

    run(args.months, args.min_volume, args.limit)


if __name__ == "__main__":
    main()
