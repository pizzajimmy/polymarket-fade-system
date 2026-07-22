"""
base.py — the Signal / MarketView / Strategy contract
=====================================================
Every edge in the portfolio is a Strategy that turns a MarketView (one market
plus precomputed features) into zero or more Signals. The engine handles all
plumbing — persistence, cooldown, follow-up tracking, alerts — so a strategy is
just its detection logic. Adding an edge = adding one `evaluate` method.

A Signal is a *claim*: "buy `side` of this market at ~`entry_price`, with this
strategy's internal `score`." Whether that claim has edge is NOT asserted here —
it is measured later by the calibration layer from the follow-up tracks. That
separation is the whole point: strategies propose, calibration disposes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import ModuleType
from typing import Optional, Union


# ── Signal ─────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    condition_id: str
    question:     str
    url:          str
    side:         str               # "YES" | "NO" — which token to buy
    entry_price:  float             # cents you would pay for `side` right now
    score:        float             # 0..100 strategy-internal confidence
    features:     dict = field(default_factory=dict)
    rationale:    str = ""
    strategy_id:  str = ""          # stamped by Strategy.run()

    def __post_init__(self):
        if self.side not in ("YES", "NO"):
            raise ValueError(f"side must be YES or NO, got {self.side!r}")


# ── MarketView ─────────────────────────────────────────────────────────────────

@dataclass
class MarketView:
    # raw (from the API / store)
    condition_id: str
    question:     str
    slug:         str
    url:          str
    token_id_yes: str
    category:     str
    end_date:     str
    yes_price:    float             # cents, 0..100
    volume_24h:   float
    liquidity:    float
    # precomputed by the engine (so strategies don't re-query)
    prev_24h:           Optional[float] = None
    ambient_vol:        Optional[float] = None   # mean |move| per reading (see note)
    vol_spike:          float = 1.0              # 24h vol vs 7d avg
    days_to_resolution: Optional[int] = None
    reading_count:      int = 0
    window_min:         Optional[float] = None
    window_max:         Optional[float] = None
    # edge-v2 fields (populated from the Gamma payload; defaults keep old
    # synthetic-universe tests valid)
    description:        str = ""
    event_id:           str = ""
    event_slug:         str = ""
    neg_risk:           int = 0
    fees_enabled:       int = 0
    best_bid:           Optional[float] = None   # cents
    best_ask:           Optional[float] = None   # cents
    volume_total:       float = 0.0              # lifetime USDC (gate: EV2_MIN_VOLUME)

    @property
    def no_price(self) -> float:
        return 100.0 - self.yes_price

    @property
    def drop_24h(self) -> Optional[float]:
        return (self.prev_24h - self.yes_price) if self.prev_24h is not None else None

    @property
    def rise_24h(self) -> Optional[float]:
        return (self.yes_price - self.prev_24h) if self.prev_24h is not None else None


# ── Context handed to every strategy each cycle ────────────────────────────────

@dataclass
class Context:
    store:    ModuleType                 # pmfade.store
    universe: list[MarketView]           # all tradeable views this cycle (for cross-market strategies)
    cache:    dict = field(default_factory=dict)   # per-cycle memo (e.g. precomputed triggers)


# ── Strategy ───────────────────────────────────────────────────────────────────

def executable_entry(side: str, best_bid, best_ask):
    """The AGGRESSIVE fill price in cents for buying `side` (cross the spread),
    or None if the quote is missing. Buying YES pays the YES ask; buying NO pays
    the NO ask = 100 − YES bid. Recorded on signals so calibration can re-grade
    edges at fillable prices, not the mid the strategy assumed."""
    if side == "YES":
        return round(best_ask, 1) if best_ask is not None else None
    return round(100 - best_bid, 1) if best_bid is not None else None


SignalOut = Union[Signal, list[Signal], None]


class Strategy(ABC):
    id: str = "base"
    enabled: bool = True
    cooldown_hours: float = 12.0         # engine skips re-firing within this window

    @abstractmethod
    def evaluate(self, mv: MarketView, ctx: Context) -> SignalOut:
        """Return Signal(s) for this market, or None."""

    def run(self, mv: MarketView, ctx: Context) -> list[Signal]:
        out = self.evaluate(mv, ctx)
        if out is None:
            return []
        sigs = [out] if isinstance(out, Signal) else list(out)
        for s in sigs:
            s.strategy_id = self.id
        return sigs
