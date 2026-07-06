"""
hardness.py — Module C: resolution-rules hardness scorer.
=========================================================
Oracle/resolution risk is the dominant tail on a penny-collecting book
(handoff §4: 1,150+ UMA-disputed markets by mid-2026; the Zelenskyy-suit
market resolved against broad media description on $237M volume). This
scores each market's rules text 0–100 — hard/objective = high, mushy = low.

Keyword scoring only (v2.0). An optional LLM pass is a documented v2.1 seam;
it is deliberately NOT built: no API key on the server, and the collector's
survivability philosophy is stdlib-only.

Use: hardness < HARDNESS_FLOOR (60) ⇒ edge_v2 logs the candidate but never
emits a signal; above the floor it enters the edge score as a haircut
(edge_net × hardness/100).
"""

from __future__ import annotations

import re
import json
from functools import lru_cache

# Calibrated against LIVE Polymarket rules text (2026-07): "a consensus of
# credible reporting" is PM's standard FALLBACK clause and appears in nearly
# every description — with the original -25/-12 weights the scorer was a
# kill-switch (an NFL-championship market scored 26). Weights below make it a
# discriminator: reporting-as-primary-source stays penalized, reporting-as-
# fallback-behind-an-official-source is halved (see score()).
BASE = 55.0

# (name, compiled regex, points). Penalties negative, rewards positive.
_RULES: list[tuple[str, re.Pattern, float]] = [
    # ── penalties: interpretation surface ─────────────────────────────────────
    ("reporting_based",
     re.compile(r"(consensus of credible reporting|credible (?:media |news )?report)", re.I), -8),
    ("qualitative_noun",
     re.compile(r"\b(major|significant|substantial|serious|notable|widely)\b", re.I), -3),
    ("intent_interpretation",
     re.compile(r"\b(intend(?:s|ed)?|intention|seek(?:s|ing)? to|attempt(?:s|ed)? to)\b", re.I), -8),
    ("vague_event_noun",
     re.compile(r"\b(official visit|meeting between|suit against|talks?\b)", re.I), -5),
    ("discretion",
     re.compile(r"(sole discretion|may be resolved|reserves? the right)", re.I), -8),
    ("compound_conditions",
     re.compile(r"\b(?:and|or)\b.*\b(?:and|or)\b.*\b(?:and|or)\b", re.I | re.S), -4),
    # ── rewards: objective anchors ─────────────────────────────────────────────
    ("named_official_source",
     re.compile(r"(official (?:website|publication|data|figures|report)|roll[- ]call|"
                r"federal register|\.gov\b|government publication|"
                r"official information from|according to the "
                r"(?:[A-Z][\w]+ ){0,3}(?:website|filing|report|data))", re.I), +15),
    ("explicit_deadline",
     re.compile(r"(\d{1,2}:\d{2}\s*(?:am|pm)?\s*(?:ET|EST|EDT|PT|UTC|GMT)|11:59)", re.I), +8),
    ("edge_case_handling",
     re.compile(r"(for the avoidance of doubt|in the event (?:that|of)|"
                r"otherwise (?:this market )?(?:will )?resolves?|resolve 50[/-]50)", re.I), +6),
    ("explicit_resolver",
     re.compile(r"(resolution source|will resolve according to|resolver?:)", re.I), +10),
]


@lru_cache(maxsize=8192)
def score(description: str) -> tuple[float, str]:
    """(hardness 0–100, JSON list of fired rule names). Cached — pure function
    of the rules text, and descriptions rarely change."""
    if not description or len(description.strip()) < 40:
        # no rules text = unknowable = below the execution floor by design
        return 40.0, json.dumps(["no_rules_text"])
    s = BASE
    fired: list[str] = []
    hits_by = {}
    for name, rx, pts in _RULES:
        hits = len(rx.findall(description))
        hits_by[name] = hits
        if hits:
            # first hit full weight, repeats at half — a rule firing five times
            # shouldn't nuke/turbo the score linearly
            s += pts * (1 + 0.5 * (min(hits, 3) - 1))
            fired.append(name)
    # reporting-language behind a named official source is PM's fallback
    # boilerplate, not the primary resolution path — halve that penalty back
    if hits_by.get("reporting_based") and hits_by.get("named_official_source"):
        s += 4
        fired.append("reporting_is_fallback")
    # long, structured rules text is itself weak evidence of care
    if len(description) > 600:
        s += 4
        fired.append("detailed_rules")
    return max(0.0, min(100.0, round(s, 1))), json.dumps(fired)
