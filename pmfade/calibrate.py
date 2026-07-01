"""
calibrate.py — turn follow-up tracks into per-strategy edge, with uncertainty.
==============================================================================
How calibration works, precisely:

  1. Every signal was logged with its strategy, score, side, and entry price.
  2. Its market's price was then tracked at +6/24/72h/7d and at RESOLUTION.
  3. For each signal we compute the realized return of actually holding that
     position — `position_return(side, entry, later_price)` — in points and as
     a % of capital at risk (entry). Resolution is ground truth; the earlier
     horizons show how the edge decays.
  4. We aggregate per strategy and per SCORE BAND, net of an assumed round-trip
     cost, and attach 95% confidence intervals so you can tell a real edge from
     a small-sample mirage.

Assessing efficacy:
  • A band is only trustworthy when its 95% CI on mean return excludes 0 (✓).
  • Wide CIs / no ✓ = not enough resolved signals yet.
  • The real test is STABILITY: `--record` snapshots the current calibration to
    the calibration_runs table; `--history` shows how each estimate moved as
    resolutions accrued. A genuine edge stabilizes and its ✓ persists; noise
    wanders. Record on the server (weekly is plenty; a cron works).

  python -m pmfade.calibrate                 # report, with CIs
  python -m pmfade.calibrate --cost 2        # assume 2pt round-trip cost
  python -m pmfade.calibrate --record        # report AND snapshot to history
  python -m pmfade.calibrate --history       # overall evolution per strategy
  python -m pmfade.calibrate --history news_fade   # per-band evolution
"""

from __future__ import annotations

import sys
import math
import argparse
import statistics as st
from collections import OrderedDict
from datetime import datetime, timezone

from . import store

HORIZONS = ["6h", "24h", "72h", "7d", "resolution"]


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── core return math ────────────────────────────────────────────────────────────

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


# ── statistics ───────────────────────────────────────────────────────────────────

def _mean_ci(xs, z=1.96):
    n = len(xs)
    if n < 2:
        return (float("nan"), float("nan"))
    se = st.stdev(xs) / math.sqrt(n)
    m = st.mean(xs)
    return (m - z * se, m + z * se)


def _wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h) * 100, min(1.0, c + h) * 100)


def _summ(rows, cost):
    """Summ for a cell. Primary metric is mean return in POINTS per trade (net of
    cost) — additive and undistorted by low-price markets, unlike % on capital
    (a 2pt cost on a 2¢ entry reads as -100%). The 95% CI and the ✓ are on points;
    cap% is kept only as secondary intuition."""
    if not rows:
        return None
    net_pts = [r - cost for _, r, _ in rows]
    cap = [(r - cost) / s["entry_price"] * 100 for s, r, _ in rows if s["entry_price"]]
    n = len(net_pts)
    wins = sum(1 for x in net_pts if x > 0)
    lo, hi = _mean_ci(net_pts)
    mean_cap = st.mean(cap) if cap else float("nan")
    return {
        "n": n,
        "win_rate": round(100 * wins / n, 1),
        "mean_ret_pts": round(st.mean(net_pts), 2),
        "ci_lo": None if lo != lo else round(lo, 2),
        "ci_hi": None if hi != hi else round(hi, 2),
        "mean_ret_pct": None if mean_cap != mean_cap else round(mean_cap, 1),
        "sig": (lo == lo and lo > 0),
    }


def _band_summaries(rows, cost):
    bands: dict[int, list] = {}
    for s, r, pct in rows:
        b = int((s["score"] or 0) // 10) * 10
        bands.setdefault(b, []).append((s, r, pct))
    return OrderedDict((b, _summ(bands[b], cost)) for b in sorted(bands))


def compute(cost):
    sigs, tracks, resolved = _load()
    strategies = sorted({s["strategy_id"] for s in sigs})
    per = {}
    for strat in strategies:
        subset = [s for s in sigs if s["strategy_id"] == strat]
        res_rows = _returns_at(subset, tracks, resolved, "resolution")
        per[strat] = {"n_sig": len(subset),
                      "overall": _summ(res_rows, cost),
                      "bands": _band_summaries(res_rows, cost)}
    return sigs, tracks, resolved, strategies, per


# ── report ───────────────────────────────────────────────────────────────────────

def _fmt_horizons(rows_by_h, cost):
    print(f"    {'horizon':<11}{'n':>5}{'win%':>7}{'mean¢':>8}{'net¢':>8}{'cap%':>8}")
    for h in HORIZONS:
        rs = rows_by_h.get(h, [])
        if not rs:
            print(f"    {h:<11}{'—':>5}")
            continue
        pts = [r for _, r, _ in rs]
        pct = [p for _, _, p in rs]
        win = 100 * sum(1 for x in pts if x > 0) / len(pts)
        mean = st.mean(pts)
        print(f"    {h:<11}{len(pts):>5}{win:>6.0f}%{mean:>+8.1f}{mean-cost:>+8.1f}"
              f"{st.mean(pct):>+7.1f}%")


def _fmt_bands(bands):
    print(f"    {'band':<8}{'n':>4}{'win%':>7}{'mean¢':>8}{'95% CI (¢)':>18}{'cap%':>7}")
    for b, s in bands.items():
        if not s:
            continue
        ci = (f"[{s['ci_lo']:+.1f}, {s['ci_hi']:+.1f}]"
              if s["ci_lo"] is not None else "n/a (need ≥2)")
        cap = f"{s['mean_ret_pct']:+.0f}%" if s["mean_ret_pct"] is not None else "—"
        flag = " ✓" if s["sig"] else ""
        print(f"    {b}-{b+9:<4}{s['n']:>4}{s['win_rate']:>6.0f}%"
              f"{s['mean_ret_pts']:>+8.1f}{ci:>18}{cap:>7}{flag}")


def report(cost: float = 2.0):
    sigs, tracks, resolved, strategies, per = compute(cost)
    n_resolved = sum(1 for s in sigs if _has_resolution(s, tracks, resolved))
    print("=" * 64)
    print(f"  CALIBRATION  ·  {len(sigs)} signals  ·  {n_resolved} resolved  ·  "
          f"cost {cost:.1f}pt")
    print("=" * 64)
    if not sigs:
        print("\nNo signals yet — run the collector for a while first.")
        return

    for strat in strategies:
        d = per[strat]
        ov = d["overall"]
        head = f"\n▸ {strat}   {d['n_sig']} signals · {ov['n'] if ov else 0} resolved"
        if ov:
            head += f"  →  {ov['mean_ret_pts']:+.1f}¢/trade"
            if ov["ci_lo"] is not None:
                head += (f" [{ov['ci_lo']:+.1f}, {ov['ci_hi']:+.1f}]"
                         + (" ✓ edge" if ov["sig"] else ""))
        print(head)
        rows_by_h = {h: _returns_at([s for s in sigs if s["strategy_id"] == strat],
                                    tracks, resolved, h) for h in HORIZONS}
        _fmt_horizons(rows_by_h, cost)
        if d["bands"]:
            print("    by score band (resolution, net of cost):")
            _fmt_bands(d["bands"])

    print("\n" + "─" * 64)
    print("Reading it: ✓ marks cells whose 95% CI on mean return is entirely above 0")
    print("— a real edge, not small-sample noise. No ✓ / wide CI = not enough")
    print("resolved signals yet. Snapshot over time with --record and watch whether a")
    print("band's estimate stabilizes and its ✓ persists: genuine edge holds up,")
    print("noise wanders.")
    if n_resolved < 30:
        print(f"\n⚠ Only {n_resolved} resolved — treat everything as indicative. "
              f"Estimates firm up past ~50–100 resolved per strategy.")


# ── record / history ─────────────────────────────────────────────────────────────

def _row(strat, band, cost, s):
    return {"cost": cost, "strategy_id": strat, "band": band, "n": s["n"],
            "win_rate": s["win_rate"], "mean_ret_pts": s["mean_ret_pts"],
            "mean_ret_pct": s["mean_ret_pct"], "ci_lo": s["ci_lo"], "ci_hi": s["ci_hi"]}


def record(cost: float):
    store.init_db()   # ensure calibration_runs exists (idempotent)
    _sigs, _t, _r, strategies, per = compute(cost)
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for strat in strategies:
        d = per[strat]
        if d["overall"]:
            rows.append(_row(strat, None, cost, d["overall"]))
        for band, s in d["bands"].items():
            if s:
                rows.append(_row(strat, band, cost, s))
    if not rows:
        print("Nothing to record — no resolved signals yet.")
        return
    store.record_calibration(run_at, rows)
    print(f"Recorded calibration snapshot {run_at}: {len(rows)} cells "
          f"across {len(strategies)} strategies.\n")


def history(strategy: str | None):
    runs = store.calibration_history(strategy)
    if not runs:
        print("No recorded calibration snapshots yet.\n"
              "Run `python -m pmfade.calibrate --record` on the server (weekly is plenty).")
        return

    if not strategy:
        print("Calibration history — overall per strategy (mean return on capital %, net):\n")
        by_strat: dict[str, list] = OrderedDict()
        for r in runs:
            if r["band"] is None:
                by_strat.setdefault(r["strategy_id"], []).append(r)
        for strat, rows in by_strat.items():
            print(f"▸ {strat}")
            print(f"    {'recorded':<20}{'n':>5}{'mean¢':>8}   95% CI (¢)")
            for r in rows:
                ci = (f"[{r['ci_lo']:+.1f}, {r['ci_hi']:+.1f}]"
                      if r["ci_lo"] is not None else "n/a")
                mp = f"{r['mean_ret_pts']:+.1f}" if r["mean_ret_pts"] is not None else "—"
                print(f"    {r['run_at'][:16]:<20}{r['n']:>5}{mp:>8}   {ci}")
            print()
        print("Stability across rows = efficacy. If a strategy's estimate settles and its "
              "CI keeps excluding 0, the edge is real.")
        return

    print(f"Calibration history — {strategy}, per score band "
          f"(mean return, points/trade, net of cost):\n")
    by_run: dict[str, list] = OrderedDict()
    for r in runs:
        by_run.setdefault(r["run_at"], []).append(r)
    for run_at, rows in by_run.items():
        print(f"  {run_at[:16]}")
        for r in sorted(rows, key=lambda x: (x["band"] is None, x["band"] or -1)):
            label = "overall" if r["band"] is None else f"{r['band']}-{r['band']+9}"
            mp = f"{r['mean_ret_pts']:+.1f}¢" if r["mean_ret_pts"] is not None else "—"
            star = " ✓" if (r["ci_lo"] is not None and r["ci_lo"] > 0) else ""
            print(f"      {label:<9} n={r['n']:<4} {mp:>8}{star}")
        print()


def main():
    _utf8()
    ap = argparse.ArgumentParser(description="Per-strategy calibration with confidence + history")
    ap.add_argument("--cost", type=float, default=2.0,
                    help="assumed round-trip cost in points (spread+fee)")
    ap.add_argument("--record", action="store_true",
                    help="append this calibration to the recorded history")
    ap.add_argument("--history", nargs="?", const="", metavar="STRATEGY",
                    help="show recorded snapshots over time (optionally for one strategy)")
    args = ap.parse_args()

    if args.history is not None:
        history(args.history or None)
        return
    if args.record:
        record(args.cost)
    report(cost=args.cost)


if __name__ == "__main__":
    main()
