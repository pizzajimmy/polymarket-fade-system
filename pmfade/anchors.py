"""
anchors.py — Module B: structural probability models per market family.
=======================================================================
Each family decomposes one recurring market shape into explicit inputs and
deterministic math, the way an rNPV model decomposes a biotech catalyst.
Anchors are functions of days_remaining, so an unmet precondition with a
shrinking window DECAYS every scan — that decay is the convergence edge_v2
prices (handoff §3).

Templates live in pmfade/families/*.json:
  { "family", "priority", "tier", "anchor_fn", "match": {"any": [regex,…]},
    "params": {…} }
JSON not YAML — the collector is stdlib-only by design. The anchor math
itself is code (registered callables below); templates hold matching rules
and parameters.

Operator overrides live in anchors_manual.json at the repo root (gitignored):
  { "<condition_id or slug>": { "anchor": 0.03, "note": "…",
      "expiry": "2026-08-01", "tier": 2, …extra input overrides… } }
Overrides beat family models; expired overrides are ignored and flagged for
a Telegram alert. Every application is logged into signal features.

Confidence tiers drive the fair-value blend weight in edge_v2
(1 mechanical=0.8, 2 base-rate=0.6, 3 judgment=0.4). A family may return a
tier different from its template when the mechanical part isn't binding —
e.g. ceasefire_by_date is tier 1 only while the 10-day-hold floor makes the
window mathematically impossible; beyond that it's tier-3 stage judgment.
"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

log = logging.getLogger("pmfade.anchors")

FAMILIES_DIR = Path(__file__).resolve().parent / "families"
MANUAL_PATH = Path(__file__).resolve().parent.parent / "anchors_manual.json"


@dataclass
class AnchorResult:
    family: str
    tier: int                 # effective tier (may differ from template)
    prob: float               # structural probability, 0..1
    inputs: dict = field(default_factory=dict)
    note: str = ""
    manual: bool = False


# ── template loading ───────────────────────────────────────────────────────────

def _load_families() -> list[dict]:
    fams = []
    if not FAMILIES_DIR.exists():
        return fams
    for p in sorted(FAMILIES_DIR.glob("*.json")):
        try:
            f = json.loads(p.read_text(encoding="utf-8"))
            f["_rx_any"] = [re.compile(rx, re.I) for rx in f.get("match", {}).get("any", [])]
            f["_rx_none"] = [re.compile(rx, re.I) for rx in f.get("match", {}).get("none", [])]
            fams.append(f)
        except Exception as e:
            log.error("Bad family template %s: %s", p.name, e)
    fams.sort(key=lambda f: f.get("priority", 100))
    return fams


_FAMILIES: Optional[list[dict]] = None


def families() -> list[dict]:
    global _FAMILIES
    if _FAMILIES is None:
        _FAMILIES = _load_families()
        log.info("Loaded %d anchor families", len(_FAMILIES))
    return _FAMILIES


@lru_cache(maxsize=16384)
def family_name(question: str) -> Optional[str]:
    f = _match(question)
    return f["family"] if f else None


def _match(question: str) -> Optional[dict]:
    q = question or ""
    for f in families():
        if any(rx.search(q) for rx in f["_rx_none"]):
            continue
        if any(rx.search(q) for rx in f["_rx_any"]):
            return f
    return None


# ── manual overrides ───────────────────────────────────────────────────────────

_manual_cache = {"mtime": None, "data": {}}
_expired_flagged: set[str] = set()


def _manual() -> dict:
    try:
        mt = MANUAL_PATH.stat().st_mtime if MANUAL_PATH.exists() else None
        if mt != _manual_cache["mtime"]:
            _manual_cache["mtime"] = mt
            _manual_cache["data"] = (json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
                                     if mt else {})
            if mt:
                log.info("anchors_manual.json loaded: %d overrides",
                         len(_manual_cache["data"]))
    except Exception as e:
        log.error("anchors_manual.json unreadable: %s", e)
    return _manual_cache["data"]


def _override_for(condition_id: str, slug: str) -> tuple[Optional[dict], Optional[str]]:
    data = _manual()
    for key in (condition_id, slug):
        if key and key in data:
            ov = data[key]
            exp = ov.get("expiry")
            if exp and exp < date.today().isoformat():
                if key not in _expired_flagged:
                    _expired_flagged.add(key)
                    _EXPIRED_QUEUE.append((key, ov.get("note", "")))
                return None, None
            return ov, key
    return None, None


_EXPIRED_QUEUE: list[tuple[str, str]] = []


def take_expired_alerts() -> list[tuple[str, str]]:
    """Expired overrides seen since last call (engine sends one Telegram each)."""
    out, _EXPIRED_QUEUE[:] = _EXPIRED_QUEUE[:], []
    return out


# ── date helpers ───────────────────────────────────────────────────────────────

def _parse_end(end_date: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(end_date.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _crosses_next_seating(end_date: str) -> bool:
    """Does the window cross the next US congressional seating (Jan 3)?"""
    ed = _parse_end(end_date)
    if not ed:
        return False
    today = date.today()
    seat = date(today.year + (1 if (today.month, today.day) >= (1, 3) else 0), 1, 3)
    return ed >= seat


# ── anchor functions ───────────────────────────────────────────────────────────

ANCHOR_FNS: dict = {}


def anchor_fn(name):
    def deco(f):
        ANCHOR_FNS[name] = f
        return f
    return deco


@anchor_fn("dated_impeachment")
def _impeachment(params, days, end_date, question, ov):
    # Mechanical: an impeachment needs a House floor path inside the window.
    # If the controlling party is opposed and no path exists, the anchor is the
    # residual only; a window crossing the Jan-3 seating date gets the flip jump
    # at the SEATING date, not the election date (handoff §3.1).
    opposed = ov.get("controlling_party_opposed",
                     params.get("controlling_party_opposed", True))
    path = ov.get("floor_path_exists", False)
    crosses = _crosses_next_seating(end_date)
    p = params.get("base_prob", 0.005)
    if path:
        p = max(p, params.get("path_prob", 0.10))
    elif crosses:
        p += params.get("seating_jump", 0.02)
    if not opposed:
        p = max(p, params.get("friendly_chamber_prob", 0.03))
    inputs = {"controlling_party_opposed": opposed, "floor_path_exists": path,
              "window_crosses_seating": crosses, "days_remaining": days}
    note = ("floor path exists" if path else
            "no floor path; window crosses seating" if crosses else
            "no floor path inside window")
    return min(p, 0.5), inputs, note, 1


@anchor_fn("ceasefire_by_date")
def _ceasefire(params, days, end_date, question, ov):
    # Polymarket's standard ceasefire rule: in effect AND holding 10 consecutive
    # days. Mechanical floor: signing buffer + hold. When days_remaining is
    # below that floor and no deal is signed, YES is near-impossible — tier 1.
    buf = params.get("signing_buffer_days", 3)
    hold = params.get("hold_days", 10)
    floor_days = buf + hold
    signed = ov.get("deal_signed", False)
    stage = ov.get("negotiation_stage", "none")
    if signed:
        since = ov.get("holding_since")
        if since:
            try:
                held = (date.today() - date.fromisoformat(since)).days
            except Exception:
                held = 0
            need = max(0, hold - held)
            p, tier = (0.90, 1) if days >= need + 1 else (0.02, 1)
            note = f"signed, holding {held}d, need {need} more within {days}d"
        else:
            p, tier = (0.60, 2) if days >= hold + 1 else (0.02, 1)
            note = f"signed, hold not started, {days}d window"
    elif days < floor_days:
        p, tier = params.get("floor_prob", 0.0075), 1
        note = f"unsigned + {days}d < {floor_days}d mechanical floor"
    else:
        p = params.get("stage_probs", {}).get(stage, 0.03)
        tier = 3
        note = f"unsigned, stage={stage}"
    inputs = {"deal_signed": signed, "negotiation_stage": stage,
              "min_chain_days": floor_days, "days_remaining": days}
    return p, inputs, note, tier


@anchor_fn("constitutional_impossibility")
def _constitutional(params, days, end_date, question, ov):
    # Third terms, annexations, amendments: 2/3 of both chambers + 38 states
    # (or treaty + counterparty consent). Purest sentiment sponges. Residual
    # scales gently with window length; never literal zero.
    p = params.get("base_prob", 0.005) + params.get("per_year", 0.01) * min(1.0, days / 365)
    inputs = {"threshold": params.get("threshold_note",
              "2/3 both chambers + 38 states / treaty + ratification"),
              "days_remaining": days}
    return min(p, 0.02), inputs, "constitutional threshold unreachable in window", 1


_AGE_Q = {50: 0.004, 55: 0.006, 60: 0.009, 65: 0.013, 70: 0.021,
          75: 0.033, 80: 0.055, 85: 0.090}


def _q_annual(age: float) -> float:
    ks = sorted(_AGE_Q)
    if age <= ks[0]:
        return _AGE_Q[ks[0]]
    if age >= ks[-1]:
        return _AGE_Q[ks[-1]]
    lo = max(k for k in ks if k <= age)
    hi = min(k for k in ks if k >= age)
    if lo == hi:
        return _AGE_Q[lo]
    t = (age - lo) / (hi - lo)
    return _AGE_Q[lo] + t * (_AGE_Q[hi] - _AGE_Q[lo])


@anchor_fn("leader_out_by_date")
def _leader_out(params, days, end_date, question, ov):
    # removal (vote-threshold math) + resignation (base rate) + actuarial
    # death/incapacity, pro-rated to window. Heads of state beat table
    # averages -> mortality adjusted down (handoff §3.3).
    yrs = days / 365.0
    removal = ov.get("removal_annual", params.get("removal_annual", 0.002)) * yrs
    resign = ov.get("resign_annual", params.get("resign_annual", 0.02)) * yrs
    age = ov.get("age", params.get("default_age", 70))
    mort = 1 - (1 - _q_annual(age) * params.get("mortality_adj", 0.6)) ** yrs
    p = min(removal + resign + mort, 0.25)
    inputs = {"removal_comp": round(removal, 4), "resign_comp": round(resign, 4),
              "mortality_comp": round(mort, 4), "age": age, "days_remaining": days}
    return p, inputs, f"sum of components ({age}y, {days}d window)", 2


@anchor_fn("legal_outcome_by_date")
def _legal(params, days, end_date, question, ov):
    # P(remaining process chain completes inside window). Tier-3 template —
    # jurisdiction durations are operator-seeded; defaults are crude medians.
    stages = ["none", "investigation", "charged", "trial"]
    durations = {**{"none": 180, "investigation": 150, "charged": 270, "trial": 90},
                 **params.get("stage_durations", {}),
                 **ov.get("stage_durations", {})}
    stage = ov.get("process_stage", "none")
    idx = stages.index(stage) if stage in stages else 0
    needed = sum(durations[s] for s in stages[idx:])
    ratio = days / needed if needed else 0
    p = max(0.01, min(0.5, 0.35 * max(0.0, ratio - 0.4)))
    inputs = {"process_stage": stage, "needed_days_median": needed,
              "days_remaining": days}
    return p, inputs, f"stage={stage}, needs ~{needed}d median vs {days}d window", 3


# ── evaluation ─────────────────────────────────────────────────────────────────

def evaluate(condition_id: str, slug: str, question: str,
             days_remaining: Optional[int], end_date: str) -> Optional[AnchorResult]:
    """Structural anchor for one market, or None if no family matches.
    Manual overrides beat family models (handoff §3)."""
    ov, ov_key = _override_for(condition_id, slug)
    fam = _match(question)

    if ov and "anchor" in ov:
        return AnchorResult(
            family=(fam["family"] if fam else "manual"),
            tier=int(ov.get("tier", 2)),
            prob=float(ov["anchor"]),
            inputs={"override_key": ov_key},
            note=f"manual override: {ov.get('note', '')}",
            manual=True)

    if not fam:
        return None
    days = days_remaining if days_remaining is not None else 365
    fn = ANCHOR_FNS.get(fam.get("anchor_fn", ""))
    if not fn:
        log.error("Family %s references unknown anchor_fn %s",
                  fam["family"], fam.get("anchor_fn"))
        return None
    try:
        prob, inputs, note, tier = fn(fam.get("params", {}), days, end_date,
                                      question, ov or {})
    except Exception as e:
        log.error("Anchor %s failed on %s: %s", fam["family"], question[:50], e)
        return None
    prob = max(0.002, min(0.98, prob))   # never literal 0/1 — residual always
    return AnchorResult(fam["family"], tier, prob, inputs, note)
