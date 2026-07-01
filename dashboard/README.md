# pmfade dashboard (local)

A local Streamlit UI for the collector running on your VPS. It reads a **local
snapshot** of the database (pulled over SSH), so the always-on server stays lean
— none of these dependencies touch it.

## Setup (once)

```bash
cd dashboard
pip install -r requirements.txt
```

You need an SSH key that already logs into the server without a password (the
one you set up during deploy). Test it: `ssh root@YOUR_IP` should just work.

## Run

```bash
streamlit run app.py
```

Your browser opens automatically. In the sidebar:

1. Enter your server as `user@host` (e.g. `root@1.2.3.4`) — it's remembered.
2. Click **Sync from server** to pull the latest snapshot.
3. Re-sync whenever you want fresh data (the collector writes every 30 min, so
   there's no point syncing more often than that).

## Pages

- **Overview** — health, totals, signals-per-day by strategy.
- **Quality → EV** — per strategy, mean return on capital by score band, net of
  cost. Fills in as signals resolve; tells you which score band is worth trading.
- **Signal explorer** — every signal, filterable by strategy/score/status, with
  a click-through to the Polymarket market.
- **Follow-ups** — open signals ranked by favourable move — what to act on.

## How the sync works

`sync.py` runs `VACUUM INTO` on the server for a clean single-file snapshot, then
`scp`s it down to `dashboard/pmfade.db`. If sqlite3 isn't on the server it falls
back to copying the db + WAL. Nothing is written back to the server — read-only.
