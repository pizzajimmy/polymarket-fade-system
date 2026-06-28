"""
news_fade — the original thesis, now as one graded strategy among many.
=======================================================================
Crowd overshoots on sentiment news → fade the move:
  • DROP (price fell ≥ threshold)  → buy YES (bet on the bounce)
  • SPIKE (price rose ≥ threshold) → buy NO  (bet on the giveback)

The raw signal is ~breakeven in aggregate (we measured it). The value is in
the SCORE: production's Quality Score rank-ordered reversion (top decile reverted
50% at +2.7pts vs ~0 at the bottom) but was never validated against outcomes.

Here the score is rebuilt transparently and — critically — every component is
written into `features`, so calibration can regress *which* components actually
predict reversion and re-weight them. The score is a prior; the tracks are truth.
"""

from __future__ import annotations

from .base import Strategy, Signal, MarketView, Context
from .. import config as C


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def quality_score(mv: MarketView, move: float, direction: str) -> tuple[float, dict]:
    """Transparent additive score in [20, 95]. Returns (score, components)."""
    entry = mv.yes_price if direction == "DROP" else mv.no_price
    dist_boundary = min(entry, 100 - entry)
    amb = mv.ambient_vol or 0.0

    comp = {
        # bigger overshoot = more to revert
        "move":      20 if move >= 20 else 13 if move >= 15 else 6 if move >= 10 else 0,
        # real money behind the move (not a thin-book blip)
        "vol_spike": 15 if mv.vol_spike >= 3 else 10 if mv.vol_spike >= C.NF_VOLUME_SPIKE
                     else 3 if mv.vol_spike >= 1.2 else 0,
        # tradeability
        "liquidity": 12 if mv.liquidity >= 10000 else 8 if mv.liquidity >= 3000
                     else 3 if mv.liquidity >= 1000 else -12,
        # entering near 0/100 = little room to revert + time-decay risk
        "boundary":  -15 if dist_boundary <= 8 else -7 if dist_boundary <= 15 else 0,
        # retail churn: thesis says fade it, but weight is small — calibration decides the sign
        "ambient":   6 if amb >= 5 else 3 if amb >= 2 else 0,
        # crypto is noisy
        "category":  -10 if mv.category == "crypto" else 0,
    }
    score = _clamp(40 + sum(comp.values()), 20, 95)
    return score, comp


class NewsFade(Strategy):
    id = "news_fade"
    cooldown_hours = C.NF_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        # gates
        if mv.liquidity < C.NF_MIN_LIQUIDITY:
            return None
        if mv.volume_24h < C.NF_MIN_VOLUME_24H:
            return None
        if mv.days_to_resolution is not None and mv.days_to_resolution < C.NF_MIN_DAYS_TO_RES:
            return None
        if mv.prev_24h is None:
            return None

        drop = mv.drop_24h
        rise = mv.rise_24h

        direction = None
        move = 0.0
        # DROP: needs volume confirmation OR a large standalone move
        if drop >= C.NF_DROP_THRESHOLD and (mv.vol_spike >= C.NF_VOLUME_SPIKE
                                            or drop >= C.NF_DROP_THRESHOLD + 5):
            direction, move = "DROP", drop
        elif rise >= C.NF_SPIKE_THRESHOLD and (mv.vol_spike >= C.NF_VOLUME_SPIKE
                                               or rise >= C.NF_SPIKE_THRESHOLD + 5):
            direction, move = "SPIKE", rise
        else:
            return None

        side = "YES" if direction == "DROP" else "NO"
        entry = mv.yes_price if side == "YES" else mv.no_price
        score, comp = quality_score(mv, move, direction)

        features = {
            "direction":   direction,
            "move_pts":    round(move, 1),
            "prev_24h":    round(mv.prev_24h, 1),
            "yes_price":   round(mv.yes_price, 1),
            "vol_spike":   round(mv.vol_spike, 2),
            "liquidity":   round(mv.liquidity, 0),
            "volume_24h":  round(mv.volume_24h, 0),
            "ambient_vol": round(mv.ambient_vol, 2) if mv.ambient_vol is not None else None,
            "days_to_res": mv.days_to_resolution,
            "category":    mv.category,
            "score_components": comp,
        }
        rationale = (f"{direction} {move:.0f}pts ({mv.prev_24h:.0f}→{mv.yes_price:.0f}¢), "
                     f"{mv.vol_spike:.1f}× vol, liq ${mv.liquidity:,.0f} → buy {side}")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      score, features, rationale)
