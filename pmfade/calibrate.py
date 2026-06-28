"""
calibrate.py — turn follow-up tracks into per-strategy edge.
============================================================
This is the whole point of the rewrite: every signal was logged with its
strategy, score, and features, and then its market's price was tracked at
+6/24/72h/7d and at resolution. Here we join those and ask, per strategy:

  • does it actually have edge (return on capital, net of cost)?
  • at which horizon does the edge realize (exit early, or hold to resolution)?
  • does the strategy's own score rank-order outcomes (so you can trade only
    the top band)?

Resolution is ground truth; the intermediate horizons show the decay profile.
Runs on the SQLite store with stdlib only. (DuckDB can ATTACH the same file if
you want to slice features ad hoc, but it isn't required.)

  python -m pmfade.calibrate            # full report
  python -m pmfade.calibrate --cost 2   # assume 2pt round-trip cost
"""

from __future__ import annotations

import argparse
import statistics as st
from . import store

HORIZONS = ["6h", "24h", "72h", "7d", "resolution"]


def position_return(side: str, entry: float, later_yes: float) -> float:
    """Return in points for holding `side` bought at `entry`, now that YES=later_yes."""
    value = later_yes if side == "YES" else (100 - later_yes)
    return value - entry


def _load():
    with store.connect() as c:
        sigs = c.execute("SELECT * FROM signals").fetchall()
        tracks: dict[str, dict[str, float]] = {}
        for t in c.execute("SELECT signal_id, horizon, yes_price FROM signal_tracks"):
            tracks.setdefault(t["signal_id"], {})[t["horizon"]] = t["yes_price"]
        resolved = {m["condition_id"]: m["resolved_price"]
                    for m in c.execute(
                        "SELECT condition_id, resolved_price FROM markets WHERE resolved=1")}
    return sigs, tracks, resolved


def _has_resolution(s, tracks, resolved) -> bool:
    return ("resolution" in tracks.get(s["signal_id"], {})
            or s["condition_id"] in resolved)


def _returns_at(sigs, tracks, resolved, horizon):
    """List of (signal_row, return_pts, return_pct_on_capital) at a horizon."""
    out = []
    for s in sigs:
        sid = s["signal_id"]
        if horizon == "resolution":
            later = tracks.get(sid, {}).get("resolution", resolved.get(s["condition_id"]))
        else:
            later = tracks.get(sid, {}).get(horizon)
        if later is None:
            continue
        r = position_return(s["side"], s["entry_price"], later)
        pct = (r / s["entry_price"] * 100) if s["entry_price"] else 0.0
        out.append((s, r, pct))
    return out


def _fmt_block(title, rows_by_h, cost):
    print(f"\n{title}")
    print(f"  {'horizon':<11}{'n':>5}{'win%':>7}{'mean¢':>8}{'med¢':>7}"
          f"{'net¢':>7}{'cap%':>8}")
    print("  " + "─" * 51)
    for h in HORIZONS:
        rs = rows_by_h.get(h, [])
        if not rs:
            print(f"  {h:<11}{'—':>5}")
            continue
        pts = [r for _, r, _ in rs]
        pct = [p for _, _, p in rs]
        win = 100 * sum(1 for x in pts if x > 0) / len(pts)
        mean = st.mean(pts)
        net = mean - cost
        flag = "  ✓" if net > 0 else ""
        print(f"  {h:<11}{len(pts):>5}{win:>6.0f}%{mean:>+8.1f}{st.median(pts):>+7.1f}"
              f"{net:>+7.1f}{st.mean(pct):>+7.1f}%{flag}")


def _score_bands(rows, label):
    """Edge vs score band at resolution — the rigorous version of the CSV analysis."""
    if not rows:
        return
    print(f"\n  {label} — edge vs score band (resolution):")
    print(f"    {'band':<9}{'n':>5}{'win%':>7}{'meanCap%':>10}")
    bands: dict[int, list] = {}
    for s, r, pct in rows:
        b = int((s["score"] or 0) // 10) * 10
        bands.setdefault(b, []).append((r, pct))
    for b in sorted(bands):
        v = bands[b]
        win = 100 * sum(1 for r, _ in v if r > 0) / len(v)
        print(f"    {b}-{b+9:<5}{len(v):>5}{win:>6.0f}%{st.mean([p for _, p in v]):>+9.1f}%")


def report(cost: float = 2.0):
    sigs, tracks, resolved = _load()
    n_resolved = sum(1 for s in sigs if _has_resolution(s, tracks, resolved))
    print("=" * 62)
    print(f"  CALIBRATION  ·  {len(sigs)} signals  ·  "
          f"{n_resolved} with resolution outcome  ·  assumed cost {cost:.1f}pt")
    print("=" * 62)

    if not sigs:
        print("\nNo signals yet — run the collector for a while first.")
        return

    strategies = sorted({s["strategy_id"] for s in sigs})

    # whole-portfolio + per-strategy
    for strat in ["(all)"] + strategies:
        subset = sigs if strat == "(all)" else [s for s in sigs if s["strategy_id"] == strat]
        rows_by_h = {h: _returns_at(subset, tracks, resolved, h) for h in HORIZONS}
        n_res = len(rows_by_h.get("resolution", []))
        _fmt_block(f"▸ {strat}   ({len(subset)} signals, {n_res} resolved)", rows_by_h, cost)
        if strat != "(all)":
            _score_bands(rows_by_h.get("resolution", []), strat)

    if n_resolved < 20:
        print(f"\n⚠ Only {n_resolved} resolved signals — results are indicative, not "
              f"significant. Edge estimates stabilize past ~50–100 per strategy.")


def main():
    ap = argparse.ArgumentParser(description="Per-strategy calibration report")
    ap.add_argument("--cost", type=float, default=2.0,
                    help="assumed round-trip cost in points (spread+fee)")
    args = ap.parse_args()
    report(cost=args.cost)


if __name__ == "__main__":
    main()
