"""
portfolio.py — the active strategy set.
Add an edge = import it and append it here. Nothing else changes.
"""
from .strategies.news_fade import NewsFade
from .strategies.settlement_lag import SettlementLag
from .strategies.longshot_bias import LongshotBias
from .strategies.correlated_lag import CorrelatedLag
from .strategies.edge_v2 import EdgeV2


def build_portfolio():
    strategies = [
        # v1 — keep flowing untouched for comparison (handoff §13)
        NewsFade(),
        SettlementLag(),
        LongshotBias(),
        CorrelatedLag(),
        # v2 — additive, not a replacement
        EdgeV2(),
    ]
    return [s for s in strategies if s.enabled]
