"""
correlated_lag — trade the market that hasn't repriced yet.
===========================================================
When a market moves hard on news, topically-related markets often lag before
catching up. Ported from the original find_lagging_correlated, but reframed to
fit the per-market model: for THIS market, ask "is it a laggard?" — i.e. did a
keyword-related market move sharply in the last 24h while this one stayed put?

Assumes same-direction correlation (the original's heuristic): if the trigger
DROPPED, the laggard is holding too high → buy NO; if it SPIKED, buy YES.
That assumption is itself a hypothesis the calibration layer will test.
"""

from __future__ import annotations

import re
from .base import Strategy, Signal, MarketView, Context
from .. import config as C

_STOP = {
    "will", "the", "a", "an", "in", "on", "at", "by", "for", "of", "to", "be",
    "is", "was", "are", "were", "have", "has", "had", "do", "does", "did", "and",
    "or", "but", "not", "this", "that", "from", "with", "before", "after", "during",
    "between", "about", "than", "more", "less", "happen", "occur", "win", "lose",
    "next", "last", "first", "new", "any", "all", "his", "her", "their", "which",
    "who", "what", "when", "where", "how", "be", "as", "if", "up", "out", "no",
}


def _keywords(question: str, max_kw: int = 8) -> set[str]:
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", question or "").lower().split()
    kws = [w for w in words if w not in _STOP and len(w) >= 3]
    kws.sort(key=len, reverse=True)
    return set(kws[:max_kw])


def _triggers(ctx: Context):
    """Markets that moved sharply this cycle, with keywords — computed once."""
    if "cl_triggers" in ctx.cache:
        return ctx.cache["cl_triggers"]
    trig = []
    for v in ctx.universe:
        if v.prev_24h is None:
            continue
        move = v.yes_price - v.prev_24h
        if abs(move) >= C.CL_TRIGGER_MOVE:
            trig.append((v, move, _keywords(v.question)))
    ctx.cache["cl_triggers"] = trig
    return trig


class CorrelatedLag(Strategy):
    id = "correlated_lag"
    cooldown_hours = C.CL_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        if mv.liquidity < C.CL_MIN_LIQUIDITY or mv.prev_24h is None:
            return None
        # the laggard must itself be roughly flat
        if abs(mv.yes_price - mv.prev_24h) >= C.CL_LAG_THRESHOLD:
            return None

        triggers = _triggers(ctx)
        if not triggers:
            return None

        my_kw = _keywords(mv.question)
        if len(my_kw) < C.CL_MIN_OVERLAP:
            return None

        best = None
        for tv, move, tkw in triggers:
            if tv.condition_id == mv.condition_id:
                continue
            overlap = my_kw & tkw
            if len(overlap) < C.CL_MIN_OVERLAP:
                continue
            if best is None or abs(move) > abs(best[1]):
                best = (tv, move, overlap)

        if best is None:
            return None

        tv, move, overlap = best
        if move < 0:                      # trigger dropped → laggard holding too high
            side, entry = "NO", mv.no_price
        else:                             # trigger spiked → laggard should rise
            side, entry = "YES", mv.yes_price

        score = min(80, 40 + abs(move) + 5 * len(overlap)
                    + (8 if mv.liquidity >= 10000 else 0))
        features = {
            "yes_price":      round(mv.yes_price, 1),
            "trigger_move":   round(move, 1),
            "trigger_q":      tv.question[:120],
            "overlap":        sorted(overlap),
            "liquidity":      round(mv.liquidity, 0),
            "category":       mv.category,
        }
        rationale = (f"flat while related market moved {move:+.0f}pts "
                     f"[{', '.join(sorted(overlap)[:3])}] → buy {side} @ {entry:.1f}¢")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      round(score, 0), features, rationale)
