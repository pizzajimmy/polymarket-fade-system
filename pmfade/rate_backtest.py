"""
rate_backtest.py — market-implied vs base-rate anchor on RESOLVED history.
==========================================================================
The hist_* substrate (backfill + resolution catcher) holds ~a year of
resolved markets with implied prices sampled at fixed horizons before close.
For every market matching a RATE family (earthquakes, storms, …) this
replays the rate_anchor idea historically:

  at each sampled horizon: anchor = base-rate P(event | remaining window)
  edge = anchor − implied.  If |edge| ≥ RB_MIN_EDGE, take the anchor side at
  the implied price; grade against the final outcome, net of cost.

Answers the operator's two questions directly:
  • how OFTEN and how LARGE are these gaps? (are the edges eaten? by whom?)
  • had we traded them all year, what would it have paid?

Honest limits: hist covers closed markets ≥ $10k volume from the backfill
window; storm anchors use window_start = the sample date (seasonality
handled); no spread data at historical timestamps, so cost is the flat
assumption — treat results as gross-of-microstructure.

  python -m pmfade.rate_backtest            # full report
  python -m pmfade.rate_backtest --min-edge 15 --cost 2
"""

from __future__ import annotations

import sys
import argparse
import statistics as st
from datetime import datetime, timezone
from collections import defaultdict

from . import store, config as C, anchors


def _close_t(row) -> int | None:
    ct = row["closed_time"] or ""
    try:
        return int(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def run(min_edge: float, cost: float, verbose: bool = False) -> None:
    with store.connect() as c:
        markets = c.execute("SELECT * FROM hist_markets").fetchall()
        prices = defaultdict(list)
        for p in c.execute("SELECT * FROM hist_prices"):
            prices[p["condition_id"]].append(p)

    matched = 0
    samples = 0
    gaps = []                       # every (family, horizon, edge) — the gap census
    trades = []                     # (family, label, horizon, edge, net)
    examples = []

    for m in markets:
        fam = anchors.rate_family(m["question"] or "")
        if not fam:
            continue
        f = next((x for x in anchors.families() if x["family"] == fam), None)
        fn = anchors.ANCHOR_FNS.get(f.get("anchor_fn", "")) if f else None
        if fn is None:
            continue
        close_t = _close_t(m)
        won_yes = (m["final_yes"] or 0) >= 50
        matched += 1

        for p in prices.get(m["condition_id"], []):
            if close_t is None or not p["sampled_t"]:
                continue
            days = (close_t - p["sampled_t"]) / 86400.0
            if days < 1:
                continue
            sample_date = datetime.fromtimestamp(p["sampled_t"], tz=timezone.utc).date()
            try:
                out = fn(f.get("params", {}), days, "", m["question"],
                         {"window_start": sample_date.isoformat()})
            except Exception:
                out = None
            if out is None:
                continue
            prob, inputs, _note, _tier = out
            anchor_c = prob * 100
            implied = p["yes_price"]
            # already-occurred guard (mirrors the live strategy): high-priced
            # P(>=1) markets likely reflect an event the model can't see
            if inputs.get("mode") == "P(>=1)" and implied >= 85:
                continue
            edge = anchor_c - implied
            samples += 1
            gaps.append((fam, p["horizon"], edge))

            if abs(edge) >= min_edge:
                side_yes = edge > 0
                entry = implied if side_yes else 100 - implied
                payoff = (100.0 if won_yes else 0.0) if side_yes else \
                         (0.0 if won_yes else 100.0)
                net = payoff - entry - cost
                trades.append((fam, inputs.get("event", "?"), p["horizon"], edge, net))
                if len(examples) < 8:
                    examples.append((m["question"][:58], p["horizon"], implied,
                                     anchor_c, "YES" if side_yes else "NO", net))

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=" * 66)
    print(f"  RATE-ANCHOR BACKTEST  ·  {matched} matched markets · "
          f"{samples} horizon samples · trade when |edge| ≥ {min_edge:.0f} · "
          f"cost {cost:.0f}pt")
    print("=" * 66)
    if not samples:
        print("\nNo rate-family markets in hist_* — run pmfade.backfill first "
              "(and note it filters volume < $10k).")
        return

    print(f"\nGap census — how mispriced are these markets vs base rate?")
    print(f"    {'family':<22}{'horizon':<9}{'n':>5}{'med gap':>9}{'|gap|≥10':>10}{'≥20':>6}")
    by_cell = defaultdict(list)
    for fam, hz, e in gaps:
        by_cell[(fam, hz)].append(e)
    for (fam, hz), es in sorted(by_cell.items()):
        g10 = 100 * sum(1 for e in es if abs(e) >= 10) / len(es)
        g20 = 100 * sum(1 for e in es if abs(e) >= 20) / len(es)
        print(f"    {fam:<22}{hz:<9}{len(es):>5}{st.median(es):>+9.1f}{g10:>9.0f}%{g20:>5.0f}%")

    if not trades:
        print(f"\nNo samples cleared the {min_edge:.0f}pt edge threshold.")
        return

    print(f"\nSimulated trades (anchor side @ implied, held to resolution, net):")
    print(f"    {'cohort':<26}{'n':>5}{'win%':>7}{'mean¢':>8}{'total¢':>9}")
    def block(keyfn, label_all=True):
        by = defaultdict(list)
        for t in trades:
            by[keyfn(t)].append(t[4])
        for k, ns in sorted(by.items(), key=lambda kv: -len(kv[1])):
            win = 100 * sum(1 for x in ns if x > 0) / len(ns)
            print(f"    {str(k):<26}{len(ns):>5}{win:>6.0f}%{st.mean(ns):>+8.1f}"
                  f"{sum(ns):>+9.0f}")
    block(lambda t: f"{t[1]} · {t[2]}")
    ns = [t[4] for t in trades]
    win = 100 * sum(1 for x in ns if x > 0) / len(ns)
    print(f"    {'ALL':<26}{len(ns):>5}{win:>6.0f}%{st.mean(ns):>+8.1f}{sum(ns):>+9.0f}")

    print(f"\nSample trades:")
    for q, hz, imp, anc, side, net in examples:
        print(f"    [{hz:>3}] mkt {imp:>5.1f}¢ vs model {anc:>5.1f}¢ → {side:<4}"
              f"{net:>+7.1f}¢  {q}")
    print("\nCaveats: flat cost (no historical spread data); backfill volume floor "
          "$10k; aftershock/ENSO clustering not modeled — treat as upper bound.")


def main():
    ap = argparse.ArgumentParser(description="Implied-vs-base-rate backtest on resolved history")
    ap.add_argument("--min-edge", type=float, default=C.RB_MIN_EDGE)
    ap.add_argument("--cost", type=float, default=2.0)
    args = ap.parse_args()
    store.init_db()
    run(args.min_edge, args.cost)


if __name__ == "__main__":
    main()
