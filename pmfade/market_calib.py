"""
market_calib.py — Module A: the market's own calibration surface.
=================================================================
Computes, from OUR resolved-market data (hist_* tables, seeded by
pmfade/backfill.py and grown incrementally by the engine's resolution
catcher), how mispriced each category × horizon × price-decile has
historically been. This is the empirical prior in edge_v2's fair value —
built from raw data precisely because published sources contradict each
other on longshot calibration (handoff §1).

NAMING: this is MARKET calibration (is the market itself calibrated?).
`pmfade/calibrate.py` + `calibration_runs` are SIGNAL calibration (are our
signals profitable?). Same word, different objects — keep them straight.

Method:
  • cell = category × horizon × implied-price decile → implied_mean,
    realized_freq, n.
  • per category × horizon: weighted least squares of logit(realized) on
    logit(implied) across deciles (weights = n, freqs clamped). slope > 1
    ⇒ longshots overpriced / favorites underpriced, worsening with |logit|.
  • a "(all)" pseudo-category pools every category → the global fallback.

Prior lookup (used by edge_v2):
  calibration_prior(price¢, category, days_to_res) → (fair¢, quality)
  1. decile cell with n ≥ CALIB_MIN_CELL_N        → realized_freq   ("cell")
  2. else category×horizon slope-line (n ≥ min)   → σ(a + b·logit p) ("marginal")
  3. else global "(all)" slope-line               →                  ("global")
  interpolated (log-hours) between the two bracketing horizons.

  python -m pmfade.market_calib --report          # surface + slopes
  python -m pmfade.market_calib --recompute       # force a snapshot now
  python -m pmfade.market_calib --note            # write docs/market-calibration-note.md
"""

from __future__ import annotations

import sys
import math
import argparse
import logging
from datetime import datetime, timezone
from typing import Optional

from . import store, config as C

log = logging.getLogger("pmfade.market_calib")

HORIZON_HOURS = dict(C.CALIB_HORIZONS)            # {"24h": 24, ...}
HORIZONS_ORDERED = [h for h, _ in C.CALIB_HORIZONS]
ALL = "(all)"


def _logit(p: float) -> float:
    p = min(0.995, max(0.005, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


# ── surface computation ────────────────────────────────────────────────────────

def _load_samples() -> list[tuple[str, str, float, int]]:
    """(category, horizon, implied_prob 0..1, outcome 0/1) per sample."""
    with store.connect() as c:
        rows = c.execute("""
            SELECT m.category, p.horizon, p.yes_price, m.final_yes
            FROM hist_prices p JOIN hist_markets m USING (condition_id)
            WHERE m.final_yes <= 5 OR m.final_yes >= 95
        """).fetchall()
    return [(r["category"], r["horizon"], r["yes_price"] / 100.0,
             1 if r["final_yes"] >= 50 else 0) for r in rows]


def _wls_logit(deciles: list[dict]) -> Optional[tuple[float, float]]:
    """Weighted least squares of logit(realized) on logit(implied) across
    decile aggregates. Returns (slope, intercept) or None if underdetermined."""
    pts = []
    for d in deciles:
        n = d["n"]
        if n == 0:
            continue
        # clamp realized away from 0/1 so logit exists (Wilson-ish floor)
        rf = min(1 - 0.5 / n, max(0.5 / n, d["realized_freq"]))
        pts.append((_logit(d["implied_mean"]), _logit(rf), n))
    if len({round(x, 6) for x, _, _ in pts}) < 2:
        return None
    W = sum(w for _, _, w in pts)
    xb = sum(w * x for x, _, w in pts) / W
    yb = sum(w * y for _, y, w in pts) / W
    sxx = sum(w * (x - xb) ** 2 for x, _, w in pts)
    if sxx <= 0:
        return None
    sxy = sum(w * (x - xb) * (y - yb) for x, y, w in pts)
    slope = sxy / sxx
    return slope, yb - slope * xb


def compute_surface() -> str:
    """Recompute all cells + slopes from hist_*; snapshot into market_calibration.
    Returns the computed_at key."""
    samples = _load_samples()
    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if not samples:
        log.warning("No resolved-market samples — run pmfade.backfill first.")
        return computed_at

    cats = sorted({c for c, _, _, _ in samples}) + [ALL]
    rows_out = []
    for cat in cats:
        for hz in HORIZONS_ORDERED:
            sub = [(p, o) for c, h, p, o in samples
                   if h == hz and (cat == ALL or c == cat)]
            if not sub:
                continue
            deciles = []
            for d in range(10):
                lo, hi = d / 10, (d + 1) / 10
                cell = [(p, o) for p, o in sub if lo <= p < hi] if d < 9 else \
                       [(p, o) for p, o in sub if lo <= p <= 1.0]
                n = len(cell)
                dd = {"decile": d, "n": n,
                      "implied_mean": (sum(p for p, _ in cell) / n) if n else None,
                      "realized_freq": (sum(o for _, o in cell) / n) if n else None}
                deciles.append(dd)
                if n:
                    rows_out.append((computed_at, cat, hz, d, dd["implied_mean"],
                                     dd["realized_freq"], n, None, None))
            fit = _wls_logit([d for d in deciles if d["n"]])
            if fit:
                rows_out.append((computed_at, cat, hz, None, None, None,
                                 sum(d["n"] for d in deciles), fit[0], fit[1]))

    with store.connect() as c:
        c.executemany("""INSERT INTO market_calibration
                         (computed_at, category, horizon, decile, implied_mean,
                          realized_freq, n, slope, intercept)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows_out)
    log.info("Market calibration surface computed: %d rows @ %s",
             len(rows_out), computed_at)
    return computed_at


def last_computed() -> Optional[str]:
    with store.connect() as c:
        r = c.execute("SELECT MAX(computed_at) m FROM market_calibration").fetchone()
    return r["m"] if r and r["m"] else None


def maybe_recompute() -> None:
    """Engine hook: recompute weekly (or on first run once data exists)."""
    lc = last_computed()
    if lc:
        age_h = (datetime.now(timezone.utc).replace(tzinfo=None)
                 - (store.parse_iso(lc) or datetime.min)).total_seconds() / 3600
        if age_h < C.CALIB_RECOMPUTE_DAYS * 24:
            return
    if store.hist_stats()["markets"] == 0:
        return
    prev = _slope_map(lc) if lc else {}
    computed_at = compute_surface()
    if prev:
        drift = []
        for k, s in _slope_map(computed_at).items():
            if k in prev and prev[k] is not None and s is not None:
                if abs(s - prev[k]) > 0.15:
                    drift.append(f"{k[0]}/{k[1]}: {prev[k]:+.2f}->{s:+.2f}")
        if drift:
            log.warning("Calibration drift vs previous snapshot: %s", "; ".join(drift))


def _slope_map(computed_at: str) -> dict:
    with store.connect() as c:
        rows = c.execute("""SELECT category, horizon, slope FROM market_calibration
                            WHERE computed_at=? AND decile IS NULL""",
                         (computed_at,)).fetchall()
    return {(r["category"], r["horizon"]): r["slope"] for r in rows}


# ── prior lookup (Surface) ─────────────────────────────────────────────────────

class Surface:
    def __init__(self, computed_at: str):
        self.computed_at = computed_at
        self.cells: dict = {}     # (cat, hz, decile) -> (realized, n)
        self.fits: dict = {}      # (cat, hz) -> (slope, intercept, n)
        with store.connect() as c:
            for r in c.execute("SELECT * FROM market_calibration WHERE computed_at=?",
                               (computed_at,)):
                if r["decile"] is None:
                    self.fits[(r["category"], r["horizon"])] = \
                        (r["slope"], r["intercept"], r["n"])
                else:
                    self.cells[(r["category"], r["horizon"], r["decile"])] = \
                        (r["realized_freq"], r["n"])

    def _prior_at(self, prob: float, cat: str, hz: str) -> tuple[Optional[float], str]:
        d = min(9, int(prob * 10))
        cell = self.cells.get((cat, hz, d))
        if cell and cell[1] >= C.CALIB_MIN_CELL_N:
            return cell[0], "cell"
        fit = self.fits.get((cat, hz))
        if fit and fit[0] is not None and fit[2] >= C.CALIB_MIN_CELL_N:
            return _sigmoid(fit[1] + fit[0] * _logit(prob)), "marginal"
        fit = self.fits.get((ALL, hz))
        if fit and fit[0] is not None:
            return _sigmoid(fit[1] + fit[0] * _logit(prob)), "global"
        return None, "none"

    def prior(self, price_cents: float, category: str,
              days_to_res: Optional[float]) -> tuple[Optional[float], str]:
        """Fair value in cents (or None) + quality tag. Interpolates in
        log-hours between the two bracketing horizon buckets."""
        prob = min(0.995, max(0.005, price_cents / 100.0))
        hours = max(1.0, (days_to_res or 1) * 24.0)
        hs = [(hz, HORIZON_HOURS[hz]) for hz in HORIZONS_ORDERED]
        lo = max((p for p in hs if p[1] <= hours), key=lambda p: p[1], default=hs[0])
        hi = min((p for p in hs if p[1] >= hours), key=lambda p: p[1], default=hs[-1])
        p_lo, q_lo = self._prior_at(prob, category, lo[0])
        if lo[0] == hi[0]:
            return (round(p_lo * 100, 1) if p_lo is not None else None), q_lo
        p_hi, q_hi = self._prior_at(prob, category, hi[0])
        if p_lo is None or p_hi is None:
            pick, q = (p_lo, q_lo) if p_lo is not None else (p_hi, q_hi)
            return (round(pick * 100, 1) if pick is not None else None), q
        t = (math.log(hours) - math.log(lo[1])) / (math.log(hi[1]) - math.log(lo[1]))
        val = p_lo + t * (p_hi - p_lo)
        rank = {"cell": 0, "marginal": 1, "global": 2, "none": 3}
        quality = max(q_lo, q_hi, key=lambda q: rank[q])
        return round(val * 100, 1), quality


_surface_cache: dict = {"key": None, "surface": None}


def get_surface() -> Optional[Surface]:
    """Cached latest surface; reloads only when a new snapshot exists."""
    lc = last_computed()
    if lc is None:
        return None
    if _surface_cache["key"] != lc:
        _surface_cache["key"] = lc
        _surface_cache["surface"] = Surface(lc)
    return _surface_cache["surface"]


def calibration_prior(price_cents: float, category: str,
                      days_to_res: Optional[float]) -> tuple[Optional[float], str]:
    s = get_surface()
    if s is None:
        return None, "none"
    return s.prior(price_cents, category, days_to_res)


# ── report / contradiction note ────────────────────────────────────────────────

def _report_lines() -> list[str]:
    lc = last_computed()
    if not lc:
        return ["No surface computed yet — run backfill, then --recompute."]
    s = Surface(lc)
    out = [f"Market calibration surface @ {lc}",
           f"(slope > 1 => longshots overpriced / favorites underpriced)", ""]
    out.append(f"{'category':<12}{'horizon':<9}{'n':>7}{'slope':>8}")
    for (cat, hz), (slope, _i, n) in sorted(s.fits.items()):
        out.append(f"{cat:<12}{hz:<9}{n:>7,}{(f'{slope:+.2f}' if slope is not None else '—'):>8}")
    out.append("")
    out.append("Deciles with n >= %d (politics, longest horizon available):" % C.CALIB_MIN_CELL_N)
    for hz in reversed(HORIZONS_ORDERED):
        rows = [(d, v) for (c_, h_, d), v in s.cells.items()
                if c_ == "politics" and h_ == hz and v[1] >= C.CALIB_MIN_CELL_N]
        if rows:
            out.append(f"  politics x {hz}:")
            for d, (rf, n) in sorted(rows):
                out.append(f"    {d*10:>3}-{d*10+9}c  implied~{d*10+5}c  "
                           f"realized {rf*100:5.1f}%  n={n}")
            break
    # the §1 contradiction, answered from our data
    p30 = s.fits.get(("politics", "30d")) or s.fits.get((ALL, "30d"))
    p24 = s.fits.get(("politics", "24h")) or s.fits.get((ALL, "24h"))
    out.append("")
    min_verdict_n = C.CALIB_MIN_CELL_N * 4
    if p30 and p30[0] is not None and p30[2] >= min_verdict_n:
        verdict = ("OVERPRICED longshots at long horizon (matches trade-tape/academic side)"
                   if p30[0] > 1.05 else
                   "UNDERPRICED longshots at long horizon (matches the analytics-site claim)"
                   if p30[0] < 0.95 else "≈ calibrated at long horizon")
        out.append(f"Handoff §1 contradiction, per OUR data: 30d slope "
                   f"{p30[0]:+.2f} (n={p30[2]:,}) -> {verdict}"
                   + (f"; 24h slope {p24[0]:+.2f}" if p24 and p24[0] is not None else ""))
    else:
        have = p30[2] if p30 else 0
        out.append(f"Handoff §1 contradiction: insufficient 30d data to answer "
                   f"(n={have:,}, need ≥{min_verdict_n:,}) — no verdict issued.")
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Market calibration surface (Module A)")
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--note", action="store_true",
                    help="write docs/market-calibration-note.md")
    args = ap.parse_args()

    store.init_db()
    if args.recompute:
        compute_surface()
    lines = _report_lines()
    if args.report or not (args.recompute or args.note):
        print("\n".join(lines))
    if args.note:
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "docs" / "market-calibration-note.md"
        p.parent.mkdir(exist_ok=True)
        p.write_text("# Market calibration note (auto-generated)\n\n```\n"
                     + "\n".join(lines) + "\n```\n", encoding="utf-8")
        print(f"\nWrote {p}")


if __name__ == "__main__":
    main()
