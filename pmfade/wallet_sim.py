"""
wallet_sim.py — what a wallet would actually have done trading a strategy.
==========================================================================
Per-trade means hide the two things that decide whether a strategy is
tradeable:

  • CAPITAL IS LOCKED. Buying NO at ~92c ties up 92c per share until the
    market resolves — often weeks. You cannot take every signal; the wallet
    runs out. This walks signals chronologically, opens positions only when
    cash allows, and frees capital at resolution.
  • THE TAIL. A 97%-win book is insurance-shaped: many small gains, rare
    ~-100% losses. The mean says +2.3c; the equity curve and max drawdown
    say whether you could have held it.

Entry price = recorded entry + spread haircut. Signals emitted since the
exec_entry capture carry their REAL recorded haircut and it is used in
preference to the assumed one.

  python -m pmfade.wallet_sim --strategy longshot_bias --bankroll 1000 \
                              --size 50 --spread 1.5
  python -m pmfade.wallet_sim --sweep          # P&L across haircuts
"""

from __future__ import annotations

import sys
import json
import argparse
import statistics as st
from datetime import datetime

from . import store


def _load(strategy: str) -> list[dict]:
    with store.connect() as c:
        rows = c.execute("""
            SELECT s.signal_id, s.ts, s.condition_id, s.question, s.side,
                   s.entry_price, s.score, s.features,
                   tr.yes_price AS res_track, tr.ts AS res_ts,
                   m.resolved_price, m.resolved_at
            FROM signals s
            LEFT JOIN signal_tracks tr
                   ON tr.signal_id = s.signal_id AND tr.horizon = 'resolution'
            LEFT JOIN markets m
                   ON m.condition_id = s.condition_id AND m.resolved = 1
            WHERE s.strategy_id = ?
            ORDER BY s.ts
        """, (strategy,)).fetchall()

    out = []
    for r in rows:
        res = r["res_track"] if r["res_track"] is not None else r["resolved_price"]
        if res is None:
            continue                                   # still open — no P&L yet
        close_ts = r["res_ts"] or r["resolved_at"] or r["ts"]
        try:
            f = json.loads(r["features"] or "{}")
        except Exception:
            f = {}
        out.append({
            "ts": r["ts"], "close_ts": close_ts, "cid": r["condition_id"],
            "q": r["question"] or "", "side": r["side"],
            "entry_mid": r["entry_price"], "score": r["score"] or 0,
            "real_haircut": f.get("fill_haircut"), "res": res,
        })
    return out


def _spark(vals: list[float], width: int = 48) -> str:
    if len(vals) < 2:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    step = max(1, len(vals) // width)
    v = vals[::step]
    lo, hi = min(v), max(v)
    rng = (hi - lo) or 1
    return "".join(bars[min(7, int((x - lo) / rng * 7))] for x in v)


def simulate(trades: list[dict], bankroll: float, size: float, spread: float,
             use_real: bool = True, min_score: float = 0.0,
             max_entry: float = 100.0) -> dict:
    # Subset filters. max_entry is the sharpest lever on an insurance book:
    # breakeven win rate == entry price, so capping entry at 92c means needing
    # 92% wins instead of 94.6% — 2.6 extra points of margin, for free.
    trades = [t for t in trades
              if (t["score"] or 0) >= min_score
              and t["entry_mid"] <= max_entry]
    cash = bankroll
    openp: list[dict] = []          # {close_ts, cid, shares, cost, win}
    held: set[str] = set()
    pnls: list[float] = []
    equity_curve: list[float] = [bankroll]
    realized = 0.0
    taken = skipped_cash = skipped_dup = 0
    deployed_samples: list[float] = []
    holds: list[float] = []
    entries: list[float] = []
    used_real = 0

    def close_due(now_ts: str):
        nonlocal cash, realized
        still, closed_any = [], False
        for p in openp:
            if p["close_ts"] <= now_ts:
                payoff = p["shares"] if p["win"] else 0.0
                cash += payoff
                pnl = payoff - p["cost"]
                realized += pnl
                pnls.append(pnl)
                held.discard(p["cid"])
                closed_any = True
            else:
                still.append(p)
        openp[:] = still
        # equity AFTER the sweep completes — computing it mid-loop counted a
        # partially-rebuilt open list and manufactured a fake ~-95% drawdown
        if closed_any:
            equity_curve.append(cash + sum(q["cost"] for q in openp))

    for t in trades:
        close_due(t["ts"])

        hc = t["real_haircut"] if (use_real and t["real_haircut"] is not None) else spread
        if use_real and t["real_haircut"] is not None:
            used_real += 1
        entry = t["entry_mid"] + hc
        if not (0.5 < entry < 99.9):
            continue
        if t["cid"] in held:                       # one position per market
            skipped_dup += 1
            continue
        if cash < size:
            skipped_cash += 1
            continue

        shares = size / (entry / 100.0)            # $1 per winning share
        entries.append(entry)
        win = (t["res"] < 50) if t["side"] == "NO" else (t["res"] >= 50)
        cash -= size
        openp.append({"close_ts": t["close_ts"], "cid": t["cid"], "shares": shares,
                      "cost": size, "win": win})
        held.add(t["cid"])
        taken += 1
        # utilisation against CURRENT capital (cash + open cost basis), not the
        # starting bankroll — otherwise profits push it past 100%
        _open_cost = sum(p["cost"] for p in openp)
        deployed_samples.append(_open_cost / max(1e-9, cash + _open_cost))
        try:
            d0 = store.parse_iso(t["ts"]); d1 = store.parse_iso(t["close_ts"])
            if d0 and d1 and d1 > d0:
                holds.append((d1 - d0).total_seconds() / 86400)
        except Exception:
            pass

    close_due("9999")                              # settle everything
    equity = cash
    peak = bankroll
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = min(max_dd, e - peak)

    wins = sum(1 for p in pnls if p > 0)
    # An insurance-shaped book lives or dies on one comparison: you pay `entry`
    # cents to win 100, so you must win entry% of the time just to break even.
    # Every 1c of spread raises that bar by a full percentage point.
    avg_entry = st.mean(entries) if entries else 0.0
    win_rate = (100 * wins / len(pnls)) if pnls else 0
    return {
        "avg_entry": avg_entry,
        "breakeven_wr": avg_entry,
        "wr_margin": win_rate - avg_entry,
        "resolved": len(trades), "taken": taken,
        "skipped_cash": skipped_cash, "skipped_dup": skipped_dup,
        "used_real_haircut": used_real,
        "wins": wins, "losses": len(pnls) - wins,
        "win_rate": (100 * wins / len(pnls)) if pnls else 0,
        "pnl": equity - bankroll, "equity": equity,
        "ret_pct": 100 * (equity - bankroll) / bankroll,
        "best": max(pnls) if pnls else 0, "worst": min(pnls) if pnls else 0,
        "max_dd": max_dd, "max_dd_pct": 100 * max_dd / bankroll,
        "avg_deployed": 100 * st.mean(deployed_samples) if deployed_samples else 0,
        "peak_deployed": 100 * max(deployed_samples) if deployed_samples else 0,
        "avg_hold_d": st.mean(holds) if holds else 0,
        "curve": equity_curve,
    }


def report(strategy: str, bankroll: float, size: float, spread: float, sweep: bool,
           min_score: float = 0.0, max_entry: float = 100.0, subsets: bool = False):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    trades = _load(strategy)
    print("=" * 66)
    print(f"  WALLET SIMULATION — {strategy}")
    print(f"  ${bankroll:,.0f} bankroll · ${size:,.0f}/trade · "
          f"assumed haircut {spread:.1f}¢")
    print("=" * 66)
    if not trades:
        print(f"\nNo resolved {strategy} signals yet.")
        return

    if min_score or max_entry < 100:
        print(f"  filters: score >= {min_score:.0f}, entry <= {max_entry:.0f}¢")
    r = simulate(trades, bankroll, size, spread, min_score=min_score, max_entry=max_entry)
    print(f"\n  resolved signals     {r['resolved']:,}")
    print(f"  trades taken         {r['taken']:,}   "
          f"(skipped: {r['skipped_cash']} no capital, {r['skipped_dup']} already held)")
    if r["used_real_haircut"]:
        print(f"  real fill haircuts   {r['used_real_haircut']} of {r['taken']} used "
              f"recorded spreads (rest assumed)")
    print(f"  wins / losses        {r['wins']} / {r['losses']}   ({r['win_rate']:.1f}%)")
    print(f"  avg entry            {r['avg_entry']:.1f}¢  →  needs "
          f"{r['breakeven_wr']:.1f}% wins to break even")
    margin = r["wr_margin"]
    print(f"  win-rate margin      {margin:+.1f} pts "
          + ("(the entire edge — 1¢ more spread costs 1 pt)"
             if margin > 0 else "(NEGATIVE — losing at these entries)"))
    print()
    print(f"  final equity         ${r['equity']:,.2f}")
    print(f"  P&L                  ${r['pnl']:+,.2f}   ({r['ret_pct']:+.1f}% on bankroll)")
    print(f"  best / worst trade   ${r['best']:+,.2f} / ${r['worst']:+,.2f}")
    print(f"  max drawdown         ${r['max_dd']:,.2f}   ({r['max_dd_pct']:.1f}%)")
    print(f"  capital deployed     {r['avg_deployed']:.0f}% avg, {r['peak_deployed']:.0f}% peak")
    print(f"  avg hold             {r['avg_hold_d']:.0f} days")
    sp = _spark(r["curve"])
    if sp:
        print(f"\n  equity curve  {sp}")

    if sweep:
        print(f"\n  spread sensitivity (same trades, varying entry haircut):")
        print(f"    {'haircut':<10}{'P&L':>12}{'return':>10}{'trades':>9}")
        for hc in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0):
            s = simulate(trades, bankroll, size, hc, use_real=False)
            flag = "  ← breakeven" if abs(s["pnl"]) < size * 0.02 else ""
            print(f"    {hc:>4.1f}¢     {s['pnl']:>+11,.2f}{s['ret_pct']:>9.1f}%"
                  f"{s['taken']:>9}{flag}")

    if subsets:
        print(f"\n  entry-cap subsets (breakeven win rate == entry price):")
        print(f"    {'max entry':<12}{'trades':>8}{'win%':>8}{'breakeven':>11}"
              f"{'margin':>9}{'P&L':>11}")
        for cap in (99, 96, 94, 92, 90, 88):
            s = simulate(trades, bankroll, size, spread, max_entry=cap)
            if not s["taken"]:
                continue
            print(f"    <= {cap}¢      {s['taken']:>7}{s['win_rate']:>7.1f}%"
                  f"{s['breakeven_wr']:>10.1f}%{s['wr_margin']:>+8.1f}"
                  f"{s['pnl']:>+11,.2f}")

    print("\n  Caveats: positions marked at cost (drawdown is realized, not "
          "mark-to-market);\n  one position per market; correlated legs in the same "
          "event not modelled;\n  assumes every signal was actually fillable at "
          "size (median top-of-book\n  depth measured at ~$57 — sizing up widens the "
          "haircut and moves you down\n  the sensitivity table).")


def main():
    ap = argparse.ArgumentParser(description="Wallet P&L simulation for a strategy")
    ap.add_argument("--strategy", default="longshot_bias")
    ap.add_argument("--bankroll", type=float, default=1000)
    ap.add_argument("--size", type=float, default=50)
    ap.add_argument("--spread", type=float, default=1.5,
                    help="assumed entry haircut in cents (measured median ~1.0, mean ~1.46)")
    ap.add_argument("--sweep", action="store_true", help="P&L across haircuts")
    ap.add_argument("--subsets", action="store_true",
                    help="P&L by entry-price cap (the margin lever)")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--max-entry", type=float, default=100.0,
                    help="only trade entries at/below this price in cents")
    args = ap.parse_args()
    store.init_db()
    report(args.strategy, args.bankroll, args.size, args.spread, args.sweep,
           args.min_score, args.max_entry, args.subsets)


if __name__ == "__main__":
    main()
