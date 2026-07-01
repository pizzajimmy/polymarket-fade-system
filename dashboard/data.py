"""
data.py — load + enrich the local snapshot (no Streamlit, so it's testable).
Return math mirrors pmfade.calibrate.position_return.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

HORIZONS = ["6h", "24h", "72h", "7d"]


def position_return_vec(side: pd.Series, entry: pd.Series, later) -> np.ndarray:
    later = pd.to_numeric(later, errors="coerce").to_numpy(dtype=float)
    value = np.where(side.to_numpy() == "YES", later, 100.0 - later)
    return value - entry.to_numpy(dtype=float)


def load_frames(db_path: str):
    con = sqlite3.connect(db_path)
    try:
        signals = pd.read_sql_query("SELECT * FROM signals", con)
        tracks  = pd.read_sql_query("SELECT signal_id, horizon, yes_price FROM signal_tracks", con)
        markets = pd.read_sql_query(
            "SELECT condition_id, resolved, resolved_price FROM markets", con)
        last_run = pd.read_sql_query(
            "SELECT * FROM scan_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1", con)
        counts = {
            "markets":  con.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
            "resolved": con.execute("SELECT COUNT(*) FROM markets WHERE resolved=1").fetchone()[0],
            "scans":    con.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0],
        }
    finally:
        con.close()
    return signals, tracks, markets, last_run, counts


def enrich(signals: pd.DataFrame, tracks: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    df = signals.copy()
    if df.empty:
        return df
    if not tracks.empty:
        piv = tracks.pivot_table(index="signal_id", columns="horizon",
                                 values="yes_price", aggfunc="last").reset_index()
        piv.columns.name = None
        df = df.merge(piv, on="signal_id", how="left")
    df = df.merge(markets, on="condition_id", how="left")

    res = df["resolution"] if "resolution" in df.columns else pd.Series(np.nan, index=df.index)
    resolved_price = df["resolved_price"] if "resolved_price" in df.columns \
        else pd.Series(np.nan, index=df.index)
    df["res_price"] = res.where(res.notna(), resolved_price)

    for h in HORIZONS:
        if h in df.columns:
            df[f"ret_{h}"] = position_return_vec(df["side"], df["entry_price"], df[h])
    df["ret_res"] = position_return_vec(df["side"], df["entry_price"], df["res_price"])

    avail = [h for h in reversed(HORIZONS) if h in df.columns]
    df["current"] = np.nan
    for h in avail:
        df["current"] = df["current"].where(df["current"].notna(), df[h])
    df["move"] = position_return_vec(df["side"], df["entry_price"], df["current"])

    df["band"] = (pd.to_numeric(df["score"], errors="coerce") // 10 * 10).astype("Int64")
    df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce")
    df["resolved_flag"] = df["res_price"].notna()
    return df


def humanize_ago(iso: str) -> str:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", ""))
    except Exception:
        return "?"
    s = (datetime.utcnow() - t).total_seconds()
    if s < 90:     return f"{int(s)}s ago"
    if s < 5400:   return f"{int(s/60)}m ago"
    if s < 172800: return f"{int(s/3600)}h ago"
    return f"{int(s/86400)}d ago"
