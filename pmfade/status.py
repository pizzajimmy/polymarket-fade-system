"""
status.py — terminal dashboard for the running collector.
=========================================================
Operational at-a-glance: is it alive, what has it collected, and what is each
strategy producing right now. Pure stdlib + ASCII, so it works over SSH.

  python -m pmfade.status                # full dashboard
  python -m pmfade.status --recent 25    # show more of the recent-signals feed
  python -m pmfade.status --days 7       # restrict per-strategy stats to a window

For deep edge analysis once signals resolve, use `python -m pmfade.calibrate`.
On the server:  sudo -u pmfade venv/bin/python -m pmfade.status
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import statistics as st
from datetime import datetime, timezone

from . import store, config as C
from .calibrate import position_return

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
B = "\033[1m";  D = "\033[2m";  X = "\033[0m"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ago(iso: str) -> str:
    t = store.parse_iso(iso)
    if not t:
        return "?"
    s = (_now() - t).total_seconds()
    if s < 90:      return f"{int(s)}s ago"
    if s < 5400:    return f"{int(s/60)}m ago"
    if s < 172800:  return f"{int(s/3600)}h ago"
    return f"{int(s/86400)}d ago"


def _bar(n: int, mx: int, width: int = 18, ch: str = "█") -> str:
    if mx <= 0:
        return ""
    f = int(round(width * n / mx))
    return ch * f + "·" * (width - f)


def _ret(v: float) -> str:
    c = G if v > 0 else R if v < 0 else ""
    return f"{c}{v:+.1f}¢{X}"


def _service_state():
    """Best-effort `systemctl is-active pmfade`; None if not on a systemd box."""
    try:
        r = subprocess.run(["systemctl", "is-active", "pmfade"],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "unknown"
    except Exception:
        return None


# ── Sections ───────────────────────────────────────────────────────────────────

def section_health():
    stats = store.db_stats()
    with store.connect() as c:
        last = c.execute("SELECT * FROM scan_runs WHERE finished_at IS NOT NULL "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    try:
        sz = store.DB_PATH.stat().st_size
        size_s = f"{sz/1e6:.1f} MB" if sz >= 1e6 else f"{sz/1e3:.0f} KB"
    except Exception:
        size_s = "?"

    print(f"{B}{'═'*64}{X}")
    print(f"  {B}PMFADE STATUS{X}  ·  {stats['db_path']}  {D}({size_s}){X}")
    print(f"{B}{'═'*64}{X}")

    print(f"\n{B}COLLECTOR{X}")
    svc = _service_state()
    if svc is not None:
        col = G if svc == "active" else R
        print(f"  service       {col}● {svc}{X}")

    if last:
        t = store.parse_iso(last["finished_at"])
        stale = t is None or (_now() - t).total_seconds() > C.POLL_INTERVAL_SECS * 2
        flag = f"{R}⚠ stale — check the service{X}" if stale else f"{G}✓ healthy{X}"
        print(f"  last scan     {_ago(last['finished_at'])}   {flag}   "
              f"{D}(every {C.POLL_INTERVAL_SECS//60}m){X}")
        err = last["error"]
        print(f"  last cycle    {last['markets_seen']} seen · {last['signals_emitted']} signals · "
              f"{last['elapsed_secs']}s · {(R+err+X) if err else 'ok'}")
    else:
        print(f"  {Y}no completed scans recorded yet{X}")

    if stats["oldest_buffer"]:
        span = (_now() - store.parse_iso(stats["oldest_buffer"])).total_seconds() / 3600
        print(f"  data span     {stats['oldest_buffer'][:16]} → "
              f"{stats['newest_buffer'][:16]}  {D}({span:.0f}h){X}")
    print(f"  totals        {stats['markets']} markets · {stats['buffer_rows']} buffer rows · "
          f"{stats['scan_runs']} scans · {stats['resolved']} resolved")
    print(f"  signals       {stats['signals']} logged · {stats['signal_tracks']} outcome tracks")


def section_by_strategy(days=None):
    cutoff = store.iso_days_ago(days) if days else "0"
    with store.connect() as c:
        rows = c.execute("""
            SELECT s.strategy_id, s.signal_id, s.side, s.entry_price, s.score, s.ts,
                   t24.yes_price AS p24, tr.yes_price AS pres, m.resolved_price AS mres
            FROM signals s
            LEFT JOIN signal_tracks t24 ON t24.signal_id=s.signal_id AND t24.horizon='24h'
            LEFT JOIN signal_tracks tr  ON tr.signal_id=s.signal_id AND tr.horizon='resolution'
            LEFT JOIN markets m ON m.condition_id=s.condition_id AND m.resolved=1
            WHERE s.ts >= ?
        """, (cutoff,)).fetchall()

    win = f" (last {days}d)" if days else ""
    if not rows:
        print(f"\n{Y}No signals yet{win} — the collector needs to run longer.{X}")
        return

    cut24 = store.iso_hours_ago(24)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["strategy_id"], []).append(r)
    last24 = sum(1 for r in rows if r["ts"] >= cut24)

    print(f"\n{B}SIGNALS BY STRATEGY{win}{X}  {D}({len(rows)} total · {last24} in 24h){X}")

    for sid in sorted(by, key=lambda k: -len(by[k])):
        rs = by[sid]
        scores = [r["score"] for r in rs if r["score"] is not None]
        ys = sum(1 for r in rs if r["side"] == "YES")
        avg = st.mean(scores) if scores else 0
        print(f"\n  {B}{sid}{X}   {len(rs)} signals   avg score {avg:.0f}   "
              f"{G}YES {ys}{X}/{R}NO {len(rs)-ys}{X}")

        # score histogram
        bins = {b: 0 for b in range(20, 100, 10)}
        for s in scores:
            bins[min(90, max(20, int(s // 10) * 10))] += 1
        present = [b for b in bins if bins[b] > 0]
        if present:
            mx = max(bins.values())
            for b in range(min(present), max(present) + 10, 10):
                print(f"    {D}{b:>2}-{b+9}{X} {_bar(bins[b], mx)} {bins[b]}")

        # outcome peek from the tracks we already have
        r24 = [position_return(r["side"], r["entry_price"], r["p24"])
               for r in rs if r["p24"] is not None]
        rres = [position_return(r["side"], r["entry_price"],
                                r["pres"] if r["pres"] is not None else r["mres"])
                for r in rs if (r["pres"] is not None or r["mres"] is not None)]
        parts = []
        if r24:
            parts.append(f"24h {_ret(st.mean(r24))} {D}(n={len(r24)}){X}")
        if rres:
            parts.append(f"resolution {_ret(st.mean(rres))} {D}(n={len(rres)}){X}")
        print(f"    {D}outcome{X}  " + ("   ".join(parts) if parts
                                        else f"{D}none resolved yet{X}"))


def section_edge_v2():
    """v2 machinery counters — silently absent on pre-migration snapshots."""
    try:
        with store.connect() as c:
            hist_n = c.execute("SELECT COUNT(*) FROM hist_markets").fetchone()[0]
            cand_n = c.execute("SELECT COUNT(*) FROM edge_candidates").fetchone()[0]
            cand_emit = c.execute("SELECT COUNT(*) FROM edge_candidates WHERE emitted=1").fetchone()[0]
            screens = c.execute("SELECT kind, COUNT(*) n FROM structure_alerts "
                                "GROUP BY kind").fetchall()
            calib = c.execute("SELECT MAX(computed_at) FROM market_calibration").fetchone()[0]
        fades = store.pending_fade_counts()
    except sqlite3.OperationalError:
        return
    if not any([hist_n, cand_n, fades, calib]):
        return
    print(f"\n{B}EDGE v2{X}")
    print(f"  calibration substrate   {hist_n:,} resolved markets"
          + (f" · surface computed {_ago(calib)}" if calib else " · surface not computed"))
    print(f"  edge candidates         {cand_n:,} evaluated · {cand_emit:,} emitted")
    if screens:
        print("  structure screens       "
              + " · ".join(f"{r['kind']} {r['n']}" for r in screens))
    if fades:
        print("  pending fades           "
              + " · ".join(f"{k} {v}" for k, v in sorted(fades.items())))


def section_recent(n=15):
    with store.connect() as c:
        rows = c.execute("SELECT ts, strategy_id, side, entry_price, score, question "
                         "FROM signals ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return
    print(f"\n{B}RECENT SIGNALS{X}  {D}(last {len(rows)}){X}")
    for r in rows:
        q = (r["question"] or "")[:40]
        sc = r["score"] or 0
        print(f"  {D}{r['ts'][5:16]}{X}  {r['strategy_id']:<14} {r['side']:<3} "
              f"{r['entry_price']:>5.1f}¢  {sc:>3.0f}  {q}")


def main():
    ap = argparse.ArgumentParser(description="pmfade collector status dashboard")
    ap.add_argument("--recent", type=int, default=15, help="rows in the recent-signals feed")
    ap.add_argument("--days", type=int, default=None,
                    help="restrict per-strategy stats to the last N days")
    args = ap.parse_args()

    try:
        section_health()
        section_edge_v2()
        section_by_strategy(days=args.days)
        section_recent(args.recent)
    except sqlite3.OperationalError as e:
        print(f"{R}Could not read the database ({e}). "
              f"Is PMFADE_DB correct and has the collector run?{X}")
    print()


if __name__ == "__main__":
    main()
