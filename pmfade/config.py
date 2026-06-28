"""
config.py — all tunable knobs in one place, env-overridable.
Strategy thresholds are *priors*, not truths — calibration tells you the real
ones. Keep defaults conservative and let the data move them.
"""
import os


def _f(k, d): return float(os.environ.get(k, d))
def _i(k, d): return int(os.environ.get(k, d))


# ── Engine ─────────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECS = _i("POLL_INTERVAL_SECS", 1800)     # 30 min
MIN_PRICE          = 1.0
MAX_PRICE          = 99.0
HEALTHCHECK_URL    = os.environ.get("HEALTHCHECK_URL", "")   # healthchecks.io ping

# Follow-up tracking horizons (label, hours-after-signal). 'resolution' is implicit.
TRACK_HORIZONS     = [("6h", 6), ("24h", 24), ("72h", 72), ("7d", 168)]
TRACK_WINDOW_DAYS  = 7      # how long a signal stays "open" for tracking

# Alerts: only signals at/above this score get a Telegram push.
NOTIFY_MIN_SCORE   = _f("NOTIFY_MIN_SCORE", 80)

# ── News-fade strategy ─────────────────────────────────────────────────────────
NF_DROP_THRESHOLD   = _f("DROP_THRESHOLD", 15)
NF_SPIKE_THRESHOLD  = _f("SPIKE_THRESHOLD", 15)
NF_VOLUME_SPIKE     = _f("VOLUME_SPIKE_RATIO", 1.8)
NF_MIN_VOLUME_24H   = _f("NF_MIN_VOLUME_24H", 500)
NF_MIN_LIQUIDITY    = _f("NF_MIN_LIQUIDITY", 1000)
NF_MIN_DAYS_TO_RES  = _i("NF_MIN_DAYS_TO_RES", 3)
NF_COOLDOWN_HRS     = _f("NF_COOLDOWN_HRS", 12)

# ── Settlement-lag strategy ────────────────────────────────────────────────────
SL_MIN_LIQUIDITY    = _f("SL_MIN_LIQUIDITY", 2000)
SL_EXTREME          = _f("SL_EXTREME", 4.0)     # within X cents of 0/100
SL_MIN_READINGS     = _i("SL_MIN_READINGS", 6)  # stability evidence in buffer
SL_MIN_DAYS_TO_RES  = _i("SL_MIN_DAYS_TO_RES", 10)
SL_MAX_DAYS_TO_RES  = _i("SL_MAX_DAYS_TO_RES", 120)
SL_MIN_ANNUAL_YIELD = _f("SL_MIN_ANNUAL_YIELD", 0.15)   # 15% annualized to bother
SL_COOLDOWN_HRS     = _f("SL_COOLDOWN_HRS", 48)

# ── Longshot-bias strategy ─────────────────────────────────────────────────────
LS_MIN_LIQUIDITY    = _f("LS_MIN_LIQUIDITY", 3000)
LS_PRICE_LO         = _f("LS_PRICE_LO", 4.0)    # longshot band: price in [LO, HI]
LS_PRICE_HI         = _f("LS_PRICE_HI", 12.0)
LS_MIN_DAYS_TO_RES  = _i("LS_MIN_DAYS_TO_RES", 14)
LS_COOLDOWN_HRS     = _f("LS_COOLDOWN_HRS", 72)

# ── Correlated-lag strategy ────────────────────────────────────────────────────
CL_MIN_LIQUIDITY    = _f("CL_MIN_LIQUIDITY", 1000)
CL_MIN_OVERLAP      = _i("CL_MIN_OVERLAP", 2)
CL_LAG_THRESHOLD    = _f("CL_LAG_THRESHOLD", 5.0)
CL_TRIGGER_MOVE     = _f("CL_TRIGGER_MOVE", 12.0)   # a market must move this much to be a trigger
CL_COOLDOWN_HRS     = _f("CL_COOLDOWN_HRS", 12)
