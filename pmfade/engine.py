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
from . import hardness, anchors
from .strategies.base import MarketView, Context
from .portfolio import build_portfolio

log = logging.getLogger("pmfade.engine")

# per-process cache so hardness/family DB writes happen only on change
_computed_cache: dict = {}


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
        volume_total=m.get("volume_total", 0) or 0,
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


# ── Structural screens (handoff §5: near-free alpha + sanity checks) ──────────

def run_structure_screens(views: list[MarketView], dry_run: bool) -> int:
    """Ladder monotonicity + negRisk ΣYES checks across event groups."""
    fired = 0
    groups: dict[str, list[MarketView]] = {}
    for v in views:
        if v.event_slug:
            groups.setdefault(v.event_slug, []).append(v)

    for slug, vs in groups.items():
        if len(vs) < 2:
            continue

        # negRisk: mutually-exclusive outcomes should sum to ~1. Legs priced
        # <1¢ are filtered upstream, which only LOWERS the sum — so alert on
        # over-sum only. Two sanity bounds (live probe 2026-07): count only
        # legs with a real book (liquidity + quotes) — dead legs sit at stale
        # midpoints; and sums far above 1 (we saw 6.26 across 15 legs) are
        # price-data artifacts, not arbs — a real overround is a few percent.
        if any(v.neg_risk for v in vs) and len(vs) >= 3:
            live = [v for v in vs if v.liquidity >= 500 and v.best_bid is not None]
            if len(live) >= 3:
                total = sum(v.yes_price for v in live) / 100.0
                if 1.06 <= total <= 1.60 and not store.structure_alert_recent("NEGRISK", slug):
                    detail = f"sum(YES)={total:.2f} across {len(live)} live legs"
                    store.insert_structure_alert("NEGRISK", slug, detail)
                    fired += 1
                    if not dry_run:
                        alerts.send_telegram(
                            f"🧮 <b>NegRisk over-sum</b>\n<i>{live[0].question[:70]}…</i>\n"
                            f"{detail}\nhttps://polymarket.com/event/{slug}")
                    log.info("[screen] NEGRISK %s: %s", slug[:40], detail)

        # date-ladder monotonicity within one anchor family:
        # P(by earlier date) must be <= P(by later date) (+tolerance)
        fams: dict[str, list[MarketView]] = {}
        for v in vs:
            fam = anchors.family_name(v.question)
            if fam and v.end_date:
                fams.setdefault(fam, []).append(v)
        for fam, lvs in fams.items():
            if len(lvs) < 2:
                continue
            lvs.sort(key=lambda v: v.end_date)
            for a, b in zip(lvs, lvs[1:]):
                if a.end_date != b.end_date and a.yes_price > b.yes_price + 1.5:
                    if not store.structure_alert_recent("LADDER", slug):
                        detail = (f"[{fam}] P(by {a.end_date[:10]})={a.yes_price:.0f}¢ "
                                  f"> P(by {b.end_date[:10]})={b.yes_price:.0f}¢")
                        store.insert_structure_alert("LADDER", slug, detail)
                        fired += 1
                        if not dry_run:
                            alerts.send_telegram(
                                f"🪜 <b>Ladder violation</b>\n<i>{a.question[:70]}…</i>\n"
                                f"{detail}\nhttps://polymarket.com/event/{slug}")
                        log.info("[screen] LADDER %s: %s", slug[:40], detail)
                    break
    return fired


# ── Notifications ──────────────────────────────────────────────────────────────

def notify_new_signals() -> int:
    n = 0
    for row in store.unnotified_signals():
        sid = row["strategy_id"]
        strat_ok = (not C.NOTIFY_STRATEGIES) or sid in C.NOTIFY_STRATEGIES
        if strat_ok and (row["score"] or 0) >= C.notify_min_score(sid):
            if alerts.send_telegram(alerts.format_signal(row), strategy=sid):
                n += 1
        store.mark_notified(row["signal_id"])   # mark either way; muted = silent
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

            # Modules C/B observability: hardness + family cached on the market
            # row (dashboard/queries); recomputed only when inputs change.
            h, flags = hardness.score(m.get("description", ""))
            fam = anchors.family_name(m["question"]) or ""
            if _computed_cache.get(cid) != (h, fam):
                store.set_market_computed(cid, hardness=h, hardness_flags=flags,
                                          family=fam)
                _computed_cache[cid] = (h, fam)

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

        screens = run_structure_screens(views, dry_run)
        if screens:
            log.info("structure screens fired: %d", screens)

        tracked = track_open_signals()
        resolved = catch_resolutions(active_cids)
        store.prune_buffer()
        store.prune_edge_candidates(C.CANDIDATES_RETAIN_DAYS)
        notified = notify_new_signals() if not dry_run else 0

        # expired manual anchor overrides -> operator alert (once per process)
        for key, note in anchors.take_expired_alerts():
            msg = (f"⚠️ <b>Anchor override expired</b>\n<code>{key}</code>\n"
                   f"{note or '(no note)'}\nRefresh or remove it in anchors_manual.json.")
            if not dry_run:
                alerts.send_telegram(msg)
            else:
                log.info("[DRY] expired override: %s", key)

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
