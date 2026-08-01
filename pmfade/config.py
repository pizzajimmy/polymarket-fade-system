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
# Restrict Telegram pushes to these strategy ids (comma-separated in env).
# Empty = alert on any strategy that clears NOTIFY_MIN_SCORE (the old behavior).
NOTIFY_STRATEGIES  = {s.strip() for s in
                      os.environ.get("NOTIFY_STRATEGIES", "").split(",") if s.strip()}


def notify_min_score(strategy: str) -> float:
    """Per-strategy score floor NOTIFY_MIN_SCORE_<STRATEGY>, else the global one.
    Read at call time so each strategy's bot can have its own threshold."""
    v = os.environ.get(f"NOTIFY_MIN_SCORE_{strategy.upper()}")
    if v is not None:
        try:
            return float(v)
        except ValueError:
            pass
    return NOTIFY_MIN_SCORE

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
# Max cents of spread we'll cross to enter. Gross edge measured ~+1.9c, so
# anything above ~1.5c is a losing trade before it starts.
LS_MAX_HAIRCUT      = _f("LS_MAX_HAIRCUT", 1.0)

# ── Correlated-lag strategy ────────────────────────────────────────────────────
CL_MIN_LIQUIDITY    = _f("CL_MIN_LIQUIDITY", 1000)
CL_MIN_OVERLAP      = _i("CL_MIN_OVERLAP", 2)
CL_LAG_THRESHOLD    = _f("CL_LAG_THRESHOLD", 5.0)
CL_TRIGGER_MOVE     = _f("CL_TRIGGER_MOVE", 12.0)   # a market must move this much to be a trigger
CL_COOLDOWN_HRS     = _f("CL_COOLDOWN_HRS", 12)

# ═══ Edge Engine v2 (signal-only; see pmfade/HANDOFF_polymarket_edge_v2.md) ════

# ── Backfill (Module A substrate) ──────────────────────────────────────────────
BACKFILL_MONTHS     = _i("BACKFILL_MONTHS", 12)      # how far back to walk resolved markets
BACKFILL_FUTURE_MO  = _i("BACKFILL_FUTURE_MO", 36)   # early-resolved markets carry far-future endDates
BACKFILL_MIN_VOLUME = _f("BACKFILL_MIN_VOLUME", 10000)
BACKFILL_RATE_S     = _f("BACKFILL_RATE_S", 0.35)    # throttle between prices-history calls

# ── Market calibration surface (Module A) ─────────────────────────────────────
# 1h horizon deliberately dropped: one daily-fidelity prices-history call per
# market can't resolve it, and published slope there is ~0.99 (no bias to model).
CALIB_HORIZONS      = [("24h", 24), ("7d", 168), ("30d", 720), ("90d", 2160)]
CALIB_MIN_CELL_N    = _i("CALIB_MIN_CELL_N", 50)
CALIB_RECOMPUTE_DAYS = _f("CALIB_RECOMPUTE_DAYS", 7)

# ── Fair-value blend (Module D) ────────────────────────────────────────────────
BLEND_W = {1: 0.8, 2: 0.6, 3: 0.4}   # anchor confidence tier -> weight; family none -> 0

# Polymarket taker fee per share ~= C * p * (1-p), applied only when the market's
# feesEnabled flag is set. Coefficients keyed by OUR inferred categories (official
# tags aren't exposed on /markets — geopolitics≈politics here). VERIFY against
# docs.polymarket.com/trading/fees before trusting to the cent.
FEE_C = {"politics": 0.04, "crypto": 0.07, "stocks": 0.04, "macro": 0.05,
         "science": 0.05, "sports": 0.03, "weather": 0.05, "other": 0.05}

# ── edge_v2 gates + cost model (Module D) ──────────────────────────────────────
NOMINAL_CLIP_USDC    = _f("NOMINAL_CLIP_USDC", 200)  # signal-only: cost-realism clip
EV2_MIN_VOLUME       = _f("EV2_MIN_VOLUME", 100000)
EV2_BOOK_DEPTH_MULT  = _f("EV2_BOOK_DEPTH_MULT", 3)
EV2_MAX_IMPACT_CENTS = _f("EV2_MAX_IMPACT_CENTS", 1.5)
EV2_LONGSHOT_LO      = _f("EV2_LONGSHOT_LO", 3.0)    # YES cents
EV2_LONGSHOT_HI      = _f("EV2_LONGSHOT_HI", 20.0)
EV2_MIN_DAYS_CARRY   = _i("EV2_MIN_DAYS_CARRY", 21)
EV2_CARRY_HURDLE     = _f("EV2_CARRY_HURDLE", 0.10)  # annualized, net
EV2_CONVERGENCE_FRACTION = _f("EV2_CONVERGENCE_FRACTION", 0.6)  # expected_hold = frac * days_to_res
EV2_BOOK_FETCH_BUDGET = _i("EV2_BOOK_FETCH_BUDGET", 25)  # CLOB book walks per cycle
EV2_COOLDOWN_HRS     = _f("EV2_COOLDOWN_HRS", 24)

# ── rate_anchor strategy (statistical base-rate book, separate from edge_v2) ──
RA_MIN_VOLUME = _f("RA_MIN_VOLUME", 25000)   # sleepy markets are the point
RA_MIN_DAYS   = _i("RA_MIN_DAYS", 7)
RB_MIN_EDGE   = _f("RB_MIN_EDGE", 10)        # backtest: trade when |anchor-implied| >= this

# ── Hardness (Module C) ────────────────────────────────────────────────────────
HARDNESS_FLOOR       = _f("HARDNESS_FLOOR", 60)      # below -> logged, never emitted

# ── Fade discrimination v2 (Module E, operator mode) ───────────────────────────
FADE_COOLDOWN_MIN       = _f("FADE_COOLDOWN_MIN", 45)
FADE_MIN_DEVIATION_PTS  = _f("FADE_MIN_DEVIATION_PTS", 15)
FADE_PENDING_EXPIRE_HRS = _f("FADE_PENDING_EXPIRE_HRS", 6)

# ── News qualification (Module E v2.1) ─────────────────────────────────────────
NEWS_FEED             = os.environ.get("NEWS_FEED", "google_rss")   # google_rss | none
NEWS_MAX_HEADLINES    = _i("NEWS_MAX_HEADLINES", 6)
NEWS_WINDOW_HRS       = _f("NEWS_WINDOW_HRS", 48)
# classifier runs only when ANTHROPIC_API_KEY is set; fails open to operator mode
NEWS_CLASSIFIER_MODEL = os.environ.get("NEWS_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
FADE_CLASSIFIER_MIN_CONF = _f("FADE_CLASSIFIER_MIN_CONF", 0.7)

# ── Strategy toggles (calibration verdicts get applied here) ───────────────────
# e.g. STRATEGIES_DISABLED="correlated_lag" in .env
STRATEGIES_DISABLED = {s.strip() for s in
                       os.environ.get("STRATEGIES_DISABLED", "").split(",") if s.strip()}

# ── Retention ──────────────────────────────────────────────────────────────────
CANDIDATES_RETAIN_DAYS = _i("CANDIDATES_RETAIN_DAYS", 180)
