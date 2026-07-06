"""
replay_fades.py — replay the production alert log through the Module-E gates.
=============================================================================
Handoff Phase-2 acceptance: how much of the v1 firehose would the v2 fade
protocol have suppressed, and what routes to the operator?

Runs LOCALLY against the Sheets export CSV (not committed):
  python tools/replay_fades.py ["path/to/Polymarket log - Raw Alerts.csv"]

Honest approximations (the CSV logs alerts, not continuous prices):
  • an "episode" = a market's first alert + any re-alerts within 6h;
  • the post-cooldown price is the next alert's price ≥45min later — episodes
    with no re-observation are reported as such, not guessed;
  • FV proxy is the episode's pre-move price (the v1/prev-24h fallback basis);
    anchor/prior FVs didn't exist historically, so the family column shows
    what WOULD have had an anchor — the structural-suppression rate in live
    operation will be higher than this replay shows.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pmfade import anchors                            # family matching only

DEFAULT_CSV = Path.home() / "Downloads" / "Polymarket log - Raw Alerts.csv"
COOLDOWN_MIN = 45
DEVIATION_PTS = 15
EPISODE_GAP_H = 6


def parse_ts(s):
    try:
        return datetime.strptime(s.replace(" NZT", "").strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not path.exists():
        print(f"CSV not found: {path}")
        sys.exit(1)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = parse_ts(r.get("Timestamp", ""))
            if not t:
                continue
            rows.append({"t": t, "q": r.get("Market Name", ""),
                         "dir": r.get("Direction", ""),
                         "before": num(r.get("Price Before")),
                         "after": num(r.get("Price After"))})
    rows.sort(key=lambda r: r["t"])
    by_mkt = defaultdict(list)
    for r in rows:
        by_mkt[r["q"]].append(r)

    # ── build episodes ─────────────────────────────────────────────────────────
    episodes = []
    for q, rs in by_mkt.items():
        i = 0
        while i < len(rs):
            ep = {"q": q, "first": rs[i], "later": []}
            j = i + 1
            while j < len(rs) and rs[j]["t"] - rs[i]["t"] <= timedelta(hours=EPISODE_GAP_H):
                ep["later"].append(rs[j])
                j += 1
            episodes.append(ep)
            i = j

    n_alerts = len(rows)
    n_ep = len(episodes)

    # ── gates ──────────────────────────────────────────────────────────────────
    no_obs = snapped = not_viable = promoted = 0
    fam_counts = defaultdict(int)
    examples = {"promoted": [], "snapped": []}
    for ep in episodes:
        f = ep["first"]
        obs = next((l for l in ep["later"]
                    if l["t"] - f["t"] >= timedelta(minutes=COOLDOWN_MIN)
                    and l["after"] is not None), None)
        if obs is None or f["before"] is None:
            no_obs += 1
            continue
        ref, post = f["before"], obs["after"]
        disp = (ref - post) if f["dir"] == "DROP" else (post - ref)
        if disp < DEVIATION_PTS:
            snapped += 1
            if len(examples["snapped"]) < 3:
                examples["snapped"].append((f["q"][:55], f["dir"], ref, post))
            continue
        if not (3 <= post <= 97):
            not_viable += 1
            continue
        promoted += 1
        fam = anchors.family_name(ep["q"]) or "(no family — operator judges)"
        fam_counts[fam] += 1
        if len(examples["promoted"]) < 5:
            examples["promoted"].append((f["q"][:55], f["dir"], ref, post, fam))

    days = max(1e-9, (rows[-1]["t"] - rows[0]["t"]).total_seconds() / 86400)
    print(f"\nModule-E replay — {path.name}")
    print(f"period {rows[0]['t']:%Y-%m-%d} → {rows[-1]['t']:%Y-%m-%d} ({days:.0f}d)\n")
    print(f"  raw v1 alerts                    {n_alerts:>6,}   ({n_alerts/days:>6.1f}/day)")
    print(f"  distinct episodes (6h collapse)  {n_ep:>6,}   ({n_ep/days:>6.1f}/day)")
    print(f"    no ≥{COOLDOWN_MIN}min re-observation      {no_obs:>6,}   (undecidable in CSV)")
    print(f"    snapped back < {DEVIATION_PTS}pts          {snapped:>6,}   (correctly not chased)")
    print(f"    boundary/not viable            {not_viable:>6,}")
    print(f"  → operator-review candidates     {promoted:>6,}   ({promoted/days:>6.1f}/day, "
          f"was {n_alerts/days:.0f}/day)\n")

    if fam_counts:
        print("  candidates by anchor family (structural inputs shown to operator):")
        for fam, n in sorted(fam_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>4}  {fam}")
    if examples["promoted"]:
        print("\n  sample promoted:")
        for q, d, ref, post, fam in examples["promoted"]:
            print(f"    [{d:<5}] {ref:>4.0f}→{post:<4.0f}  {fam[:26]:<28} {q}")
    if examples["snapped"]:
        print("\n  sample snapped-back (fade correctly skipped):")
        for q, d, ref, post in examples["snapped"]:
            print(f"    [{d:<5}] {ref:>4.0f}→{post:<4.0f}  {q}")
    print()


if __name__ == "__main__":
    main()
