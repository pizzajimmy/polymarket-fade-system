# Polymarket Fade System — strategy-portfolio harness

A signal **and calibration** harness for Polymarket. It runs a *portfolio* of
edges over the whole market universe, logs every signal it emits, then tracks
each signalled market's price at fixed horizons and at resolution — so every
strategy can be **measured independently** and you trade only what actually pays.

## Why it was rebuilt

The original system fired one signal (news-fade DROP/SPIKE) into a Telegram
firehose (~380/day) with no record of what happened next. A look at the
production log showed the problem precisely:

- The raw news-fade is **≈ breakeven** in aggregate — fades reverted about as
  often as they continued.
- But its Quality Score was **monotonically predictive** of reversion and was
  never validated, so it couldn't be trusted or used as a filter.
- The trades that actually made money were **non-news structural mispricings**
  that were never logged at all.
- There was **no feedback loop**, so none of this was knowable from the inside.

This rebuild makes every edge a first-class, independently-calibrated strategy,
and makes the feedback loop the center of the system rather than an afterthought.

## How it works

```
        Gamma API (all active markets, every ~30 min)
                     │
            ┌────────▼─────────┐
            │  engine.run_cycle │
            └────────┬─────────┘
   skip fixtures/weather/resolved · record price to rolling buffer
                     │
            build MarketView (prev-24h, ambient vol, vol spike, days-to-res)
                     │
        ┌────────────▼─────────────┐   portfolio (add an edge = add a file)
        │  news_fade  settlement_lag │
        │  longshot_bias  correlated_lag
        └────────────┬─────────────┘
            emit Signals  →  signals table (permanent, with score + features)
                     │
   follow-up: record signalled market's price at +6/24/72h/7d  →  signal_tracks
   resolution catcher: when a market closes, record who won     →  signal_tracks
                     │
            ┌────────▼─────────┐
            │   calibrate.py    │  per-strategy edge: win%, return/horizon,
            └──────────────────┘  EV net of cost, and edge-vs-score curve
```

Strategies *propose*; calibration *disposes*. A strategy's `score` is a prior —
the tracks are truth.

## The portfolio

| Strategy | Edge | Buys |
|---|---|---|
| `news_fade` | crowd overshoots on news → fade it | DROP→YES, SPIKE→NO |
| `settlement_lag` | effectively-decided market trading a few cents off the boundary | the near-certain side |
| `longshot_bias` | longshots are systematically overpriced (basket edge) | NO on cheap longshots |
| `correlated_lag` | related market hasn't repriced yet | the laggard, in the trigger's direction |

`settlement_lag` replaces the old `stale_extreme` whose `edge_pts = 100 − price`
treated distance-to-boundary as edge (it's risk); the time filter also excludes
the LeBron-2028 / Jesus-return longshots that were correctly priced, not stale.

## Data model (two-tier retention — stays small forever)

- `price_buffer` — the full universe, **rolling window only** (`RETAIN_BUFFER_DAYS`),
  pruned every cycle. Exists only to compute features.
- `signals`, `signal_tracks` — **permanent, append-only**. Every signal + its
  outcome track. This is all calibration reads, and it's tiny.
- `markets` (+ resolution), `scan_runs` (observability/heartbeat).

Storage is **SQLite** (stdlib, no server) on a durable disk — the original pain
was Railway's *ephemeral* disk and Sheets-as-a-database, not SQLite. One machine,
one user: SQLite is the right tool and the collector needs only `requests`.

## Quickstart (local)

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; defaults are fine for a dry run

python run_portfolio.py run --dry-run   # one cycle, no Telegram
python run_portfolio.py stats           # what's in the store
python -m pmfade.status                 # health + per-strategy signal dashboard
python -m pmfade.calibrate              # edge report (needs resolved signals)
```

## Calibration workflow

1. Let the collector run continuously (it needs weeks to accumulate resolutions).
2. `python -m pmfade.calibrate --cost 2` — read the per-strategy table.
3. For each strategy, find the **score band where the resolution-horizon return
   on capital is positive net of cost**. That's your trade threshold.
4. Raise `NOTIFY_MIN_SCORE` (and per-strategy gates in `pmfade/config.py`) so
   only the proven band pushes alerts. Disable strategies that don't earn.

## Deploy (VPS + systemd + healthcheck)

```bash
# on a small VPS (~$5/mo: Hetzner, DO, etc.)
sudo useradd -r -s /usr/sbin/nologin pmfade
sudo mkdir -p /opt/pmfade /var/lib/pmfade && sudo chown pmfade /var/lib/pmfade
# copy the repo to /opt/pmfade, create venv, pip install -r requirements.txt
cp .env.example /opt/pmfade/.env   # set PMFADE_DB=/var/lib/pmfade/pmfade.db + HEALTHCHECK_URL

sudo cp deploy/pmfade.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pmfade
journalctl -u pmfade -f
```

`Restart=always` + a [healthchecks.io](https://healthchecks.io) heartbeat are
the two things that fix "it went offline for ages without me noticing."

## Layout

```
run_portfolio.py        entrypoint (run / loop / stats)
pmfade/
  config.py             all tunable knobs (env-overridable)
  store.py              SQLite data layer + two-tier retention
  markets.py            universe fetch, categorization, resolved-state lookup
  engine.py             one scan cycle, end to end
  alerts.py             Telegram + healthcheck heartbeat
  status.py             terminal health + signal dashboard
  calibrate.py          per-strategy edge report
  strategies/
    base.py             Signal · MarketView · Strategy contract
    news_fade.py  settlement_lag.py  longshot_bias.py  correlated_lag.py
  portfolio.py          the active strategy set
deploy/pmfade.service   systemd unit
dashboard/              local Streamlit UI (pulls a snapshot over SSH; see its README)
```

The legacy single-strategy system (`scanner.py`, `alert_bot.py`, `run.py`,
`sheets.py`, `db.py`, `inspect_db.py`, the HTML tools) is superseded by `pmfade/`
and kept only for reference.
