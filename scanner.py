"""
scanner.py — market scanner + drop detector
=============================================
Orchestrates the full scan cycle:
  1. Fetch all active markets from Gamma API
  2. Upsert market metadata into SQLite
  3. Record current price in price_history
  4. Calculate drop vs 24h ago and ambient volatility
  5. Fire Telegram alerts for qualifying setups

Run on a cron: */30 * * * *  (every 30 minutes)
Or continuous: python scanner.py --loop
"""

import os
import json
import time
import logging
import argparse
import requests
from datetime import datetime
from pathlib import Path

import db
from gamma import fetch_all_active_markets

# ── Config ────────────────────────────────────────────────────────────────────

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")

DROP_THRESHOLD      = float(os.environ.get("DROP_THRESHOLD", "15"))     # points
MIN_LIQUIDITY       = float(os.environ.get("MIN_LIQUIDITY", "1000"))    # USDC
MIN_VOLUME_24H      = float(os.environ.get("MIN_VOLUME_24H", "500"))    # USDC
AMBIENT_VOL_HIGH    = float(os.environ.get("AMBIENT_VOL_HIGH", "5"))    # pts/day
VOLUME_SPIKE_RATIO  = float(os.environ.get("VOLUME_SPIKE_RATIO", "1.8"))
ALERT_COOLDOWN_HRS  = int(os.environ.get("ALERT_COOLDOWN_HRS", "12"))   # hours
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL_SECS", "1800")) # 30 min
PRUNE_DAYS          = int(os.environ.get("PRUNE_DAYS", "90"))           # keep 90 days

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scanner")


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured — printing alert locally.")
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


def format_drop_alert(market: dict, drop: float, ambient_vol: float | None,
                      vol_spike: float, prev_price: float) -> str:
    q = market["question"][:90] + ("…" if len(market["question"]) > 90 else "")
    cat = market.get("category", "unknown")
    vol_str = f"{vol_spike:.1f}× avg volume" if vol_spike > 1 else ""
    amb_str = ""
    if ambient_vol:
        amb_flag = " ⚠ HIGH RETAIL VOL" if ambient_vol >= AMBIENT_VOL_HIGH else ""
        amb_str = f"\nAmbient vol: {ambient_vol:.1f} pts/day{amb_flag}"
    url = market.get("url", "")
    url_tag = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""

    return (
        f"📉 <b>Price drop alert — {cat}</b>\n"
        f"<i>{q}</i>\n\n"
        f"Drop:    <b>{prev_price:.1f}¢ → {market['yes_price']:.1f}¢  (−{drop:.1f} pts)</b>\n"
        f"Volume:  ${market['volume_24h']:,.0f} 24h  {vol_str}\n"
        f"Liquidity: ${market['liquidity']:,.0f}"
        f"{amb_str}"
        f"{url_tag}"
    )


def format_ambient_alert(market: dict, ambient_vol: float) -> str:
    q = market["question"][:90] + ("…" if len(market["question"]) > 90 else "")
    url = market.get("url", "")
    url_tag = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""
    return (
        f"🔀 <b>High ambient volatility — retail signal</b>\n"
        f"<i>{q}</i>\n\n"
        f"Avg daily move: <b>{ambient_vol:.1f} pts/day</b>  (threshold: {AMBIENT_VOL_HIGH}+)\n"
        f"Category: {market.get('category','unknown')}\n"
        f"Current price: {market['yes_price']:.1f}¢  ·  Liquidity: ${market['liquidity']:,.0f}\n"
        f"<i>Pre-qualify as a fade candidate when news hits this market.</i>"
        f"{url_tag}"
    )


# ── Core scan ─────────────────────────────────────────────────────────────────

def _is_tradeable(market: dict) -> bool:
    """Basic filter — skip illiquid / tiny markets."""
    return (
        market["liquidity"] >= MIN_LIQUIDITY
        and market["volume_24h"] >= MIN_VOLUME_24H
    )


def analyse_market(market: dict) -> dict:
    """
    For a market we've just polled, compute:
      - drop vs 24h ago
      - ambient volatility (30-day)
      - volume spike ratio
    Returns an analysis dict with alert_types list.
    """
    cid      = market["condition_id"]
    price    = market["yes_price"]
    vol_24h  = market["volume_24h"]

    prev_price   = db.price_n_hours_ago(cid, hours=24)
    ambient_vol  = db.ambient_volatility(cid, days=30)
    vol_spike_r  = db.volume_spike(cid, vol_24h) if vol_24h else 1.0

    drop = (prev_price - price) if prev_price else 0

    alert_types = []

    # Drop alert: 15+ points down in 24h, with volume confirmation
    if (prev_price is not None
            and drop >= DROP_THRESHOLD
            and vol_spike_r >= VOLUME_SPIKE_RATIO):
        alert_types.append("DROP")

    # Drop alert without volume spike (looser — still worth flagging)
    elif (prev_price is not None
          and drop >= DROP_THRESHOLD + 5):    # require larger drop if no vol spike
        alert_types.append("DROP")

    # Ambient vol alert: first time we see a high-vol market (once per day)
    if (ambient_vol is not None
            and ambient_vol >= AMBIENT_VOL_HIGH
            and db.alert_cooldown_passed(cid, "AMBIENT_HIGH", cooldown_hours=24)):
        alert_types.append("AMBIENT_HIGH")

    return {
        "condition_id":  cid,
        "price":         price,
        "prev_price":    prev_price,
        "drop":          round(drop, 2),
        "ambient_vol":   ambient_vol,
        "vol_spike":     vol_spike_r,
        "alert_types":   alert_types,
    }


def run_scan(dry_run: bool = False) -> dict:
    """
    Full scan cycle. Returns summary dict with counts.
    dry_run=True: analyse and log but don't send Telegram messages.
    """
    log.info("=== Scan cycle starting ===")
    start = time.time()

    markets_seen  = 0
    prices_stored = 0
    alerts_fired  = 0
    drop_markets  = []

    for market in fetch_all_active_markets():
        markets_seen += 1

        # Always upsert metadata so we have it for the alert bot too
        db.upsert_market(
            condition_id  = market["condition_id"],
            question      = market["question"],
            slug          = market.get("slug", ""),
            url           = market.get("url", ""),
            token_id_yes  = market.get("token_id_yes", ""),
            category      = market.get("category", "politics"),
            end_date      = market.get("end_date", ""),
        )

        # Record price even for illiquid markets — builds ambient vol history
        db.record_price(
            condition_id = market["condition_id"],
            price        = market["yes_price"],
            volume_24h   = market["volume_24h"],
            liquidity    = market["liquidity"],
        )
        prices_stored += 1

        # Only analyse markets with enough liquidity to trade
        if not _is_tradeable(market):
            continue

        analysis = analyse_market(market)

        for alert_type in analysis["alert_types"]:
            if not db.alert_cooldown_passed(
                market["condition_id"], alert_type, ALERT_COOLDOWN_HRS
            ):
                log.debug(f"Cooldown active — skipping {alert_type} for {market['question'][:40]}")
                continue

            if alert_type == "DROP":
                msg = format_drop_alert(
                    market,
                    analysis["drop"],
                    analysis["ambient_vol"],
                    analysis["vol_spike"],
                    analysis["prev_price"],
                )
                drop_markets.append({
                    "question":    market["question"],
                    "url":         market.get("url", ""),
                    "price":       market["yes_price"],
                    "prev_price":  analysis["prev_price"],
                    "drop":        analysis["drop"],
                    "ambient_vol": analysis["ambient_vol"],
                    "vol_spike":   analysis["vol_spike"],
                    "category":    market.get("category", ""),
                })

            elif alert_type == "AMBIENT_HIGH":
                msg = format_ambient_alert(market, analysis["ambient_vol"])

            if not dry_run:
                if send_telegram(msg):
                    db.log_alert(
                        market["condition_id"], alert_type,
                        price     = market["yes_price"],
                        drop      = analysis.get("drop"),
                        ambient_vol = analysis.get("ambient_vol"),
                    )
                    alerts_fired += 1
            else:
                log.info(f"[DRY RUN] Would alert {alert_type}: {market['question'][:60]}")
                alerts_fired += 1

    elapsed = round(time.time() - start, 1)
    summary = {
        "markets_scanned": markets_seen,
        "prices_stored":   prices_stored,
        "alerts_fired":    alerts_fired,
        "drop_candidates": len(drop_markets),
        "elapsed_secs":    elapsed,
        "drop_markets":    drop_markets,
    }
    log.info(
        f"=== Scan complete: {markets_seen} markets, "
        f"{alerts_fired} alerts, {elapsed}s ==="
    )
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket market scanner")
    parser.add_argument("--loop",    action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Analyse only, no Telegram")
    parser.add_argument("--stats",   action="store_true", help="Print DB stats and exit")
    parser.add_argument("--prune",   action="store_true", help="Prune old readings and exit")
    parser.add_argument("--report",  action="store_true",
                        help="Print current drop candidates as JSON and exit")
    args = parser.parse_args()

    db.init_db()

    if args.stats:
        import json
        print(json.dumps(db.db_stats(), indent=2))
        return

    if args.prune:
        n = db.prune_old_readings(keep_days=PRUNE_DAYS)
        print(f"Pruned {n} readings.")
        return

    if args.report:
        summary = run_scan(dry_run=True)
        import json
        print(json.dumps(summary["drop_markets"], indent=2))
        return

    if args.loop:
        log.info(f"Starting continuous scanner (interval: {POLL_INTERVAL}s)…")
        while True:
            try:
                run_scan(dry_run=args.dry_run)
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
            log.info(f"Sleeping {POLL_INTERVAL}s…")
            time.sleep(POLL_INTERVAL)
    else:
        run_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
