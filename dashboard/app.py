"""
app.py — local Streamlit dashboard for the pmfade collector.
============================================================
Runs on YOUR machine, reads a local snapshot of the VPS database. Keeps the
always-on server lean (stdlib only) while all the heavy UI lives here.

  cd dashboard
  pip install -r requirements.txt
  streamlit run app.py

Then set your server (root@your-ip) in the sidebar and hit Sync.
"""

from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

from sync import pull, LOCAL_DB
from data import load_frames as _load_frames, enrich, humanize_ago

HERE = Path(__file__).resolve().parent
SERVER_FILE = HERE / "server.txt"

st.set_page_config(page_title="pmfade", layout="wide")


def _mtime() -> float:
    return LOCAL_DB.stat().st_mtime if LOCAL_DB.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_frames(mtime: float):
    return _load_frames(str(LOCAL_DB))


# ── sidebar: server + sync + filters ───────────────────────────────────────────

def sidebar():
    st.sidebar.title("pmfade")
    default_server = SERVER_FILE.read_text().strip() if SERVER_FILE.exists() else ""
    server = st.sidebar.text_input("Server (user@host)", value=default_server,
                                   placeholder="root@1.2.3.4")
    if st.sidebar.button("Sync from server", type="primary", use_container_width=True):
        if server:
            SERVER_FILE.write_text(server.strip())
        with st.spinner("Pulling snapshot over SSH…"):
            ok, msg = pull(server)
        (st.sidebar.success if ok else st.sidebar.error)(msg)
        if ok:
            st.cache_data.clear()

    if LOCAL_DB.exists():
        st.sidebar.caption("Local copy: "
                           + humanize_ago(datetime.utcfromtimestamp(_mtime()).isoformat()))
    else:
        st.sidebar.caption("No local copy yet — sync to begin.")

    st.sidebar.divider()
    cost = st.sidebar.slider("Assumed round-trip cost (pts)", 0.0, 6.0, 2.0, 0.5,
                             help="Spread + fees subtracted from every position before scoring edge.")
    return server, cost


# ── sections ────────────────────────────────────────────────────────────────────

def section_overview(df, last_run, counts):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Signals", f"{len(df):,}")
    c2.metric("Resolved", f"{int(df['resolved_flag'].sum()):,}" if not df.empty else "0")
    c3.metric("Markets tracked", f"{counts['markets']:,}")
    if not last_run.empty:
        r = last_run.iloc[0]
        stale = False
        try:
            stale = (datetime.utcnow() - datetime.fromisoformat(str(r["finished_at"]))).total_seconds() > 3600
        except Exception:
            pass
        c4.metric("Last scan", humanize_ago(r["finished_at"]),
                  delta="stale" if stale else "healthy",
                  delta_color="inverse" if stale else "normal")
    else:
        c4.metric("Last scan", "—")

    if df.empty:
        st.info("No signals in this snapshot yet. Let the collector run, then Sync.")
        return

    st.subheader("Signals per day")
    daily = (df.assign(day=df["ts_dt"].dt.date)
               .groupby(["day", "strategy_id"]).size().reset_index(name="n"))
    fig = px.bar(daily, x="day", y="n", color="strategy_id",
                 labels={"n": "signals", "day": "", "strategy_id": "strategy"})
    fig.update_layout(height=320, margin=dict(t=10, b=0, l=0, r=0), legend_title_text="")
    st.plotly_chart(fig, use_container_width=True)


def section_quality_ev(df, cost):
    if df.empty:
        st.info("No signals yet.")
        return
    strategies = sorted(df["strategy_id"].unique())
    strat = st.selectbox("Strategy", strategies)
    sub = df[(df["strategy_id"] == strat) & df["resolved_flag"]].copy()

    st.caption(f"{len(sub)} resolved of {int((df['strategy_id'] == strat).sum())} signals · "
               f"cost {cost:g}pt subtracted per trade")
    if sub.empty:
        st.warning("No resolved signals for this strategy yet — this is the view that fills in "
                   "over the coming weeks. It answers: does a higher score actually mean higher "
                   "return?")
        return

    sub["net_pct"] = (sub["ret_res"] - cost) / sub["entry_price"] * 100
    g = (sub.groupby("band")
            .agg(mean_net=("net_pct", "mean"), n=("net_pct", "size"),
                 win=("ret_res", lambda s: 100 * (s > 0).mean()))
            .reset_index().dropna(subset=["band"]))
    g["label"] = g["band"].astype(int).astype(str) + "–" + (g["band"].astype(int) + 9).astype(str)

    fig = go.Figure(go.Bar(
        x=g["label"], y=g["mean_net"].round(1),
        marker_color=["#2a78d6" if v >= 0 else "#d03b3b" for v in g["mean_net"]],
        customdata=np.stack([g["n"], g["win"].round(0)], axis=-1),
        hovertemplate="score %{x}<br>%{y:+.1f}% on capital<br>n=%{customdata[0]}, "
                      "win %{customdata[1]}%<extra></extra>"))
    fig.add_hline(y=0, line_width=1, line_color="#888780")
    fig.update_layout(height=340, margin=dict(t=10, b=0, l=0, r=0),
                      yaxis_title="mean return on capital (%)", xaxis_title="score band")
    st.plotly_chart(fig, use_container_width=True)

    pos = g[g["mean_net"] > 0]
    if not pos.empty:
        thr = int(pos["band"].min())
        st.success(f"Edge turns positive at score {thr}+ "
                   f"(mean {pos['mean_net'].iloc[0]:+.1f}% on capital, net of cost). "
                   f"That's the band worth trading — raise this strategy's gate to it.")
    else:
        st.info("No score band is net-positive yet on this sample.")


def section_signal_explorer(df):
    if df.empty:
        st.info("No signals yet.")
        return
    f1, f2, f3 = st.columns([2, 2, 1])
    strats = f1.multiselect("Strategy", sorted(df["strategy_id"].unique()),
                            default=sorted(df["strategy_id"].unique()))
    smin, smax = f2.slider("Score", 0, 100, (0, 100))
    status = f3.radio("Status", ["all", "open", "resolved"])

    v = df[df["strategy_id"].isin(strats)]
    v = v[pd.to_numeric(v["score"], errors="coerce").between(smin, smax)]
    if status == "open":
        v = v[~v["resolved_flag"]]
    elif status == "resolved":
        v = v[v["resolved_flag"]]

    v = v.sort_values("ts_dt", ascending=False)
    show = pd.DataFrame({
        "when": v["ts_dt"].dt.strftime("%m-%d %H:%M"),
        "strategy": v["strategy_id"],
        "side": v["side"],
        "entry¢": v["entry_price"].round(1),
        "current¢": v["current"].round(1),
        "score": pd.to_numeric(v["score"], errors="coerce").round(0),
        "return¢": v["ret_res"].round(1),
        "status": np.where(v["resolved_flag"], "resolved", "open"),
        "market": v["url"],
        "question": v["question"].astype(str).str.slice(0, 70),
    })
    st.caption(f"{len(show):,} signals")
    st.dataframe(show, use_container_width=True, hide_index=True, height=460,
                 column_config={"market": st.column_config.LinkColumn("market", display_text="open ↗")})


def section_followups(df):
    if df.empty:
        st.info("No signals yet.")
        return
    st.caption("Open signals (not yet resolved), biggest favourable move first — "
               "candidates to act on or take profit.")
    v = df[~df["resolved_flag"]].copy()
    if v.empty:
        st.info("Nothing open right now.")
        return
    now = pd.Timestamp(datetime.utcnow())
    v["age_d"] = ((now - v["ts_dt"]).dt.total_seconds() / 86400).round(1)
    v = v.sort_values("move", ascending=False)
    show = pd.DataFrame({
        "strategy": v["strategy_id"],
        "side": v["side"],
        "entry¢": v["entry_price"].round(1),
        "current¢": v["current"].round(1),
        "move¢": v["move"].round(1),
        "score": pd.to_numeric(v["score"], errors="coerce").round(0),
        "age (d)": v["age_d"],
        "market": v["url"],
        "question": v["question"].astype(str).str.slice(0, 70),
    })
    st.dataframe(show, use_container_width=True, hide_index=True, height=460,
                 column_config={"market": st.column_config.LinkColumn("market", display_text="open ↗")})


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    server, cost = sidebar()

    if not LOCAL_DB.exists():
        st.title("pmfade dashboard")
        st.info("No local snapshot yet. Enter your server (e.g. `root@1.2.3.4`) in the "
                "sidebar and click Sync from server to pull the data down.")
        return

    try:
        signals, tracks, markets, last_run, counts = load_frames(_mtime())
        df = enrich(signals, tracks, markets)
    except Exception as e:
        st.error(f"Couldn't read the snapshot: {e}")
        return

    tabs = st.tabs(["Overview", "Quality → EV", "Signal explorer", "Follow-ups"])
    with tabs[0]:
        section_overview(df, last_run, counts)
    with tabs[1]:
        section_quality_ev(df, cost)
    with tabs[2]:
        section_signal_explorer(df)
    with tabs[3]:
        section_followups(df)


main()
