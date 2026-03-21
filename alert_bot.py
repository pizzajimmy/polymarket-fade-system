"""
Polymarket Price Alert Bot
==========================
Monitors open positions via the Polymarket CLOB API.
Fires Telegram alerts on: target hit, stop loss, 60% recovery, stale position.

Run on a cron: 0 * * * *  (every hour)
Or continuous: python alert_bot.py --loop
"""

import os
import json
import time
import logging
import argparse
import requests
from datetime import date, datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")
DATA_FILE = Path(os.environ.get("POSITIONS_FILE", "positions.json"))

POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL_SECS", "3600"))   # 1 hour
MAX_DAYS_OPEN  = int(os.environ.get("MAX_DAYS_OPEN", "14"))
RECOVERY_PCT   = float(os.environ.get("RECOVERY_PCT", "0.60"))        # 60%
CLOB_BASE      = "https://clob.polymarket.com"
GAMMA_BASE     = "https://gamma-api.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("alert_bot")


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message via the Telegram Bot API. Returns True on success."""
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured — set TG_TOKEN and TG_CHAT_ID.")
        print(f"\n[ALERT — would send to Telegram]\n{message}\n")
        return False
    url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT, "text": message, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=data, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def fmt_alert(emoji: str, title: str, question: str, detail: str) -> str:
    short_q = question[:80] + ("…" if len(question) > 80 else "")
    return (
        f"{emoji} <b>{title}</b>\n"
        f"<i>{short_q}</i>\n"
        f"{detail}"
    )


# ── CLOB API ──────────────────────────────────────────────────────────────────

def get_mid_price(token_id: str) -> float | None:
    """
    Return mid-price in cents for a YES token.
    Falls back to best ask if no bids exist.
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        r.raise_for_status()
        book = r.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0][0]) * 100 if bids else None
        best_ask = float(asks[0][0]) * 100 if asks else None

        if best_bid and best_ask:
            return round((best_bid + best_ask) / 2, 2)
        return best_ask or best_bid
    except Exception as e:
        log.warning(f"CLOB fetch failed for {token_id[:12]}…: {e}")
        return None


def simulate_slippage(token_id: str, usdc_amount: float) -> dict | None:
    """
    Estimate fill VWAP and slippage for a given USDC buy amount.
    Used before entering a trade — not needed for monitoring, but
    exported here for use in manual pre-trade checks.
    """
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        r.raise_for_status()
        asks = r.json().get("asks", [])

        filled, shares = 0.0, 0.0
        for price_s, size_s in asks:
            price, size = float(price_s), float(size_s)
            take   = min(size, usdc_amount - filled)
            shares += take / price
            filled += take
            if filled >= usdc_amount:
                break

        if filled < usdc_amount * 0.9:
            return {"error": "insufficient_liquidity", "fillable_usdc": round(filled, 2)}

        vwap         = filled / shares if shares else None
        best_ask     = float(asks[0][0]) if asks else None
        slippage_pts = round((vwap - best_ask) * 100, 2) if vwap and best_ask else None

        return {
            "vwap_cents":    round(vwap * 100, 2) if vwap else None,
            "slippage_pts":  slippage_pts,
            "shares":        round(shares, 2),
            "filled_usdc":   round(filled, 2),
        }
    except Exception as e:
        log.error(f"Slippage sim failed: {e}")
        return None


# ── Position store ────────────────────────────────────────────────────────────
#
# positions.json schema:
# [
#   {
#     "id":           "unique string — use market slug or timestamp",
#     "question":     "Will policy X pass?",
#     "url":          "https://polymarket.com/event/...",
#     "token_id":     "71321045abc...",    ← YES token, clobTokenIds[0]
#     "entry_date":   "2025-03-18",
#     "entry_price":  30.0,               ← cents
#     "target_exit":  43.0,               ← cents
#     "stop_loss":    22.0,               ← cents
#     "cost_usdc":    250.0,
#     "status":       "OPEN",             ← OPEN | CLOSED
#     "alerted":      []                  ← list of alert types already sent
#   }
# ]

def load_positions() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text())
    except Exception as e:
        log.error(f"Failed to read {DATA_FILE}: {e}")
        return []


def save_positions(positions: list[dict]) -> None:
    DATA_FILE.write_text(json.dumps(positions, indent=2))


# ── Alert logic ───────────────────────────────────────────────────────────────

def days_held(entry_date_str: str) -> int:
    try:
        d = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        return (date.today() - d).days
    except Exception:
        return 0


def check_position(pos: dict) -> list[str]:
    """
    Evaluate a single position. Returns list of alert_type strings fired.
    Mutates pos["alerted"] to prevent duplicate alerts.
    """
    if pos.get("status") != "OPEN":
        return []

    token_id   = pos.get("token_id", "")
    entry      = float(pos["entry_price"])
    target     = float(pos["target_exit"])
    stop       = float(pos["stop_loss"])
    question   = pos.get("question", "Unknown market")
    url        = pos.get("url", "")
    alerted    = pos.setdefault("alerted", [])
    fired      = []

    current = get_mid_price(token_id) if token_id else None

    if current is None:
        log.warning(f"No price for: {question[:50]}")
        return []

    pos["last_price"]   = current
    pos["last_checked"] = datetime.utcnow().isoformat()

    gap      = target - entry          # total expected recovery
    recovered = (current - entry) / gap if gap > 0 else 0
    d_held   = days_held(pos.get("entry_date", ""))
    url_tag  = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""

    log.info(
        f"{question[:45]:<45}  "
        f"entry={entry:.1f}¢  current={current:.1f}¢  "
        f"target={target:.1f}¢  stop={stop:.1f}¢  days={d_held}"
    )

    # ── Target hit ────────────────────────────────────────────────────────────
    if current >= target and "TARGET" not in alerted:
        msg = fmt_alert(
            "🎯", "Target hit — sell now",
            question,
            f"Current: <b>{current:.1f}¢</b>  ·  Target: {target:.1f}¢\n"
            f"Held {d_held} days  ·  Entry: {entry:.1f}¢{url_tag}",
        )
        if send_telegram(msg):
            alerted.append("TARGET")
            fired.append("TARGET")

    # ── Stop loss ─────────────────────────────────────────────────────────────
    elif current <= stop and "STOP" not in alerted:
        msg = fmt_alert(
            "🛑", "Stop loss hit — exit now",
            question,
            f"Current: <b>{current:.1f}¢</b>  ·  Stop: {stop:.1f}¢\n"
            f"Loss: {current - entry:+.1f} pts  ·  Entry: {entry:.1f}¢{url_tag}",
        )
        if send_telegram(msg):
            alerted.append("STOP")
            fired.append("STOP")

    # ── 60 % recovery ─────────────────────────────────────────────────────────
    if recovered >= RECOVERY_PCT and "PARTIAL" not in alerted:
        pct = int(recovered * 100)
        msg = fmt_alert(
            "📈", f"{pct}% recovered — consider partial exit",
            question,
            f"Current: <b>{current:.1f}¢</b>  ·  Target: {target:.1f}¢\n"
            f"Entry: {entry:.1f}¢  ·  Held {d_held} days{url_tag}",
        )
        if send_telegram(msg):
            alerted.append("PARTIAL")
            fired.append("PARTIAL")

    # ── Stale position ────────────────────────────────────────────────────────
    if d_held >= MAX_DAYS_OPEN and "STALE" not in alerted:
        msg = fmt_alert(
            "⏰", f"Stale position — {d_held} days, no recovery",
            question,
            f"Current: <b>{current:.1f}¢</b>  ·  Entry: {entry:.1f}¢\n"
            f"Thesis may be wrong. Reassess or exit.{url_tag}",
        )
        if send_telegram(msg):
            alerted.append("STALE")
            fired.append("STALE")

    return fired


def run_once() -> None:
    positions = load_positions()
    open_pos  = [p for p in positions if p.get("status") == "OPEN"]

    if not open_pos:
        log.info("No open positions to monitor.")
        return

    log.info(f"Checking {len(open_pos)} open position(s)…")
    any_fired = False

    for pos in positions:
        if pos.get("status") != "OPEN":
            continue
        fired = check_position(pos)
        if fired:
            any_fired = True
            log.info(f"  Fired: {fired}  →  {pos['question'][:50]}")

    save_positions(positions)
    log.info("Done." + (" Alerts sent." if any_fired else " No alerts triggered."))


def run_loop() -> None:
    log.info(f"Starting continuous monitor (interval: {POLL_INTERVAL}s)…")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Unexpected error in run_once: {e}")
        log.info(f"Sleeping {POLL_INTERVAL}s until next check…")
        time.sleep(POLL_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket price alert bot")
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously instead of once",
    )
    parser.add_argument(
        "--slippage", nargs=2, metavar=("TOKEN_ID", "USDC"),
        help="Simulate slippage for a token before entering a trade",
    )
    args = parser.parse_args()

    if args.slippage:
        token_id, usdc = args.slippage
        result = simulate_slippage(token_id, float(usdc))
        print(json.dumps(result, indent=2))
    elif args.loop:
        run_loop()
    else:
        run_once()
