"""
longshot_bias — systematically fade overpriced longshots.
=========================================================
Prediction markets exhibit favorite-longshot bias: low-probability outcomes
trade richer than they should. The edge is statistical and lives in the BASKET,
not any single market — most longshots correctly don't happen, you collect the
small premium, and the rare hit is paid for by the many misses.

So this strategy does NOT assert per-market edge. It flags candidates in a band
where the bias is best documented (cheap but not dust, distant resolution,
liquid enough to be a real market) and buys NO. Whether the basket actually has
positive EV is exactly what the calibration layer is built to answer — fire it,
track it to resolution, and let the win/loss tally settle it.
"""

from __future__ import annotations

from .base import Strategy, Signal, MarketView, Context, executable_entry
from .. import config as C

# the bias is a political/economic-belief phenomenon; crypto/sports price-action
# longshots are a different animal, so steer clear of them here.
_ALLOWED = {"politics", "macro", "stocks", "science"}


class LongshotBias(Strategy):
    id = "longshot_bias"
    cooldown_hours = C.LS_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        if mv.category not in _ALLOWED:
            return None
        if mv.liquidity < C.LS_MIN_LIQUIDITY:
            return None
        if not (C.LS_PRICE_LO <= mv.yes_price <= C.LS_PRICE_HI):
            return None
        d = mv.days_to_resolution
        if d is None or d < C.LS_MIN_DAYS_TO_RES:
            return None

        entry = mv.no_price                       # buy NO on the longshot
        gain = 100 - entry                        # = yes_price, the premium collected
        ret = gain / entry
        annual_yield = ret * (365.0 / max(d, 1))

        # Modest, deliberately flat score — we don't pretend to rank these well.
        # Slight nudge for liquidity and for the mid-band (cleanest bias zone).
        midband = 1.0 - abs(mv.yes_price - (C.LS_PRICE_LO + C.LS_PRICE_HI) / 2) / \
                  ((C.LS_PRICE_HI - C.LS_PRICE_LO) / 2)
        score = 45 + 10 * midband + (8 if mv.liquidity >= 10000 else 0)

        ee = executable_entry("NO", mv.best_bid, mv.best_ask)
        spread = (round(mv.best_ask - mv.best_bid, 2)
                  if mv.best_bid is not None and mv.best_ask is not None else None)
        features = {
            "yes_price":    round(mv.yes_price, 1),
            "entry_no":     round(entry, 1),
            "premium_cents": round(gain, 2),
            "days_to_res":  d,
            "annual_yield": round(annual_yield, 3),
            "liquidity":    round(mv.liquidity, 0),
            "category":     mv.category,
            # fillability: exec_entry = the price you'd actually pay to buy NO
            "best_bid":     mv.best_bid,
            "best_ask":     mv.best_ask,
            "spread":       spread,
            "exec_entry":   ee,
            "fill_haircut": round(ee - entry, 2) if ee is not None else None,
        }
        rationale = (f"longshot {mv.yes_price:.1f}¢, {d}d → buy NO @ {entry:.1f}¢ "
                     f"(collect {gain:.1f}¢ premium)")
        return Signal(mv.condition_id, mv.question, mv.url, "NO", round(entry, 1),
                      round(score, 0), features, rationale)
