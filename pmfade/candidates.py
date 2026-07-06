"""
candidates.py — print the latest scan's ranked edge_v2 evaluation table.
The edge_candidates table (every evaluated candidate, emitted or not) is the
backtest substrate; this is its human view.

  python -m pmfade.candidates            # latest scan batch
  python -m pmfade.candidates -n 100
"""

from __future__ import annotations

import sys
import json
import argparse

from . import store


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Latest edge_v2 candidate table")
    ap.add_argument("-n", type=int, default=50)
    args = ap.parse_args()

    store.init_db()
    rows = store.latest_candidates(args.n)
    if not rows:
        print("No candidates recorded yet — let the collector run a cycle.")
        return

    print(f"\nedge_v2 candidates — scan {rows[0]['ts'][:16]}  ({len(rows)} shown)\n")
    print(f"{'emit':<5}{'side':<5}{'px':>5}{'FV':>7}{'net¢':>7}{'ann%':>7}"
          f"{'hard':>6}  {'family':<22}{'failed gates':<28} question")
    print("─" * 130)
    for r in rows:
        gates = json.loads(r["gates_passed"] or "{}")
        failed = ",".join(k for k, v in gates.items()
                          if v is False and k != "book_walked") or "-"
        print(f"{'  ✓' if r['emitted'] else '  ·':<5}"
              f"{r['side'] or '?':<5}"
              f"{r['market_price']:>5.0f}{r['fv']:>7.1f}"
              f"{(r['edge_net'] if r['edge_net'] is not None else 0):>+7.1f}"
              f"{(r['edge_net_annualized'] or 0)*100:>6.0f}%"
              f"{(r['hardness'] or 0):>6.0f}  "
              f"{(r['family'] or 'prior-only'):<22}"
              f"{failed:<28} {(r['question'] or '')[:45]}")
    print()


if __name__ == "__main__":
    main()
