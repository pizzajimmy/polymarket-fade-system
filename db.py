"""
db.py — SQLite price history store
===================================
Schema:
  markets      — one row per Polymarket market (keyed by condition_id)
  price_history — one row per poll per market
  scan_alerts  — log of every alert fired (prevents duplicates within cooldown)
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager

log = logging.getLogger("scanner.db")

DB_PATH = Path("prices.db")


# ── Connection ────────────────────────────────────────────────────────────────

@contextmanager
def conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # safe for concurrent reads
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id    TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    slug            TEXT,
    url             TEXT,
    token_id_yes    TEXT,
    category        TEXT,
    end_date        TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL REFERENCES markets(condition_id),
    price           REAL NOT NULL,       -- YES price in cents (0–100)
    volume_24h      REAL,                -- USDC
    liquidity       REAL,                -- USDC order book depth
    polled_at       TEXT NOT NULL        -- ISO8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_ph_condition_time
    ON price_history (condition_id, polled_at DESC);

CREATE TABLE IF NOT EXISTS scan_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    alert_type      TEXT NOT NULL,       -- DROP | RECOVERY | AMBIENT_HIGH
    price_at_alert  REAL,
    drop_magnitude  REAL,
    ambient_vol     REAL,
    fired_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_condition
    ON scan_alerts (condition_id, alert_type, fired_at DESC);
"""


def init_db():
    """Create tables if they don't exist."""
    with conn() as c:
        c.executescript(SCHEMA)
    log.info(f"Database ready: {DB_PATH.resolve()}")


# ── Markets ───────────────────────────────────────────────────────────────────

def upsert_market(condition_id: str, question: str, slug: str = "",
                  url: str = "", token_id_yes: str = "",
                  category: str = "", end_date: str = "") -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO markets (condition_id, question, slug, url,
                                 token_id_yes, category, end_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(condition_id) DO UPDATE SET
                question     = excluded.question,
                slug         = excluded.slug,
                url          = excluded.url,
                token_id_yes = excluded.token_id_yes,
                category     = excluded.category,
                end_date     = excluded.end_date,
                updated_at   = datetime('now')
        """, (condition_id, question, slug, url, token_id_yes, category, end_date))


def get_market(condition_id: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM markets WHERE condition_id = ?", (condition_id,)
        ).fetchone()


def all_tracked_markets() -> list[sqlite3.Row]:
    with conn() as c:
        return c.execute("SELECT * FROM markets ORDER BY question").fetchall()


# ── Price history ─────────────────────────────────────────────────────────────

def record_price(condition_id: str, price: float,
                 volume_24h: float = None, liquidity: float = None) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO price_history (condition_id, price, volume_24h, liquidity, polled_at)
            VALUES (?, ?, ?, ?, datetime('now'))
        """, (condition_id, price, volume_24h, liquidity))


def latest_price(condition_id: str) -> float | None:
    """Most recent recorded price for a market."""
    with conn() as c:
        row = c.execute("""
            SELECT price FROM price_history
            WHERE condition_id = ?
            ORDER BY polled_at DESC LIMIT 1
        """, (condition_id,)).fetchone()
    return row["price"] if row else None


def price_n_hours_ago(condition_id: str, hours: int = 24) -> float | None:
    """Closest recorded price to N hours ago."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with conn() as c:
        row = c.execute("""
            SELECT price FROM price_history
            WHERE condition_id = ? AND polled_at <= ?
            ORDER BY polled_at DESC LIMIT 1
        """, (condition_id, cutoff)).fetchone()
    return row["price"] if row else None


def price_history_window(condition_id: str, days: int = 30) -> list[float]:
    """
    All prices in the last N days, oldest first.
    Used for ambient volatility calculation.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT price FROM price_history
            WHERE condition_id = ? AND polled_at >= ?
            ORDER BY polled_at ASC
        """, (condition_id, cutoff)).fetchall()
    return [r["price"] for r in rows]


def volume_spike(condition_id: str, current_volume: float,
                 lookback_days: int = 7) -> float:
    """
    Returns ratio of current 24h volume vs 7-day average.
    A ratio > 2.0 indicates a volume spike accompanying the price drop.
    """
    cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT volume_24h FROM price_history
            WHERE condition_id = ? AND polled_at >= ? AND volume_24h IS NOT NULL
            ORDER BY polled_at DESC
        """, (condition_id, cutoff)).fetchall()
    if len(rows) < 3:
        return 1.0   # not enough history — assume no spike
    avg = sum(r["volume_24h"] for r in rows) / len(rows)
    return round(current_volume / avg, 2) if avg > 0 else 1.0

# db.py — find markets stuck near 0 or 100 that haven't resolved
def stale_extreme_markets(min_days_at_extreme: int = 2,
                          extreme_threshold: float = 10.0,
                          min_liquidity: float = 500) -> list[dict]:
    """
    Find markets where price has been near 0 or 100 for N days
    but the market hasn't officially resolved yet.
    These are NO-buy (price near 100 = YES certain)
    or YES-buy (price near 0 = NO certain) opportunities.
    """
    cutoff = (datetime.utcnow() - timedelta(days=min_days_at_extreme)).isoformat()
    with conn() as c:
        rows = c.execute("""
            SELECT m.condition_id, m.question, m.url, m.end_date,
                   m.category, p.price, p.liquidity
            FROM markets m
            JOIN price_history p ON p.condition_id = m.condition_id
            WHERE p.polled_at = (
                SELECT MAX(polled_at) FROM price_history
                WHERE condition_id = m.condition_id
            )
            AND (p.price <= ? OR p.price >= ?)
            AND p.liquidity >= ?
            AND m.end_date > datetime('now')
        """, (extreme_threshold, 100 - extreme_threshold, min_liquidity)
        ).fetchall()

        results = []
        for row in rows:
            # Confirm it's been at this extreme for N days, not just arrived
            history = c.execute("""
                SELECT MIN(price) as min_p, MAX(price) as max_p
                FROM price_history
                WHERE condition_id = ? AND polled_at >= ?
            """, (row["condition_id"], cutoff)).fetchone()

            if not history:
                continue

            # All readings in the window should be extreme
            if row["price"] <= extreme_threshold:
                if history["max_p"] <= extreme_threshold + 5:
                    results.append(dict(row) | {"signal": "BUY_NO",
                                                "edge_pts": 100 - row["price"]})
            elif row["price"] >= (100 - extreme_threshold):
                if history["min_p"] >= (100 - extreme_threshold) - 5:
                    results.append(dict(row) | {"signal": "BUY_YES",
                                                "edge_pts": row["price"]})

        return sorted(results, key=lambda x: x["edge_pts"], reverse=True)
# ── Ambient volatility ────────────────────────────────────────────────────────

def ambient_volatility(condition_id: str, days: int = 30) -> float | None:
    """
    Mean absolute day-over-day price change over N days.
    High value (>5 pts/day) = retail-dominated market.

    Uses consecutive readings, not calendar-day buckets, for accuracy
    with irregular polling intervals.
    """
    prices = price_history_window(condition_id, days)
    if len(prices) < 4:
        return None
    moves = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return round(sum(moves) / len(moves), 2)


# ── Alert deduplication ───────────────────────────────────────────────────────

def alert_cooldown_passed(condition_id: str, alert_type: str,
                          cooldown_hours: int = 12) -> bool:
    """
    True if no alert of this type has been fired for this market
    within the cooldown window. Prevents spam on slowly-moving drops.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=cooldown_hours)).isoformat()
    with conn() as c:
        row = c.execute("""
            SELECT id FROM scan_alerts
            WHERE condition_id = ? AND alert_type = ? AND fired_at >= ?
            LIMIT 1
        """, (condition_id, alert_type, cutoff)).fetchone()
    return row is None


def log_alert(condition_id: str, alert_type: str,
              price: float = None, drop: float = None,
              ambient_vol: float = None) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO scan_alerts
                (condition_id, alert_type, price_at_alert, drop_magnitude, ambient_vol)
            VALUES (?, ?, ?, ?, ?)
        """, (condition_id, alert_type, price, drop, ambient_vol))


# ── Stats / diagnostics ───────────────────────────────────────────────────────

def db_stats() -> dict:
    with conn() as c:
        markets_n   = c.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        readings_n  = c.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        alerts_n    = c.execute("SELECT COUNT(*) FROM scan_alerts").fetchone()[0]
        oldest      = c.execute(
            "SELECT MIN(polled_at) FROM price_history"
        ).fetchone()[0]
        newest      = c.execute(
            "SELECT MAX(polled_at) FROM price_history"
        ).fetchone()[0]
    return {
        "markets_tracked": markets_n,
        "total_readings":  readings_n,
        "total_alerts":    alerts_n,
        "oldest_reading":  oldest,
        "newest_reading":  newest,
        "db_path":         str(DB_PATH.resolve()),
    }


def prune_old_readings(keep_days: int = 90) -> int:
    """Delete readings older than N days. Returns count deleted."""
    cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
    with conn() as c:
        n = c.execute(
            "DELETE FROM price_history WHERE polled_at < ?", (cutoff,)
        ).rowcount
    log.info(f"Pruned {n} readings older than {keep_days} days")
    return n
