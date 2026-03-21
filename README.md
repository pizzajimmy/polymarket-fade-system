# Polymarket Fade System

A systematic toolkit for trading narrative overcorrections on Polymarket.
Scans for large price drops, monitors open positions, and fires Telegram alerts
when entry setups appear or exit triggers are hit.

## Strategy summary

The system exploits a single pattern: prediction market crowds systematically
overshoot on sentiment news. When a headline causes a 20-point drop that should
only move the probability 8 points, the gap is the trade. The toolkit automates
scanning for these setups and enforces mechanical exits.

---

## Repository structure

```
polymarket-fade-system/
│
├── run.py                   ← Production entrypoint (starts both processes)
│
├── scanner.py               ← Market scanner — polls Gamma API every 30 min,
│                               detects drops, fires Telegram alerts
├── gamma.py                 ← Gamma API client with pagination + retries
├── db.py                    ← SQLite layer — price history, ambient vol, alerts
├── inspect.py               ← CLI query tool for the database
│
├── alert_bot.py             ← Position monitor — checks open positions hourly,
│                               fires Telegram on target/stop/recovery/stale
│
├── positions.json           ← Your live open positions (git-ignored)
├── positions.example.json   ← Schema reference — copy to positions.json
│
├── tools/
│   └── polymarket_tools.html ← Bankroll tracker + entry scorecard (browser app)
│
├── requirements.txt
└── railway.toml             ← Railway deployment config
```

---

## Setup

### 1. Telegram bot (10 min)

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts → save token
2. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `message.chat.id` in the JSON response — save it

Test it works:
```
https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>&text=hello
```

### 2. Local development

```bash
git clone https://github.com/YOUR_USERNAME/polymarket-fade-system
cd polymarket-fade-system

pip install -r requirements.txt

# Copy the example positions file
cp positions.example.json positions.json

# Set credentials
export TG_TOKEN="your-bot-token"
export TG_CHAT_ID="your-chat-id"

# Run the scanner once (dry run — no Telegram messages)
python scanner.py --dry-run

# Run the alert bot once against your positions
python alert_bot.py

# Check what's in the database after a scan
python inspect.py stats
python inspect.py top-vol
python inspect.py drops
```

### 3. Open the browser tools

Open `tools/polymarket_tools.html` directly in any browser.
Data persists locally in the browser's storage API.
Use this for:
- Bankroll tracker (open/close positions, track P&L)
- Entry scorecard (log every candidate evaluated, traded or not)

### 4. Deploy to Railway

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init          # creates a new project
railway up            # deploys from current directory
```

Set environment variables in Railway dashboard → Variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TG_TOKEN` | Yes | — | Telegram bot token from BotFather |
| `TG_CHAT_ID` | Yes | — | Your Telegram chat ID |
| `DROP_THRESHOLD` | No | 15 | Min points drop to trigger alert |
| `MIN_LIQUIDITY` | No | 1000 | Min USDC liquidity to scan |
| `MIN_VOLUME_24H` | No | 500 | Min USDC 24h volume to scan |
| `AMBIENT_VOL_HIGH` | No | 5 | Pts/day threshold for retail signal alert |
| `POLL_INTERVAL_SECS` | No | 1800 | Scanner interval (seconds) |
| `ALERT_COOLDOWN_HRS` | No | 12 | Hours between repeat alerts per market |
| `MAX_DAYS_OPEN` | No | 14 | Days before stale position alert |

Railway uses `railway.toml` to run `python run.py`, which starts the scanner
and alert bot as parallel threads sharing the same SQLite file.

### 5. Raspberry Pi setup

```bash
# Clone to Pi
git clone https://github.com/YOUR_USERNAME/polymarket-fade-system /home/pi/pm

# Create systemd service for scanner
sudo nano /etc/systemd/system/pm-scanner.service
```

```ini
[Unit]
Description=Polymarket Scanner
After=network.target

[Service]
WorkingDirectory=/home/pi/pm
ExecStart=/usr/bin/python3 scanner.py --loop
Restart=always
Environment=TG_TOKEN=xxx
Environment=TG_CHAT_ID=yyy

[Install]
WantedBy=multi-user.target
```

Repeat for `pm-alertbot.service` using `alert_bot.py --loop`. Then:

```bash
sudo systemctl enable pm-scanner pm-alertbot
sudo systemctl start pm-scanner pm-alertbot
journalctl -u pm-scanner -f    # follow logs
```

---

## Adding a position (workflow)

1. Scanner fires a DROP alert on Telegram with market link
2. Open `tools/polymarket_tools.html` → Entry scorecard tab
3. Fill in all fields including cross-platform prices — save the evaluation
4. If GO: run Kelly calc, open position on Polymarket
5. Add the position to `positions.json`:

```json
{
  "id":           "unique-slug",
  "question":     "Will X happen?",
  "url":          "https://polymarket.com/event/...",
  "token_id":     "clobTokenIds[0] from Gamma API",
  "entry_date":   "2025-03-21",
  "entry_price":  30.0,
  "target_exit":  43.0,
  "stop_loss":    22.0,
  "cost_usdc":    250.0,
  "status":       "OPEN",
  "alerted":      []
}
```

6. `git add positions.json && git push` → Railway redeploys in ~30 seconds
7. Alert bot picks up the new position on its next hourly check

## Closing a position

1. Sell on Polymarket when the alert fires (or manually)
2. Set `"status": "CLOSED"` in `positions.json`
3. Record exit in the bankroll tracker HTML tool
4. Push to git

---

## Inspect commands

```bash
python inspect.py stats                   # DB summary
python inspect.py markets                 # all tracked markets
python inspect.py top-vol                 # highest ambient volatility (retail signal)
python inspect.py drops                   # DROP alerts fired in last 7 days
python inspect.py history CONDITION_ID    # price table for one market
python inspect.py vol CONDITION_ID        # ambient vol breakdown + sparkline
```

---

## Pre-trade slippage check

Before entering any position, simulate your fill:

```bash
python alert_bot.py --slippage TOKEN_ID 250
```

Output:
```json
{
  "vwap_cents": 31.4,
  "slippage_pts": 1.4,
  "shares": 795.3,
  "filled_usdc": 250.0
}
```

Rules: slippage < 2pts → proceed. 2–4pts → halve size. > 4pts → pass.

---

## Finding a token_id

```bash
curl "https://gamma-api.polymarket.com/markets?slug=MARKET_SLUG" | python -m json.tool | grep clobTokenIds
```

The YES token is `clobTokenIds[0]`.

---

## Monthly calibration checklist

- [ ] Run `python inspect.py drops` — review all alerts fired
- [ ] Open bankroll tracker — check realised P&L vs expected
- [ ] Open entry scorecard — review all evaluations including NO-GO rows
- [ ] Blind-reclassify 10 past SENTIMENT/STRUCTURAL decisions
- [ ] If 10+ new closed evaluations in a category, consider updating source weights
