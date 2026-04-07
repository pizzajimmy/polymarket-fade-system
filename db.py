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
    price_prev_scan REAL,                -- price from the scan just before this alert
    drop_magnitude  REAL,
    ambient_vol     REAL,
    quality_score   INTEGER DEFAULT 0,
    quality_flags   TEXT DEFAULT '',
    followup_price  REAL,                -- price recorded during 10-min followup
    followup_at     TEXT,                -- when the followup was recorded
    fired_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_condition
    ON scan_alerts (condition_id, alert_type, fired_at DESC);

CREATE TABLE IF NOT EXISTS alert_checkpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     INTEGER NOT NULL REFERENCES scan_alerts(id),
    hours_after  REAL NOT NULL,          -- nominal interval (1, 6, 24, 72, …)
    price        REAL NOT NULL,
    recorded_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cp_alert
    ON alert_checkpoints (alert_id, hours_after);
"""

CHECKPOINT_HOURS = [1, 6, 24, 72, 168, 336, 720]  # 1h, 6h, 24h, 3d, 7d, 14d, 30d


def init_db():
    """Create tables if they don't exist."""
    with conn() as c:
        c.executescript(SCHEMA)
        migrations = [
            "ALTER TABLE scan_alerts ADD COLUMN quality_score INTEGER DEFAULT 0",
            "ALTER TABLE scan_alerts ADD COLUMN quality_flags TEXT DEFAULT ''",
            "ALTER TABLE scan_alerts ADD COLUMN price_prev_scan REAL",
            "ALTER TABLE scan_alerts ADD COLUMN followup_price REAL",
            "ALTER TABLE scan_alerts ADD COLUMN followup_at TEXT",
            # Feature 2: event_slug on markets
            "ALTER TABLE markets ADD COLUMN event_slug TEXT DEFAULT ''",
            # Feature 5: order book snapshot at alert time
            "ALTER TABLE scan_alerts ADD COLUMN best_bid REAL",
            "ALTER TABLE scan_alerts ADD COLUMN best_ask REAL",
            "ALTER TABLE scan_alerts ADD COLUMN spread_pts REAL",
            "ALTER TABLE scan_alerts ADD COLUMN bid_depth_5 REAL",
            "ALTER TABLE scan_alerts ADD COLUMN ask_depth_5 REAL",
            "ALTER TABLE scan_alerts ADD COLUMN bid_ask_imbalance REAL",
        ]
        for sql in migrations:
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists
    log.info(f"Database ready: {DB_PATH.resolve()}")


# ── Markets ───────────────────────────────────────────────────────────────────

def upsert_market(condition_id: str, question: str, slug: str = "",
                  url: str = "", token_id_yes: str = "",
                  category: str = "", end_date: str = "",
                  event_slug: str = "") -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO markets (condition_id, question, slug, url,
                                 token_id_yes, category, end_date,
                                 event_slug, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(condition_id) DO UPDATE SET
                question     = excluded.question,
                slug         = excluded.slug,
                url          = excluded.url,
                token_id_yes = excluded.token_id_yes,
                category     = excluded.category,
                end_date     = excluded.end_date,
                event_slug   = excluded.event_slug,
                updated_at   = datetime('now')
        """, (condition_id, question, slug, url, token_id_yes,
              category, end_date, event_slug))


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


def price_before_current(condition_id: str) -> float | None:
    """
    Returns the second-most-recent recorded price — i.e. the price from
    the scan cycle immediately before this one. Acts as the 'pre-alert'
    baseline (typically 10-30 min before, depending on poll interval).
    """
    with conn() as c:
        rows = c.execute("""
            SELECT price FROM price_history
            WHERE condition_id = ?
            ORDER BY polled_at DESC LIMIT 2
        """, (condition_id,)).fetchall()
    return rows[1]["price"] if len(rows) >= 2 else None


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
            AND m.end_date > datetime('now', '+14 days')
        """, (extreme_threshold, 100 - extreme_threshold, min_liquidity)
        ).fetchall()

        results = []
        for row in rows:
            history = c.execute("""
                SELECT MIN(price) as min_p, MAX(price) as max_p,
                       MIN(polled_at) as oldest, COUNT(*) as reading_count
                FROM price_history
                WHERE condition_id = ? AND polled_at >= ?
            """, (row["condition_id"], cutoff)).fetchone()

            if not history or not history["min_p"]:
                continue

            # Must have readings spanning at least N days
            # (not just 2 readings from yesterday)
            if history["oldest"] is None:
                continue
            oldest_dt = datetime.fromisoformat(history["oldest"])
            span_days = (datetime.utcnow() - oldest_dt).days
            if span_days < min_days_at_extreme:
                continue

            # Must have at least 4 readings (avoids single-reading flukes)
            if history["reading_count"] < 4:
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
        FIXTURE_KEYWORDS = [
            " vs ", " vs. ", "o/u ", "over/under", "spread:",
            "both teams to score", "first half", "map 1", "map 2",
            "odd/even", "moneyline", "correct score",
        ]

        results = [
            r for r in results
            if not any(kw in r["question"].lower() for kw in FIXTURE_KEYWORDS)
        ]
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
              ambient_vol: float = None,
              quality_score: int = 0, quality_flags: list = None,
              price_prev_scan: float = None,
              book_snapshot: dict = None) -> None:
    flags_str = ", ".join(quality_flags) if quality_flags else ""
    ob = book_snapshot or {}
    with conn() as c:
        c.execute("""
            INSERT INTO scan_alerts
                (condition_id, alert_type, price_at_alert, price_prev_scan,
                 drop_magnitude, ambient_vol, quality_score, quality_flags,
                 best_bid, best_ask, spread_pts, bid_depth_5,
                 ask_depth_5, bid_ask_imbalance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (condition_id, alert_type, price, price_prev_scan,
              drop, ambient_vol, quality_score, flags_str,
              ob.get("best_bid_cents"), ob.get("best_ask_cents"),
              ob.get("spread_pts"), ob.get("bid_depth_5lvl"),
              ob.get("ask_depth_5lvl"), ob.get("bid_ask_imbalance")))


def get_pending_10min_followups(mins_min: float = 8, mins_max: float = 90) -> list[dict]:
    """
    Return alerts fired between mins_min and mins_max ago that have not yet
    had a followup price recorded. The wide window (up to 90 min) ensures
    we catch alerts even if the followup cron missed a cycle.
    """
    with conn() as c:
        rows = c.execute("""
            SELECT a.id, a.condition_id, a.alert_type, a.price_at_alert,
                   a.price_prev_scan, a.fired_at, m.question, m.url, m.category
            FROM scan_alerts a
            JOIN markets m ON m.condition_id = a.condition_id
            WHERE a.fired_at >= datetime('now', ? || ' minutes')
              AND a.fired_at <= datetime('now', ? || ' minutes')
              AND a.followup_price IS NULL
            ORDER BY a.fired_at DESC
        """, (f'-{mins_max}', f'-{mins_min}')).fetchall()
    return [dict(r) for r in rows]


def mark_10min_followup(alert_id: int, price: float) -> None:
    """Record the followup price against an alert so it isn't checked again."""
    with conn() as c:
        c.execute("""
            UPDATE scan_alerts
            SET followup_price = ?, followup_at = datetime('now')
            WHERE id = ?
        """, (price, alert_id))


def get_recent_alerts(hours_min: float = 1.5, hours_max: float = 3.0) -> list[dict]:
    """Return alerts fired between hours_min and hours_max ago."""
    with conn() as c:
        rows = c.execute("""
            SELECT a.condition_id, a.alert_type, a.price_at_alert,
                   a.fired_at, a.ambient_vol, m.question, m.url, m.category
            FROM scan_alerts a
            JOIN markets m ON m.condition_id = a.condition_id
            WHERE a.fired_at >= datetime('now', ? || ' hours')
              AND a.fired_at <= datetime('now', ? || ' hours')
            ORDER BY a.fired_at DESC
        """, (f'-{hours_max}', f'-{hours_min}')).fetchall()
    return [dict(r) for r in rows]


# ── Price checkpoints ────────────────────────────────────────────────────────

def get_pending_checkpoints() -> list[dict]:
    """
    Find alert×interval pairs that are due but not yet recorded.
    Returns dicts with alert_id, condition_id, hours_after, fired_at, question, url.
    """
    results = []
    with conn() as c:
        # Only look at alerts from the last 31 days (beyond that, all checkpoints are done)
        cutoff = (datetime.utcnow() - timedelta(days=31)).isoformat()
        alerts = c.execute("""
            SELECT a.id, a.condition_id, a.fired_at, m.question, m.url
            FROM scan_alerts a
            JOIN markets m ON m.condition_id = a.condition_id
            WHERE a.fired_at >= ?
            ORDER BY a.fired_at DESC
        """, (cutoff,)).fetchall()

        for alert in alerts:
            fired = datetime.fromisoformat(alert["fired_at"])
            elapsed_hours = (datetime.utcnow() - fired).total_seconds() / 3600

            # Find which checkpoints have already been recorded
            existing = c.execute("""
                SELECT hours_after FROM alert_checkpoints
                WHERE alert_id = ?
            """, (alert["id"],)).fetchall()
            recorded = {row["hours_after"] for row in existing}

            for interval in CHECKPOINT_HOURS:
                if interval in recorded:
                    continue
                if elapsed_hours >= interval:
                    results.append({
                        "alert_id":     alert["id"],
                        "condition_id": alert["condition_id"],
                        "hours_after":  interval,
                        "fired_at":     alert["fired_at"],
                        "question":     alert["question"],
                        "url":          alert["url"],
                    })
    return results


def record_checkpoint(alert_id: int, hours_after: float, price: float) -> None:
    with conn() as c:
        c.execute("""
            INSERT INTO alert_checkpoints (alert_id, hours_after, price)
            VALUES (?, ?, ?)
        """, (alert_id, hours_after, price))


def get_checkpoints_for_alert(alert_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT hours_after, price, recorded_at
            FROM alert_checkpoints
            WHERE alert_id = ?
            ORDER BY hours_after ASC
        """, (alert_id,)).fetchall()
    return [dict(r) for r in rows]


# ── Comparable alert lookup ──────────────────────────────────────────────────

def find_comparable_alerts(category: str, alert_type: str,
                           drop_magnitude: float, limit: int = 5) -> list[dict]:
    """
    Find past alerts with similar characteristics.
    Returns dicts with question, alert price, drop, quality score,
    fired_at, and checkpoint recovery curve.
    """
    mag_lo = abs(drop_magnitude) * 0.6
    mag_hi = abs(drop_magnitude) * 1.5
    with conn() as c:
        rows = c.execute("""
            SELECT a.id, a.condition_id, a.alert_type, a.price_at_alert,
                   a.drop_magnitude, a.quality_score, a.fired_at,
                   a.followup_price, m.question, m.url, m.category
            FROM scan_alerts a
            JOIN markets m ON m.condition_id = a.condition_id
            WHERE m.category = ?
              AND a.alert_type = ?
              AND ABS(a.drop_magnitude) BETWEEN ? AND ?
            ORDER BY a.fired_at DESC
            LIMIT ?
        """, (category, alert_type, mag_lo, mag_hi, limit)).fetchall()

    results = []
    for row in rows:
        d = dict(row)
        d["checkpoints"] = get_checkpoints_for_alert(row["id"])
        results.append(d)
    return results


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
