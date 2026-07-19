"""
rate_anchor — statistical base-rate mispricings, as their own book.
==================================================================
The operator's earthquake trade, generalized: recurring events with stable
historical rates (earthquakes, hurricanes, …) where the market's implied
probability diverges from the base rate. Separated from edge_v2 so the two
books' records never contaminate each other:

  • FV = the anchor, PURE (no calibration-prior blend) — the strategy's whole
    identity is "model vs market"; the prior is logged for reference only.
  • BOTH directions allowed: buy whichever side the model says is underpriced.
    A base-rate model earns the directional call that edge_v2's fade-only
    rule exists to avoid.
  • Wide price zone [3, 97] and a LOWER volume floor — these are sleepy,
    low-attention markets; that's precisely why the edges survive.

Same cost stack as edge_v2 (fees when enabled, spread + budgeted book walk,
hardness haircut, annualized carry hurdle). Candidates persist to
edge_candidates tagged strategy_id='rate_anchor'.
Backtest the idea on resolved history: `python -m pmfade.rate_backtest`.
"""

from __future__ import annotations

import json
import logging

from .base import Strategy, Signal, MarketView, Context
from .. import config as C
from .. import anchors, hardness, market_calib
from .edge_v2 import _walk_book_usd

log = logging.getLogger("pmfade.rate_anchor")


class RateAnchor(Strategy):
    id = "rate_anchor"
    cooldown_hours = C.EV2_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        if not anchors.rate_family(mv.question):
            return None
        d = mv.days_to_resolution
        if d is None or d < C.RA_MIN_DAYS:
            return None
        if mv.volume_total < C.RA_MIN_VOLUME:
            return None
        cheap = min(mv.yes_price, mv.no_price)
        if not (3.0 <= cheap and mv.yes_price <= 97.0 and mv.yes_price >= 3.0):
            return None

        a = anchors.evaluate(mv.condition_id, mv.slug, mv.question, d, mv.end_date)
        if a is None:
            return None
        fv = a.prob * 100                                   # pure anchor
        prior_c, prior_q = market_calib.calibration_prior(mv.yes_price, mv.category, d)

        # direction: buy whichever side the model says is underpriced
        side = "YES" if fv > mv.yes_price else "NO"
        entry = mv.yes_price if side == "YES" else mv.no_price
        fv_side = fv if side == "YES" else 100 - fv
        edge_gross = fv_side - entry

        gates: dict[str, bool] = {}
        p_entry = entry / 100.0
        fee = (C.FEE_C.get(mv.category, C.FEE_C["other"]) * p_entry * (1 - p_entry) * 100
               if mv.fees_enabled else 0.0)
        if mv.best_bid is not None and mv.best_ask is not None:
            mid = (mv.best_bid + mv.best_ask) / 2
            spread_cost = (mv.best_ask - mid) if side == "YES" else (mid - mv.best_bid)
        else:
            spread_cost = 0.75

        gates["book_depth"] = True
        book_walked = False
        if edge_gross - fee - spread_cost > 0 and mv.token_id_yes:
            budget = ctx.cache.setdefault("ev2_book_budget", C.EV2_BOOK_FETCH_BUDGET)
            if budget > 0:
                ctx.cache["ev2_book_budget"] = budget - 1
                try:
                    from slippage import fetch_book
                    book = fetch_book(mv.token_id_yes)
                    levels = book.get("bids" if side == "NO" else "asks", [])
                    vwap, depth_usd = _walk_book_usd(levels, C.NOMINAL_CLIP_USDC,
                                                     side_no=(side == "NO"))
                    book_walked = True
                    if vwap is not None:
                        spread_cost = max(spread_cost, vwap - entry)
                    gates["book_depth"] = depth_usd >= C.EV2_BOOK_DEPTH_MULT * C.NOMINAL_CLIP_USDC
                except Exception as e:
                    log.debug("book walk failed %s: %s", mv.condition_id[:10], e)

        edge_net = edge_gross - fee - spread_cost
        h, _flags = hardness.score(mv.description)
        edge_net_h = edge_net * h / 100.0
        expected_hold = max(1.0, C.EV2_CONVERGENCE_FRACTION * d)
        ann = (edge_net_h / entry) * (365.0 / expected_hold) if entry > 0 else 0.0

        gates["positive_net"] = edge_net_h > 0
        gates["hardness"] = h >= C.HARDNESS_FLOOR
        gates["hurdle"] = ann >= C.EV2_CARRY_HURDLE
        emit = all(gates.values())
        score = max(20.0, min(90.0, 40 + 300 * ann))

        ctx.store.insert_edge_candidate({
            "strategy_id": self.id,
            "condition_id": mv.condition_id, "question": mv.question, "side": side,
            "market_price": round(mv.yes_price, 2), "fv": round(fv, 2),
            "anchor": round(fv, 2),
            "prior": round(prior_c, 2) if prior_c is not None else None,
            "blend_w": 1.0, "family": a.family, "tier": a.tier,
            "edge_gross": round(edge_gross, 2), "fee_cost": round(fee, 3),
            "spread_cost": round(spread_cost, 2), "edge_net": round(edge_net_h, 2),
            "edge_net_annualized": round(ann, 4), "hardness": h,
            "gates_passed": json.dumps({**gates, "book_walked": book_walked}),
            "emitted": 1 if emit else 0,
        })
        if not emit:
            return None

        features = {
            "fv": round(fv, 2), "anchor": round(fv, 2),
            "prior_ref": prior_c, "prior_quality": prior_q,
            "family": a.family, "tier": a.tier,
            "anchor_inputs": a.inputs, "anchor_note": a.note,
            "edge_gross": round(edge_gross, 2), "fee_cost": round(fee, 3),
            "spread_cost": round(spread_cost, 2), "edge_net": round(edge_net_h, 2),
            "annualized": round(ann, 3), "hardness": h,
            "days_to_res": d, "book_walked": book_walked,
            "shape": "model",
        }
        rationale = (f"{a.inputs.get('event','rate')} model {fv:.0f}¢ vs market "
                     f"{mv.yes_price:.0f}¢ → buy {side} @ {entry:.1f}¢, "
                     f"net {edge_net_h:+.1f}¢, {ann*100:.0f}%/yr")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      round(score, 0), features, rationale)
