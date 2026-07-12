"""
news_fade_v2 — Module E: fade discrimination, operator-in-the-loop.
===================================================================
A spike alone is never a trade (handoff §6). The protocol, adapted to a
30-minute scan cadence and NO news feed (operator decision):

  scan T   — spike/drop detected → write a pending_fades row, emit NOTHING.
             The first 30–90 min are the panic phase; the wick is not the
             fade reference.
  scan T+n — first scan ≥ FADE_COOLDOWN_MIN later (≈60 min at 30-min cadence):
             gate 1  deviation: price must still sit ≥ FADE_MIN_DEVIATION_PTS
                     from fair value (edge_v2's blend when a family/prior
                     exists; else the pre-move 24h price — v1 semantics),
                     in the fade direction.
             gate 2  hold-to-resolution viability (§6.5, non-negotiable):
                     the fade entry must be +EV held to expiry per that FV —
                     the reversion is an acceleration on a position we'd own
                     anyway, never a standalone timing bet.
             pass → Signal + operator-confirm Telegram listing the family's
                     structural inputs ("fade only if none of these changed").
                     The HUMAN is the input-change classifier; a `news_context`
                     hook stays open for a future feed.
             fail → row marked suppressed with the failing gate.

  Pending rows older than FADE_PENDING_EXPIRE_HRS expire unactioned.

v1 news_fade keeps running untouched — its signals are the comparison
baseline for this gate stack (handoff §13 / Phase-2 acceptance).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .base import Strategy, Signal, MarketView, Context
from .. import config as C
from .. import alerts
from .edge_v2 import compute_fv

log = logging.getLogger("pmfade.news_fade_v2")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NewsFadeV2(Strategy):
    id = "news_fade_v2"
    cooldown_hours = C.NF_COOLDOWN_HRS

    # ── detection (same shape as v1: 24h move + volume confirmation) ──────────
    def _detect(self, mv: MarketView):
        if mv.prev_24h is None:
            return None
        drop, rise = mv.drop_24h, mv.rise_24h
        if drop >= C.NF_DROP_THRESHOLD and (mv.vol_spike >= C.NF_VOLUME_SPIKE
                                            or drop >= C.NF_DROP_THRESHOLD + 5):
            return "DROP"
        if rise >= C.NF_SPIKE_THRESHOLD and (mv.vol_spike >= C.NF_VOLUME_SPIKE
                                             or rise >= C.NF_SPIKE_THRESHOLD + 5):
            return "SPIKE"
        return None

    def evaluate(self, mv: MarketView, ctx: Context):
        store = ctx.store
        if mv.liquidity < C.NF_MIN_LIQUIDITY or mv.volume_24h < C.NF_MIN_VOLUME_24H:
            return None
        if (mv.days_to_resolution is not None
                and mv.days_to_resolution < C.NF_MIN_DAYS_TO_RES):
            return None

        pending = store.get_pending_fade(mv.condition_id)
        if pending is None:
            direction = self._detect(mv)
            if direction:
                fv0, _ = compute_fv(mv)
                store.insert_pending_fade(mv.condition_id, direction,
                                          mv.yes_price, fv0)
                log.info("  [pending] %s %s @ %.1f¢ — cooling %.0fmin",
                         direction, mv.question[:40], mv.yes_price,
                         C.FADE_COOLDOWN_MIN)
            return None

        # ── a pending fade exists: cooling, expired, or decidable ─────────────
        detected = store.parse_iso(pending["detected_at"])
        age_min = ((_now() - detected).total_seconds() / 60) if detected else 1e9
        if age_min < C.FADE_COOLDOWN_MIN:
            return None
        if age_min > C.FADE_PENDING_EXPIRE_HRS * 60:
            store.decide_pending_fade(pending["id"], "expired", f"{age_min:.0f}min old")
            return None

        direction = pending["direction"]
        side = "YES" if direction == "DROP" else "NO"
        entry = mv.yes_price if side == "YES" else mv.no_price

        fv, meta = compute_fv(mv)
        basis = meta["basis"]
        if fv is None:
            fv, basis = pending["ref_price"] if direction == "SPIKE" else None, "none"
            # for a DROP with no FV machinery, the pre-move price is the reference
            if fv is None:
                fv, basis = mv.prev_24h, "prev24h"
            if fv is None:
                store.decide_pending_fade(pending["id"], "suppressed", "no FV basis")
                return None
        elif basis == "none":
            basis = "prev24h"
            fv = mv.prev_24h if mv.prev_24h is not None else pending["ref_price"]

        # gate 1: deviation from FV, in the fade direction, post-cooldown
        deviation = (fv - mv.yes_price) if direction == "DROP" else (mv.yes_price - fv)
        if deviation < C.FADE_MIN_DEVIATION_PTS:
            store.decide_pending_fade(pending["id"], "suppressed",
                                      f"deviation {deviation:.1f} < {C.FADE_MIN_DEVIATION_PTS}")
            return None

        # gate 2: hold-to-resolution viability — fade entry +EV at expiry per FV
        fv_side = fv if side == "YES" else 100 - fv
        ev_to_res = fv_side - entry
        if ev_to_res <= 2.0:                       # must clear ~cost held to expiry
            store.decide_pending_fade(pending["id"], "suppressed",
                                      f"not viable to resolution ({ev_to_res:+.1f}¢)")
            return None

        a = meta.get("anchor_res")
        inputs = (a.inputs if a else None)

        # ── news qualification (v2.1) — only for candidates that passed both
        # gates, so the RSS fetch + optional classifier run a few times a day
        # at most. Everything here fails open to the operator path.
        from .. import news, news_classify
        headlines = news.fetch_for_market(mv.question)
        cls = news_classify.classify(mv.question, direction, pending["ref_price"],
                                     mv.yes_price, (a.family if a else None),
                                     inputs, headlines)
        if (cls and cls.input_changed
                and cls.confidence >= C.FADE_CLASSIFIER_MIN_CONF):
            # information, not sentiment: suppress the fade, flag a re-anchor
            store.decide_pending_fade(
                pending["id"], "suppressed",
                f"input_changed:{cls.which_input} ({cls.confidence:.2f})")
            store.insert_structure_alert(
                "REANCHOR", mv.event_slug or mv.slug,
                f"{mv.question[:80]} — {cls.rationale}")
            alerts.send_telegram(
                f"🧷 <b>Re-anchor needed</b> (fade suppressed — input changed)\n"
                f"<i>{mv.question[:80]}</i>\n"
                f"{cls.which_input}: {cls.rationale}\n"
                f"Update anchors_manual.json for this market.")
            log.info("  [suppressed by classifier] %s: %s",
                     mv.question[:40], cls.which_input)
            return None

        store.decide_pending_fade(pending["id"], "promoted", f"dev {deviation:.1f}")

        # anchor/prior-based FV alerts (floor 80); fallback-basis stays silent-but-logged
        floor = 80 if basis in ("anchor", "prior") else 70
        score = min(95.0, floor + (deviation - C.FADE_MIN_DEVIATION_PTS))

        check_lines = ["Operator check — fade ONLY if none of these changed:"]
        if a:
            check_lines += [f"• {k} = {v}" for k, v in (inputs or {}).items()]
            check_lines.append(f"({a.family}: {a.note})")
        else:
            check_lines.append("• no anchor family matched — judge the news yourself")
        check_lines.append("")
        check_lines.append(news.headlines_block(headlines))
        if cls:
            check_lines.append(f"Classifier: no structural change detected "
                               f"(conf {cls.confidence:.2f}) — {cls.rationale}")
        features = {
            "direction": direction, "detected_at": pending["detected_at"],
            "ref_price": pending["ref_price"], "cooldown_min": round(age_min),
            "fv": round(fv, 1), "fv_basis": basis,
            "deviation": round(deviation, 1), "ev_to_resolution": round(ev_to_res, 1),
            "family": (a.family if a else None), "anchor_inputs": inputs,
            "operator_check": "\n".join(check_lines),
            "headlines_n": len(headlines),
            "classifier": (None if cls is None else
                           {"input_changed": cls.input_changed,
                            "confidence": cls.confidence,
                            "rationale": cls.rationale}),
        }
        rationale = (f"{direction} cooled {age_min:.0f}min, {deviation:.0f}pts off "
                     f"FV {fv:.0f}¢ [{basis}] → buy {side} @ {entry:.1f}¢")
        return Signal(mv.condition_id, mv.question, mv.url, side, round(entry, 1),
                      round(score, 0), features, rationale)
