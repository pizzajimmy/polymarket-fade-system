"""
edge_v2 — Module D: anchored, cost-aware edge scorer.
=====================================================
FV = w·anchor + (1−w)·calibration_prior, w from the anchor's confidence tier
(0.8 mechanical / 0.6 base-rate / 0.4 judgment; no family → prior only, and
the signal score is capped at 65 — prior-only edges are weaker claims).

The tradeable shape (handoff §5): the CHEAP side sits in the longshot zone
[3, 20]¢ and FV says it's OVERPRICED → buy the expensive side (fade the
longshot / buy the underpriced favorite). If FV instead says the cheap side
is UNDERpriced, buying it is a directional call — logged as a candidate,
never emitted (route-to-operator by table, since we're signal-only).

Cost stack, subtracted in order:
  1. fees      — C·p·(1−p) per share, only when the market's feesEnabled flag
                 is set (coefficients per inferred category; verify vs docs).
  2. spread    — half-spread from the stored top-of-book for everyone, plus a
                 real CLOB book walk (VWAP at NOMINAL_CLIP + depth check) for
                 the shortlist, budgeted per cycle. NB: CLOB book sizes are
                 SHARES; USD at a level = price × size — the legacy
                 slippage.simulate_fill treats size as USD, so we walk the
                 book ourselves here.
  3. hardness  — edge_net × hardness/100 (Module C haircut).
  4. carry     — annualized on the haircut edge with
                 expected_hold = EV2_CONVERGENCE_FRACTION × days_to_res;
                 gate at EV2_CARRY_HURDLE.

Every candidate passing the pre-gates is persisted to edge_candidates —
that table, not the emitted signals, is the backtest substrate.
"""

from __future__ import annotations

import json
import logging

from .base import Strategy, Signal, MarketView, Context
from .. import config as C
from .. import anchors, hardness, market_calib

log = logging.getLogger("pmfade.edge_v2")


def _walk_book_usd(levels: list, clip_usd: float, side_no: bool) -> tuple[float | None, float]:
    """Walk one side of the CLOB book. `levels` are [price_str, size_shares_str].
    Returns (vwap_cents_for_our_side, usd_depth_within_impact_bound).
    For NO purchases we fill against YES *bids*: our price per share = 1 − bid.

    NB the raw CLOB /book returns levels sorted BEST-LAST (bids ascending,
    asks descending) — discovered live 2026-07 after months of the legacy
    tooling silently walking from the worst level. Sort defensively so OUR
    price ascends regardless of API order: asks by price asc, bids desc."""
    levels = sorted(levels, key=lambda l: float(l[0] if isinstance(l, list) else l["price"]),
                    reverse=side_no)
    filled = 0.0     # USD spent toward the clip
    shares = 0.0
    depth_usd = 0.0  # USD available within EV2_MAX_IMPACT_CENTS of top
    top_ours = None
    for lvl in levels:
        price_s, size_s = (lvl[0], lvl[1]) if isinstance(lvl, list) else (lvl["price"], lvl["size"])
        p = float(price_s)                      # YES-side price, 0..1
        ours = (1 - p) if side_no else p        # what WE pay per share, 0..1
        if ours <= 0:
            continue
        if top_ours is None:
            top_ours = ours
        usd_at_level = ours * float(size_s)     # CLOB sizes are SHARES
        if (ours - top_ours) * 100 <= C.EV2_MAX_IMPACT_CENTS:
            depth_usd += usd_at_level
        if filled < clip_usd:
            take = min(usd_at_level, clip_usd - filled)
            shares += take / ours
            filled += take
    vwap = (filled / shares * 100) if shares else None
    return vwap, depth_usd


def compute_fv(mv: MarketView):
    """Shared fair-value blend (edge_v2 + news_fade_v2 use the same machinery).
    Returns (fv_cents, meta dict) or (None, meta) when no basis exists."""
    d = mv.days_to_resolution
    a = anchors.evaluate(mv.condition_id, mv.slug, mv.question, d, mv.end_date)
    prior_c, prior_q = market_calib.calibration_prior(mv.yes_price, mv.category, d)
    if a is None and prior_c is None:
        return None, {"anchor": None, "prior": None, "prior_quality": prior_q,
                      "blend_w": 0.0, "anchor_res": None, "basis": "none"}
    if a is not None:
        w = C.BLEND_W.get(a.tier, 0.4)
        anchor_c = a.prob * 100
        fv = w * anchor_c + (1 - w) * (prior_c if prior_c is not None else mv.yes_price)
        basis = "anchor"
    else:
        w, anchor_c, fv = 0.0, None, prior_c
        basis = "prior"
    return fv, {"anchor": anchor_c, "prior": prior_c, "prior_quality": prior_q,
                "blend_w": w, "anchor_res": a, "basis": basis}


class EdgeV2(Strategy):
    id = "edge_v2"
    cooldown_hours = C.EV2_COOLDOWN_HRS

    def evaluate(self, mv: MarketView, ctx: Context):
        d = mv.days_to_resolution
        cheap = min(mv.yes_price, mv.no_price)

        # Rate-anchored markets (earthquakes/storms/…) belong to the rate_anchor
        # strategy — excluded here entirely so the two books' records never mix.
        if anchors.rate_family(mv.question):
            return None

        # pre-gates: only markets in the tradeable fade shape get candidate rows
        if not (C.EV2_LONGSHOT_LO <= cheap <= C.EV2_LONGSHOT_HI):
            return None
        if mv.volume_total < C.EV2_MIN_VOLUME:
            return None
        if d is None or d < C.EV2_MIN_DAYS_CARRY:
            return None

        # ── fair value ─────────────────────────────────────────────────────────
        fv, meta = compute_fv(mv)
        if fv is None:
            return None                                    # no FV basis
        a = meta["anchor_res"]
        w, anchor_c, prior_c, prior_q = (meta["blend_w"], meta["anchor"],
                                         meta["prior"], meta["prior_quality"])

        # ── direction: fade the overpriced cheap side only (handoff Module D).
        # "Cheap side underpriced" is a directional call — that shape lives in
        # the rate_anchor strategy where a base-rate model earns it.
        cheap_is_yes = mv.yes_price <= mv.no_price
        cheap_fv = fv if cheap_is_yes else 100 - fv
        gates: dict[str, bool] = {}
        # STRUCTURAL-FV GATE (added 2026-07 on evidence). With no anchor family,
        # FV collapses to the calibration prior — a price-LEVEL statistic
        # ("markets at 8c resolve YES 3% of the time"), not a market-specific
        # fair value. Trading it just re-derives longshot_bias with extra steps
        # (the v1-vs-v2 overlap section shows the two taking identical trades),
        # and it is the same conceptual error already removed from
        # news_fade_v2. Measured: prior-only resolved -41c/trade [-73.7, -8.4]
        # at n=10, with anchor calibration flagging prior-only signals
        # overconfident (70% implied vs 40% realized). Candidates are still
        # logged for analysis; they just no longer emit.
        gates["structural_fv"] = (meta["basis"] == "anchor")
        gates["shape"] = cheap_fv < cheap                    # cheap side overpriced
        side = ("NO" if cheap_is_yes else "YES")             # buy the expensive side
        entry = mv.no_price if side == "NO" else mv.yes_price
        fv_side = (100 - fv) if side == "NO" else fv
        edge_gross = fv_side - entry                         # >0 when shape holds

        # ── cost stack ─────────────────────────────────────────────────────────
        p_entry = entry / 100.0
        fee = (C.FEE_C.get(mv.category, C.FEE_C["other"]) * p_entry * (1 - p_entry) * 100
               if mv.fees_enabled else 0.0)

        if mv.best_bid is not None and mv.best_ask is not None:
            mid = (mv.best_bid + mv.best_ask) / 2
            spread_cost = (mv.best_ask - mid) if side == "YES" else (mid - mv.best_bid)
        else:
            spread_cost = 0.75                               # unknown book: assume typical

        gates["book_depth"] = True
        book_walked = False
        if gates["shape"] and edge_gross - fee - spread_cost > 0 and mv.token_id_yes:
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

        # score: annualized carry scaled; prior-only capped at 65 (weaker claim)
        score = max(20.0, min(95.0, 40 + 300 * ann))
        if a is None:
            score = min(score, 65.0)

        ctx.store.insert_edge_candidate({
            "condition_id": mv.condition_id, "question": mv.question, "side": side,
            "market_price": round(mv.yes_price, 2), "fv": round(fv, 2),
            "anchor": round(anchor_c, 2) if anchor_c is not None else None,
            "prior": round(prior_c, 2) if prior_c is not None else None,
            "blend_w": w, "family": (a.family if a else None),
            "tier": (a.tier if a else None),
            "edge_gross": round(edge_gross, 2), "fee_cost": round(fee, 3),
            "spread_cost": round(spread_cost, 2), "edge_net": round(edge_net_h, 2),
            "edge_net_annualized": round(ann, 4), "hardness": h,
            "gates_passed": json.dumps({**gates, "book_walked": book_walked}),
            "emitted": 1 if emit else 0,
        })

        if not emit:
            return None

        features = {
            "fv": round(fv, 2), "anchor": anchor_c, "prior": prior_c,
            "prior_quality": prior_q, "blend_w": w,
            "family": (a.family if a else None), "tier": (a.tier if a else None),
            "anchor_inputs": (a.inputs if a else None),
            "anchor_note": (a.note if a else None),
            "edge_gross": round(edge_gross, 2), "fee_cost": round(fee, 3),
            "spread_cost": round(spread_cost, 2), "edge_net": round(edge_net_h, 2),
            "annualized": round(ann, 3), "hardness": h,
            "days_to_res": d, "expected_hold_d": round(expected_hold, 1),
            "book_walked": book_walked,
            "shape": "fade",
        }
        rationale = (f"{mv.yes_price:.0f}¢ vs FV {fv:.1f}¢ "
                     f"[{(a.family if a else 'prior-only')}] → buy {side} @ {entry:.1f}¢, "
                     f"net {edge_net_h:+.1f}¢, {ann*100:.0f}%/yr")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      round(score, 0), features, rationale)
