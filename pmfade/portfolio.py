"""
portfolio.py — the active strategy set.
Add an edge = import it and append it here. Nothing else changes.
"""
from .strategies.news_fade import NewsFade
from .strategies.settlement_lag import SettlementLag
from .strategies.longshot_bias import LongshotBias
from .strategies.correlated_lag import CorrelatedLag


def build_portfolio():
    strategies = [
        NewsFade(),
        SettlementLag(),
        LongshotBias(),
        CorrelatedLag(),
    ]
    return [s for s in strategies if s.enabled]
