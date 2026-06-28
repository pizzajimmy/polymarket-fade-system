"""
settlement_lag — buy the near-certain side of an effectively-decided market.
============================================================================
A market pinned within a few cents of 0/100, stable for several readings, with
resolution weeks-to-months out, is often *effectively decided* but trades a few
cents off the boundary because capital is locked up until settlement. Buying the
certain side collects that gap.

This REPLACES the old stale_extreme math, which reported `edge_pts = 100 - price`
(≈99) for a 1¢ longshot — treating distance-to-boundary as edge when it is
actually risk. Here the edge is honest carry: buying YES at 97¢ risks the whole
97¢ to gain 3¢, so it is a *yield* play whose worth is the annualized return,
and whose tail is the rare flip. The score reflects that; calibration measures
how often the "certain" side actually wins (the only thing that matters).

Note the time filter (≤ SL_MAX_DAYS_TO_RES) is what excludes the LeBron-2028 /
Jesus-return longshots — they're correctly-priced, not stale.
"""

from __future__ import annotations

from .base import Strategy, Signal, MarketView, Context
from .. import config as C


class SettlementLag(Strategy):
    id = "settlement_lag"
    cooldown_hours = C.SL_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        if mv.liquidity < C.SL_MIN_LIQUIDITY:
            return None
        d = mv.days_to_resolution
        if d is None or d < C.SL_MIN_DAYS_TO_RES or d > C.SL_MAX_DAYS_TO_RES:
            return None
        if mv.reading_count < C.SL_MIN_READINGS or mv.window_min is None:
            return None

        band = C.SL_EXTREME
        if mv.yes_price >= 100 - band:
            # near-certain YES — must have stayed pinned high (stability)
            if mv.window_min < (100 - band) - 2:
                return None
            side, entry, certain = "YES", mv.yes_price, "YES"
        elif mv.yes_price <= band:
            # near-certain NO — must have stayed pinned low
            if mv.window_max > band + 2:
                return None
            side, entry, certain = "NO", mv.no_price, "NO"
        else:
            return None

        gain = 100 - entry                       # cents collected if it settles as expected
        if gain <= 0.5:                          # dust — not worth the slot
            return None
        ret = gain / entry                       # return on capital at risk
        annual_yield = ret * (365.0 / max(d, 1))
        if annual_yield < C.SL_MIN_ANNUAL_YIELD:
            return None

        # Score: reward annualized carry, lightly reward proximity-to-boundary
        # (more "certain"). Capped — a very large gap means the market prices real
        # tail risk, not staleness, so don't let it dominate.
        score = min(90, 35 + 220 * min(annual_yield, 0.6) + (band - (100 - mv.yes_price
                    if side == "YES" else mv.yes_price)) * 2)
        score = max(20, score)

        features = {
            "side_certain":  certain,
            "entry":         round(entry, 1),
            "gain_cents":    round(gain, 2),
            "yes_price":     round(mv.yes_price, 1),
            "days_to_res":   d,
            "return_pct":    round(ret * 100, 2),
            "annual_yield":  round(annual_yield, 3),
            "liquidity":     round(mv.liquidity, 0),
            "readings":      mv.reading_count,
            "window_min":    mv.window_min,
            "window_max":    mv.window_max,
            "category":      mv.category,
        }
        rationale = (f"pinned {mv.yes_price:.1f}¢, {d}d to res, "
                     f"{annual_yield*100:.0f}%/yr carry → buy {side} @ {entry:.1f}¢")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      round(score, 0), features, rationale)
