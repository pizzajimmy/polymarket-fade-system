"""
inspect.py — query tool for the price history database
========================================================
Useful for debugging, checking ambient vol scores, reviewing
recent drops, and verifying data quality before relying on it
for trading decisions.

Usage:
  python inspect.py markets              — list all tracked markets
  python inspect.py history CONDITION_ID — show price history for a market
  python inspect.py vol CONDITION_ID     — show ambient volatility breakdown
  python inspect.py drops                — show all DROP alerts in last 7 days
  python inspect.py stats                — DB summary stats
  python inspect.py top-vol              — markets by ambient volatility (desc)
"""

import sys
import json
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

import db

DB_PATH = Path("prices.db")


def cmd_markets():
    rows = db.all_tracked_markets()
    if not rows:
        print("No markets tracked yet. Run scanner.py first.")
        return
    print(f"\n{'CONDITION ID':<20}  {'CATEGORY':<12}  {'QUESTION'}")
    print("─" * 80)
    for r in rows:
        print(f"{r['condition_id'][:18]:<20}  {r['category']:<12}  {r['question'][:55]}")
    print(f"\n{len(rows)} markets total\n")


def cmd_history(condition_id: str, limit: int = 50):
    with db.conn() as c:
        rows = c.execute("""
            SELECT polled_at, price, volume_24h, liquidity
            FROM price_history
            WHERE condition_id = ?
            ORDER BY polled_at DESC
            LIMIT ?
        """, (condition_id, limit)).fetchall()

    if not rows:
        print(f"No history found for condition_id: {condition_id}")
        return

    market = db.get_market(condition_id)
    if market:
        print(f"\nMarket: {market['question']}")
        print(f"URL:    {market['url']}\n")

    print(f"{'TIMESTAMP':<22}  {'PRICE':>8}  {'VOL 24H':>12}  {'LIQUIDITY':>12}")
    print("─" * 60)
    for r in rows:
        v = f"${r['volume_24h']:,.0f}" if r['volume_24h'] else "—"
        l = f"${r['liquidity']:,.0f}"  if r['liquidity']  else "—"
        print(f"{r['polled_at']:<22}  {r['price']:>7.1f}¢  {v:>12}  {l:>12}")
    print(f"\n{len(rows)} readings shown (latest first)\n")


def cmd_vol(condition_id: str):
    prices = db.price_history_window(condition_id, days=30)
    av     = db.ambient_volatility(condition_id, days=30)

    market = db.get_market(condition_id)
    if market:
        print(f"\nMarket: {market['question']}")

    if len(prices) < 4:
        print(f"Not enough data ({len(prices)} readings). Need at least 4.")
        return

    moves = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]

    print(f"\nAmbient volatility (30-day window)")
    print(f"  Readings:        {len(prices)}")
    print(f"  Avg daily move:  {av:.2f} pts/day")
    print(f"  Max single move: {max(moves):.2f} pts")
    print(f"  Min single move: {min(moves):.2f} pts")
    print(f"  Classification:  {'HIGH RETAIL' if av >= 5 else 'moderate' if av >= 2 else 'low (sophisticated)'}\n")

    # Simple sparkline
    buckets = [round(p) for p in prices[-40:]]
    lo, hi  = min(buckets), max(buckets)
    rng     = hi - lo or 1
    bars    = "▁▂▃▄▅▆▇█"
    line    = "".join(bars[int((p - lo) / rng * 7)] for p in buckets)
    print(f"  Price trend (last {len(buckets)} readings):  {line}\n")


def cmd_drops(days: int = 7):
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with db.conn() as c:
        rows = c.execute("""
            SELECT a.fired_at, a.drop_magnitude, a.price_at_alert,
                   a.ambient_vol, m.question, m.url, m.category
            FROM scan_alerts a
            JOIN markets m ON m.condition_id = a.condition_id
            WHERE a.alert_type = 'DROP' AND a.fired_at >= ?
            ORDER BY a.fired_at DESC
        """, (cutoff,)).fetchall()

    if not rows:
        print(f"\nNo DROP alerts in the last {days} days.\n")
        return

    print(f"\n{'FIRED AT':<22}  {'DROP':>6}  {'AT':>6}  {'AMB VOL':>8}  QUESTION")
    print("─" * 90)
    for r in rows:
        av = f"{r['ambient_vol']:.1f}" if r['ambient_vol'] else "—"
        print(
            f"{r['fired_at'][:19]:<22}  "
            f"{r['drop_magnitude']:>5.1f}¢  "
            f"{r['price_at_alert']:>5.1f}¢  "
            f"{av:>8}  "
            f"{r['question'][:45]}"
        )
    print(f"\n{len(rows)} alerts total\n")


def cmd_top_vol(limit: int = 20):
    with db.conn() as c:
        markets = c.execute("SELECT condition_id, question, category FROM markets").fetchall()

    results = []
    for m in markets:
        av = db.ambient_volatility(m["condition_id"], days=30)
        if av is not None:
            results.append((av, m["category"], m["question"][:60], m["condition_id"]))

    results.sort(reverse=True)

    print(f"\n{'AMB VOL':>8}  {'CATEGORY':<12}  QUESTION")
    print("─" * 80)
    for av, cat, q, cid in results[:limit]:
        flag = " ← HIGH RETAIL" if av >= 5 else ""
        print(f"{av:>7.1f}¢  {cat:<12}  {q}{flag}")
    print(f"\nTop {min(limit, len(results))} markets by ambient volatility\n")


def cmd_stats():
    stats = db.db_stats()
    print("\nDatabase stats")
    print("─" * 40)
    for k, v in stats.items():
        print(f"  {k:<22} {v}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

COMMANDS = {
    "markets":  (cmd_markets,  []),
    "history":  (cmd_history,  ["condition_id"]),
    "vol":      (cmd_vol,      ["condition_id"]),
    "drops":    (cmd_drops,    []),
    "top-vol":  (cmd_top_vol,  []),
    "stats":    (cmd_stats,    []),
}

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0)

    cmd   = args[0]
    extra = args[1:]
    fn, params = COMMANDS[cmd]

    if len(extra) < len(params):
        print(f"Usage: python inspect.py {cmd} {' '.join(params)}")
        sys.exit(1)

    fn(*extra)
