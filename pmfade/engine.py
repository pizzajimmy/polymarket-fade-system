"""
engine.py — one scan cycle, end to end.
=======================================
  1. Fetch the active universe (Gamma), skip fixtures/weather and resolved.
  2. Upsert market + append current price to the rolling buffer.
  3. Build a MarketView (precompute prev-24h, ambient vol, vol spike, days-to-res).
  4. Run the whole portfolio over every view; collect signals.
  5. Persist signals (respecting per-strategy cooldown).
  6. Follow-up: append the price of each open signal's market at due horizons.
  7. Resolution catcher: mark resolved markets + record their resolution price.
  8. Prune the buffer, push high-score alerts, ping the heartbeat, log the run.
"""

from __future__ import annotations

import time
import uuid
import logging
from datetime import datetime, timezone

from . import store, config as C, alerts
from . import markets as mk
from .strategies.base import MarketView, Context
from .portfolio import build_portfolio

log = logging.getLogger("pmfade.engine")


# ── Feature computation ────────────────────────────────────────────────────────

def _days_to_res(end_date: str):
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00")).replace(tzinfo=None)
        return max(0, (end - datetime.now(timezone.utc).replace(tzinfo=None)).days)
    except Exception:
        return None


def build_view(m: dict) -> MarketView:
    cid = m["condition_id"]
    window = store.price_window(cid, days=10)
    reading_count = len(window)
    ambient = None
    if reading_count >= 4:
        moves = [abs(window[i] - window[i - 1]) for i in range(1, reading_count)]
        ambient = round(sum(moves) / len(moves), 2)

    vols = store.volume_window(cid, days=7)
    vol_spike = 1.0
    if len(vols) >= 3:
        avg = sum(vols) / len(vols)
        if avg > 0:
            vol_spike = round((m["volume_24h"] or 0) / avg, 2)

    return MarketView(
        condition_id=cid, question=m["question"], slug=m.get("slug", ""),
        url=m.get("url", ""), token_id_yes=m.get("token_id_yes", ""),
        category=m["category"], end_date=m.get("end_date", ""),
        yes_price=m["yes_price"], volume_24h=m.get("volume_24h", 0) or 0,
        liquidity=m.get("liquidity", 0) or 0,
        prev_24h=store.price_n_hours_ago(cid, 24),
        ambient_vol=ambient, vol_spike=vol_spike,
        days_to_resolution=_days_to_res(m.get("end_date", "")),
        reading_count=reading_count,
        window_min=min(window) if window else None,
        window_max=max(window) if window else None,
        description=m.get("description", ""),
        event_id=m.get("event_id", ""),
        event_slug=m.get("event_slug", ""),
        neg_risk=m.get("neg_risk", 0) or 0,
        fees_enabled=m.get("fees_enabled", 0) or 0,
        best_bid=m.get("best_bid"),
        best_ask=m.get("best_ask"),
    )


# ── Follow-up tracking ─────────────────────────────────────────────────────────

def track_open_signals() -> int:
    n = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for s in store.signals_awaiting_tracking(C.TRACK_WINDOW_DAYS):
        sid, cid = s["signal_id"], s["condition_id"]
        ts = store.parse_iso(s["ts"])
        if ts is None:
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        price = store.latest_price(cid)
        if price is None:
            continue
        done = store.recorded_horizons(sid)
        for label, hours in C.TRACK_HORIZONS:
            if label not in done and age_h >= hours:
                store.record_track(sid, label, price)
                n += 1
    return n


# ── Resolution catcher ─────────────────────────────────────────────────────────

def catch_resolutions(active_cids: set[str]) -> int:
    """Markets with open signals that vanished from the active feed: confirm
    whether they actually resolved (authority is the resolved-state lookup, so a
    transient feed gap won't false-resolve), and record the resolution price."""
    n = 0
    open_cids = store.open_signal_condition_ids(C.TRACK_WINDOW_DAYS + 60)
    candidates = (open_cids & store.unresolved_market_ids()) - active_cids
    for cid in candidates:
        mkt = store.get_market(cid)
        if not mkt:
            continue
        rp = mk.fetch_resolved_price(mkt["slug"])
        if rp is None:
            continue
        store.mark_resolved(cid, rp)
        for sid in store.signal_ids_for_condition(cid):
            store.record_track(sid, "resolution", rp)
        n += 1
        # Grow the market-calibration substrate from live operation: one
        # prices-history call per (rare) resolution. Never let it break tracking.
        try:
            from . import backfill
            backfill.ingest_resolved_market(
                {"condition_id": cid, "token_id_yes": mkt["token_id_yes"] or "",
                 "question": mkt["question"], "category": mkt["category"] or "politics",
                 "end_date": mkt["end_date"] or "", "closed_time": "",
                 "volume_total": 0},
                final_yes=rp)
        except Exception as e:
            log.warning("hist ingest failed for %s: %s", cid[:12], e)
    return n


# ── Notifications ──────────────────────────────────────────────────────────────

def notify_new_signals() -> int:
    n = 0
    for row in store.unnotified_signals():
        if (row["score"] or 0) >= C.NOTIFY_MIN_SCORE:
            if alerts.send_telegram(alerts.format_signal(row)):
                n += 1
        store.mark_notified(row["signal_id"])   # mark either way; low-score = silent
    return n


# ── Cycle ──────────────────────────────────────────────────────────────────────

def run_cycle(dry_run: bool = False) -> dict:
    run_id = store.start_run()
    start = time.time()
    portfolio = build_portfolio()
    log.info("=== cycle start (%d strategies%s) ===",
             len(portfolio), " · DRY RUN" if dry_run else "")

    seen = 0
    active_cids: set[str] = set()
    views: list[MarketView] = []
    error = None

    try:
        # Module A: refresh the market-calibration surface weekly (cheap, DB-only).
        try:
            from . import market_calib
            market_calib.maybe_recompute()
        except Exception as e:
            log.warning("market_calib recompute skipped: %s", e)

        for m in mk.fetch_universe():
            seen += 1
            if mk.is_fixture(m["question"]) or m["category"] in ("sports", "weather"):
                continue
            if not (C.MIN_PRICE <= m["yes_price"] <= C.MAX_PRICE):
                continue
            cid = m["condition_id"]
            active_cids.add(cid)
            store.upsert_market(cid, m["question"], m.get("slug", ""), m.get("url", ""),
                                m.get("token_id_yes", ""), m["category"], m.get("end_date", ""),
                                description=m.get("description", ""),
                                event_id=m.get("event_id", ""),
                                event_slug=m.get("event_slug", ""),
                                neg_risk=m.get("neg_risk", 0) or 0,
                                fees_enabled=m.get("fees_enabled", 0) or 0)
            store.record_price(cid, m["yes_price"], m.get("volume_24h"), m.get("liquidity"),
                               best_bid=m.get("best_bid"), best_ask=m.get("best_ask"))
            views.append(build_view(m))

        ctx = Context(store=store, universe=views)

        emitted = 0
        for mv in views:
            for strat in portfolio:
                for sig in strat.run(mv, ctx):
                    if store.recent_signal_exists(sig.strategy_id, sig.condition_id,
                                                  strat.cooldown_hours):
                        continue
                    store.insert_signal(uuid.uuid4().hex, sig.strategy_id, sig.condition_id,
                                        sig.question, sig.url, sig.side, sig.entry_price,
                                        sig.score, sig.features)
                    emitted += 1
                    log.info("  [%s] %.0f  %s", sig.strategy_id, sig.score, sig.rationale[:80])

        tracked = track_open_signals()
        resolved = catch_resolutions(active_cids)
        store.prune_buffer()
        notified = notify_new_signals() if not dry_run else 0

    except Exception as e:
        error = repr(e)
        log.error("cycle error: %s", e, exc_info=True)
        emitted = tracked = resolved = notified = 0

    elapsed = round(time.time() - start, 1)
    store.finish_run(run_id, seen, emitted, elapsed, error)
    alerts.heartbeat(C.HEALTHCHECK_URL, fail=bool(error))
    log.info("=== cycle done: %d seen, %d signals, %d tracked, %d resolved, %d alerts, %ss ===",
             seen, emitted, tracked, resolved, notified, elapsed)
    return {"seen": seen, "emitted": emitted, "tracked": tracked,
            "resolved": resolved, "notified": notified, "elapsed": elapsed, "error": error}
