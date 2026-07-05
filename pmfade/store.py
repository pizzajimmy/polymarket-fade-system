"""
store.py — durable data layer for the strategy-portfolio harness
================================================================
Backed by SQLite (stdlib only — no server, no driver, nothing to break
unattended). The pain that motivated this rewrite was *ephemeral* storage
on Railway + Google Sheets as a database, not SQLite itself. On a durable
disk (a small VPS), a WAL-mode SQLite file is bulletproof for a single-writer
collector and a single user.

Two-tier retention keeps it small forever:
  • price_buffer — the FULL market universe, but only a rolling window
    (RETAIN_BUFFER_DAYS). Exists solely to compute features (24h-ago price,
    ambient vol). Pruned every cycle.
  • signals / signal_tracks — PERMANENT, append-only. Every signal a strategy
    emits, plus that market's price at fixed follow-up horizons and at
    resolution. This is all the calibration layer ever reads, and it stays
    tiny (hundreds of signals/day, not 1.7M price rows/day).

Analytics (calibrate.py) read this file directly; DuckDB can ATTACH it for
speed but stdlib sqlite3 is enough for the aggregations.

Env:
  PMFADE_DB             path to the SQLite file (default ./pmfade.db)
  RETAIN_BUFFER_DAYS    rolling buffer window in days (default 10)
"""

from __future__ import annotations

import os
import json
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import Iterable, Iterator, Optional

log = logging.getLogger("pmfade.store")

DB_PATH = Path(os.environ.get("PMFADE_DB", "pmfade.db"))
RETAIN_BUFFER_DAYS = int(os.environ.get("RETAIN_BUFFER_DAYS", "10"))


# ── Time helpers (naive UTC, ISO8601, lexically sortable) ──────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)
    except Exception:
        return None


# ── Connection ─────────────────────────────────────────────────────────────────

@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id   TEXT PRIMARY KEY,
    question       TEXT NOT NULL,
    slug           TEXT,
    url            TEXT,
    token_id_yes   TEXT,
    category       TEXT,
    end_date       TEXT,
    resolved       INTEGER DEFAULT 0,
    resolved_price REAL,
    resolved_at    TEXT,
    first_seen     TEXT,
    updated_at     TEXT,
    description    TEXT DEFAULT '',
    event_id       TEXT DEFAULT '',
    event_slug     TEXT DEFAULT '',
    neg_risk       INTEGER DEFAULT 0,
    fees_enabled   INTEGER DEFAULT 0,
    hardness       REAL,
    hardness_flags TEXT,
    family         TEXT
);

CREATE TABLE IF NOT EXISTS price_buffer (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    yes_price    REAL NOT NULL,
    volume_24h   REAL,
    liquidity    REAL,
    polled_at    TEXT NOT NULL,
    best_bid     REAL,
    best_ask     REAL
);
CREATE INDEX IF NOT EXISTS idx_buffer_cid_time
    ON price_buffer (condition_id, polled_at DESC);

CREATE TABLE IF NOT EXISTS signals (
    signal_id    TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    strategy_id  TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    question     TEXT,
    url          TEXT,
    side         TEXT NOT NULL,          -- YES | NO  (token to buy)
    entry_price  REAL NOT NULL,          -- cents paid for `side`
    score        REAL,
    features     TEXT,                   -- JSON
    notified     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals (strategy_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_cid      ON signals (condition_id, ts DESC);

CREATE TABLE IF NOT EXISTS signal_tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    horizon    TEXT NOT NULL,            -- 6h | 24h | 72h | 7d | resolution
    yes_price  REAL NOT NULL,
    UNIQUE(signal_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_tracks_signal ON signal_tracks (signal_id);

CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT,
    finished_at     TEXT,
    markets_seen    INTEGER,
    signals_emitted INTEGER,
    elapsed_secs    REAL,
    error           TEXT
);

-- ── edge-v2: resolved-market history for the market-calibration surface ──────
-- (distinct from calibration_runs, which snapshots OUR SIGNALS' calibration;
--  hist_* + market_calibration describe THE MARKET's calibration)

CREATE TABLE IF NOT EXISTS hist_markets (
    condition_id TEXT PRIMARY KEY,
    question     TEXT,
    category     TEXT,
    end_date     TEXT,
    closed_time  TEXT,
    final_yes    REAL,               -- resolved YES price in cents (~0 or ~100)
    volume       REAL,               -- lifetime volume USDC
    fetched_at   TEXT
);

CREATE TABLE IF NOT EXISTS hist_prices (
    condition_id TEXT NOT NULL,
    horizon      TEXT NOT NULL,      -- 24h | 7d | 30d | 90d (pre-close sample)
    yes_price    REAL NOT NULL,      -- cents at that horizon
    sampled_t    INTEGER,            -- unix ts of the sampled point
    UNIQUE(condition_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_hist_prices_cid ON hist_prices (condition_id);

CREATE TABLE IF NOT EXISTS market_calibration (
    computed_at  TEXT NOT NULL,
    category     TEXT NOT NULL,
    horizon      TEXT NOT NULL,
    decile       INTEGER,            -- 0..9 by implied price; NULL = slope row
    implied_mean REAL,
    realized_freq REAL,
    n            INTEGER,
    slope        REAL,               -- per category x horizon (on slope rows)
    intercept    REAL
);
CREATE INDEX IF NOT EXISTS idx_mcalib ON market_calibration (computed_at DESC, category, horizon);

CREATE TABLE IF NOT EXISTS edge_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    question      TEXT,
    side          TEXT,
    market_price  REAL,
    fv            REAL,
    anchor        REAL,
    prior         REAL,
    blend_w       REAL,
    family        TEXT,
    tier          INTEGER,
    edge_gross    REAL,
    fee_cost      REAL,
    spread_cost   REAL,
    edge_net      REAL,
    edge_net_annualized REAL,
    hardness      REAL,
    gates_passed  TEXT,              -- JSON list of gate names
    emitted       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_ts ON edge_candidates (ts DESC);

CREATE TABLE IF NOT EXISTS structure_alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- LADDER | NEGRISK
    event_slug TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS pending_fades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    direction    TEXT NOT NULL,      -- DROP | SPIKE
    detected_at  TEXT NOT NULL,
    ref_price    REAL,               -- yes price at detection
    fv_at_detect REAL,
    status       TEXT DEFAULT 'pending',  -- pending | promoted | suppressed | expired
    reason       TEXT,
    decided_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_fades ON pending_fades (condition_id, status);

CREATE TABLE IF NOT EXISTS calibration_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,       -- when the calibration was recorded
    cost          REAL,                -- cost assumption used
    strategy_id   TEXT NOT NULL,
    band          INTEGER,             -- score band (NULL = strategy overall)
    n             INTEGER,             -- resolved signals in this cell
    win_rate      REAL,                -- % net-positive
    mean_ret_pts  REAL,                -- mean return, points, net of cost
    mean_ret_pct  REAL,                -- mean return on capital % (secondary intuition)
    ci_lo         REAL,                -- 95% CI on mean return (points) — the ✓ metric
    ci_hi         REAL
);
CREATE INDEX IF NOT EXISTS idx_calib_run ON calibration_runs (run_at DESC, strategy_id);
"""


def _ensure_columns(con: sqlite3.Connection, table: str, cols: dict[str, str]) -> None:
    """Idempotent ALTER-based migration for databases created before a column
    existed (the live VPS DB predates the edge-v2 schema)."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in cols.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            log.info("Migrated: %s += %s", table, name)


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        _ensure_columns(c, "markets", {
            "description":    "TEXT DEFAULT ''",
            "event_id":       "TEXT DEFAULT ''",
            "event_slug":     "TEXT DEFAULT ''",
            "neg_risk":       "INTEGER DEFAULT 0",
            "fees_enabled":   "INTEGER DEFAULT 0",
            "hardness":       "REAL",
            "hardness_flags": "TEXT",
            "family":         "TEXT",
        })
        _ensure_columns(c, "price_buffer", {
            "best_bid": "REAL",
            "best_ask": "REAL",
        })
    log.info("Store ready: %s", DB_PATH.resolve())


# ── Markets ────────────────────────────────────────────────────────────────────

def upsert_market(condition_id: str, question: str, slug: str = "", url: str = "",
                  token_id_yes: str = "", category: str = "", end_date: str = "",
                  description: str = "", event_id: str = "", event_slug: str = "",
                  neg_risk: int = 0, fees_enabled: int = 0) -> None:
    # NB: hardness / hardness_flags / family are computed fields (Modules C/B) —
    # deliberately NOT touched here so scan upserts don't clobber them.
    ts = now_iso()
    with connect() as c:
        c.execute("""
            INSERT INTO markets (condition_id, question, slug, url, token_id_yes,
                                 category, end_date, description, event_id,
                                 event_slug, neg_risk, fees_enabled,
                                 first_seen, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question, slug=excluded.slug, url=excluded.url,
                token_id_yes=excluded.token_id_yes, category=excluded.category,
                end_date=excluded.end_date, description=excluded.description,
                event_id=excluded.event_id, event_slug=excluded.event_slug,
                neg_risk=excluded.neg_risk, fees_enabled=excluded.fees_enabled,
                updated_at=excluded.updated_at
        """, (condition_id, question, slug, url, token_id_yes, category, end_date,
              description, event_id, event_slug, neg_risk, fees_enabled, ts, ts))


def set_market_computed(condition_id: str, hardness: Optional[float] = None,
                        hardness_flags: Optional[str] = None,
                        family: Optional[str] = None) -> None:
    """Update the computed (non-ingested) market fields; None leaves a field as-is."""
    sets, args = [], []
    if hardness is not None:
        sets.append("hardness=?");       args.append(hardness)
    if hardness_flags is not None:
        sets.append("hardness_flags=?"); args.append(hardness_flags)
    if family is not None:
        sets.append("family=?");         args.append(family)
    if not sets:
        return
    args.append(condition_id)
    with connect() as c:
        c.execute(f"UPDATE markets SET {', '.join(sets)} WHERE condition_id=?", args)


def get_market(condition_id: str) -> Optional[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM markets WHERE condition_id=?",
                         (condition_id,)).fetchone()


def mark_resolved(condition_id: str, resolved_price: Optional[float]) -> None:
    with connect() as c:
        c.execute("""UPDATE markets SET resolved=1, resolved_price=?, resolved_at=?
                     WHERE condition_id=? AND resolved=0""",
                  (resolved_price, now_iso(), condition_id))


def unresolved_market_ids() -> set[str]:
    with connect() as c:
        return {r["condition_id"] for r in
                c.execute("SELECT condition_id FROM markets WHERE resolved=0").fetchall()}


# ── Price buffer (rolling) ─────────────────────────────────────────────────────

def record_price(condition_id: str, yes_price: float,
                 volume_24h: Optional[float] = None,
                 liquidity: Optional[float] = None, at: Optional[str] = None,
                 best_bid: Optional[float] = None,
                 best_ask: Optional[float] = None) -> None:
    with connect() as c:
        c.execute("""INSERT INTO price_buffer
                     (condition_id, yes_price, volume_24h, liquidity, polled_at,
                      best_bid, best_ask)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (condition_id, yes_price, volume_24h, liquidity, at or now_iso(),
                   best_bid, best_ask))


def latest_price(condition_id: str) -> Optional[float]:
    with connect() as c:
        r = c.execute("""SELECT yes_price FROM price_buffer WHERE condition_id=?
                         ORDER BY polled_at DESC LIMIT 1""", (condition_id,)).fetchone()
    return r["yes_price"] if r else None


def price_n_hours_ago(condition_id: str, hours: float = 24) -> Optional[float]:
    """Closest recorded price at or before N hours ago."""
    cutoff = iso_hours_ago(hours)
    with connect() as c:
        r = c.execute("""SELECT yes_price FROM price_buffer
                         WHERE condition_id=? AND polled_at <= ?
                         ORDER BY polled_at DESC LIMIT 1""", (condition_id, cutoff)).fetchone()
    return r["yes_price"] if r else None


def price_window(condition_id: str, days: float = 10) -> list[float]:
    """All prices in the last N days, oldest first (for ambient vol)."""
    cutoff = iso_days_ago(days)
    with connect() as c:
        rows = c.execute("""SELECT yes_price FROM price_buffer
                            WHERE condition_id=? AND polled_at >= ?
                            ORDER BY polled_at ASC""", (condition_id, cutoff)).fetchall()
    return [r["yes_price"] for r in rows]


def volume_window(condition_id: str, days: float = 7) -> list[float]:
    cutoff = iso_days_ago(days)
    with connect() as c:
        rows = c.execute("""SELECT volume_24h FROM price_buffer
                            WHERE condition_id=? AND polled_at >= ? AND volume_24h IS NOT NULL
                            ORDER BY polled_at DESC""", (condition_id, cutoff)).fetchall()
    return [r["volume_24h"] for r in rows]


def prune_buffer(days: int = RETAIN_BUFFER_DAYS) -> int:
    cutoff = iso_days_ago(days)
    with connect() as c:
        n = c.execute("DELETE FROM price_buffer WHERE polled_at < ?", (cutoff,)).rowcount
    if n:
        log.info("Pruned %d buffer rows older than %d days", n, days)
    return n


# ── Signals (permanent) ────────────────────────────────────────────────────────

def insert_signal(signal_id: str, strategy_id: str, condition_id: str, question: str,
                  url: str, side: str, entry_price: float, score: float,
                  features: dict, at: Optional[str] = None) -> None:
    with connect() as c:
        c.execute("""INSERT INTO signals
                     (signal_id, ts, strategy_id, condition_id, question, url,
                      side, entry_price, score, features)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (signal_id, at or now_iso(), strategy_id, condition_id, question,
                   url, side, entry_price, score, json.dumps(features, default=str)))


def recent_signal_exists(strategy_id: str, condition_id: str, within_hours: float) -> bool:
    """Cooldown: has this strategy already fired on this market recently?"""
    cutoff = iso_hours_ago(within_hours)
    with connect() as c:
        r = c.execute("""SELECT 1 FROM signals
                         WHERE strategy_id=? AND condition_id=? AND ts >= ?
                         LIMIT 1""", (strategy_id, condition_id, cutoff)).fetchone()
    return r is not None


def signals_awaiting_tracking(window_days: float = 7) -> list[sqlite3.Row]:
    """Signals young enough to still have follow-up horizons pending."""
    cutoff = iso_days_ago(window_days)
    with connect() as c:
        return c.execute("""SELECT signal_id, ts, condition_id FROM signals
                            WHERE ts >= ?""", (cutoff,)).fetchall()


def recorded_horizons(signal_id: str) -> set[str]:
    with connect() as c:
        return {r["horizon"] for r in
                c.execute("SELECT horizon FROM signal_tracks WHERE signal_id=?",
                          (signal_id,)).fetchall()}


def record_track(signal_id: str, horizon: str, yes_price: float,
                 at: Optional[str] = None) -> None:
    with connect() as c:
        c.execute("""INSERT OR IGNORE INTO signal_tracks
                     (signal_id, ts, horizon, yes_price) VALUES (?, ?, ?, ?)""",
                  (signal_id, at or now_iso(), horizon, yes_price))


def signal_ids_for_condition(condition_id: str) -> list[str]:
    with connect() as c:
        return [r["signal_id"] for r in
                c.execute("SELECT signal_id FROM signals WHERE condition_id=?",
                          (condition_id,)).fetchall()]


def open_signal_condition_ids(window_days: float = 7) -> set[str]:
    cutoff = iso_days_ago(window_days)
    with connect() as c:
        return {r["condition_id"] for r in
                c.execute("SELECT DISTINCT condition_id FROM signals WHERE ts >= ?",
                          (cutoff,)).fetchall()}


def unnotified_signals() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM signals WHERE notified=0 ORDER BY ts").fetchall()


def mark_notified(signal_id: str) -> None:
    with connect() as c:
        c.execute("UPDATE signals SET notified=1 WHERE signal_id=?", (signal_id,))


# ── Scan runs (observability) ──────────────────────────────────────────────────

def start_run() -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO scan_runs (started_at) VALUES (?)", (now_iso(),))
        return cur.lastrowid


def finish_run(run_id: int, markets_seen: int, signals_emitted: int,
               elapsed_secs: float, error: Optional[str] = None) -> None:
    with connect() as c:
        c.execute("""UPDATE scan_runs SET finished_at=?, markets_seen=?,
                     signals_emitted=?, elapsed_secs=?, error=? WHERE id=?""",
                  (now_iso(), markets_seen, signals_emitted, elapsed_secs, error, run_id))


# ── Resolved-market history (market-calibration substrate) ────────────────────

def hist_has(condition_id: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM hist_markets WHERE condition_id=?",
                         (condition_id,)).fetchone() is not None


def hist_upsert_market(condition_id: str, question: str, category: str,
                       end_date: str, closed_time: str, final_yes: float,
                       volume: float) -> None:
    with connect() as c:
        c.execute("""INSERT INTO hist_markets
                     (condition_id, question, category, end_date, closed_time,
                      final_yes, volume, fetched_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(condition_id) DO UPDATE SET
                       final_yes=excluded.final_yes, closed_time=excluded.closed_time,
                       volume=excluded.volume, fetched_at=excluded.fetched_at""",
                  (condition_id, question, category, end_date, closed_time,
                   final_yes, volume, now_iso()))


def hist_record_price(condition_id: str, horizon: str, yes_price: float,
                      sampled_t: Optional[int] = None) -> None:
    with connect() as c:
        c.execute("""INSERT OR IGNORE INTO hist_prices
                     (condition_id, horizon, yes_price, sampled_t)
                     VALUES (?, ?, ?, ?)""",
                  (condition_id, horizon, yes_price, sampled_t))


def hist_stats() -> dict:
    with connect() as c:
        markets_n = c.execute("SELECT COUNT(*) FROM hist_markets").fetchone()[0]
        samples_n = c.execute("SELECT COUNT(*) FROM hist_prices").fetchone()[0]
        cells = c.execute("""
            SELECT m.category, p.horizon, COUNT(*) n
            FROM hist_prices p JOIN hist_markets m ON m.condition_id=p.condition_id
            GROUP BY m.category, p.horizon ORDER BY m.category, p.horizon
        """).fetchall()
    return {"markets": markets_n, "samples": samples_n,
            "cells": [(r["category"], r["horizon"], r["n"]) for r in cells]}


def record_calibration(run_at: str, rows: list[dict]) -> None:
    """Persist one calibration snapshot: one row per (strategy, band) cell."""
    with connect() as c:
        c.executemany("""
            INSERT INTO calibration_runs
                (run_at, cost, strategy_id, band, n, win_rate,
                 mean_ret_pts, mean_ret_pct, ci_lo, ci_hi)
            VALUES (:run_at, :cost, :strategy_id, :band, :n, :win_rate,
                    :mean_ret_pts, :mean_ret_pct, :ci_lo, :ci_hi)
        """, [{"run_at": run_at, **r} for r in rows])


def calibration_history(strategy_id: str | None = None) -> list:
    with connect() as c:
        try:
            if strategy_id:
                return c.execute(
                    "SELECT * FROM calibration_runs WHERE strategy_id=? ORDER BY run_at",
                    (strategy_id,)).fetchall()
            return c.execute("SELECT * FROM calibration_runs ORDER BY run_at").fetchall()
        except sqlite3.OperationalError:
            return []


def db_stats() -> dict:
    with connect() as c:
        def one(sql):
            return c.execute(sql).fetchone()[0]
        return {
            "db_path":          str(DB_PATH.resolve()),
            "markets":          one("SELECT COUNT(*) FROM markets"),
            "resolved":         one("SELECT COUNT(*) FROM markets WHERE resolved=1"),
            "buffer_rows":      one("SELECT COUNT(*) FROM price_buffer"),
            "signals":          one("SELECT COUNT(*) FROM signals"),
            "signal_tracks":    one("SELECT COUNT(*) FROM signal_tracks"),
            "oldest_buffer":    one("SELECT MIN(polled_at) FROM price_buffer"),
            "newest_buffer":    one("SELECT MAX(polled_at) FROM price_buffer"),
            "scan_runs":        one("SELECT COUNT(*) FROM scan_runs"),
        }
