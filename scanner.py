"""
scanner.py — market scanner + drop detector
=============================================
Orchestrates the full scan cycle:
  1. Fetch all active markets from Gamma API
  2. Upsert market metadata into SQLite
  3. Record current price in price_history
  4. Calculate drop vs 24h ago and ambient volatility
  5. Fire Telegram alerts for qualifying setups
  6. Check for correlated markets that haven't repriced yet (lag sniper)

Run on a cron: */30 * * * *  (every 30 minutes)
Or continuous: python scanner.py --loop
"""

import os
import re
import time
import logging
import argparse
import requests
from datetime import datetime
from sheets import log_drop_alert, log_spike_alert

import db
from gamma import fetch_all_active_markets

# ── Config ────────────────────────────────────────────────────────────────────

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")
SPIKE_THRESHOLD    = float(os.environ.get("SPIKE_THRESHOLD", "15"))
DROP_THRESHOLD     = float(os.environ.get("DROP_THRESHOLD", "15"))
MIN_LIQUIDITY      = float(os.environ.get("MIN_LIQUIDITY", "1000"))
MIN_VOLUME_24H     = float(os.environ.get("MIN_VOLUME_24H", "500"))
MIN_DAYS_TO_RES    = int(os.environ.get("MIN_DAYS_TO_RESOLUTION", "7"))
AMBIENT_VOL_HIGH   = float(os.environ.get("AMBIENT_VOL_HIGH", "5"))
VOLUME_SPIKE_RATIO = float(os.environ.get("VOLUME_SPIKE_RATIO", "1.8"))
ALERT_COOLDOWN_HRS = int(os.environ.get("ALERT_COOLDOWN_HRS", "12"))
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL_SECS", "1800"))
PRUNE_DAYS         = int(os.environ.get("PRUNE_DAYS", "90"))

# Minimum keyword overlap to consider markets correlated
CORR_MIN_OVERLAP   = int(os.environ.get("CORR_MIN_OVERLAP", "2"))
# A lagging market must NOT have moved more than this in the expected direction
CORR_LAG_THRESHOLD = float(os.environ.get("CORR_LAG_THRESHOLD", "5.0"))
# Max correlated markets to show per alert
CORR_MAX_RESULTS   = int(os.environ.get("CORR_MAX_RESULTS", "5"))

FIXTURE_KEYWORDS = [
    " vs ", " vs. ", "o/u ", "over/under", "spread:",
    "both teams to score", "first half", "map 1", "map 2",
    "odd/even", "moneyline", "correct score", "next goal",
]

STOPWORDS = {
    "will", "the", "a", "an", "in", "on", "at", "by", "for", "of", "to",
    "be", "is", "was", "are", "were", "have", "has", "had", "do", "does",
    "did", "and", "or", "but", "not", "this", "that", "from", "with",
    "before", "after", "during", "between", "about", "than", "more", "less",
    "happen", "occur", "pass", "win", "lose", "become", "get", "any", "all",
    "next", "last", "first", "new", "old", "big", "small", "high", "low",
    "its", "their", "there", "which", "who", "what", "when", "where", "how",
    "by", "as", "if", "up", "out", "no", "so", "we", "he", "she", "they",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scanner")


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram not configured — printing alert locally.")
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


def format_drop_alert(market: dict, drop: float, ambient_vol: float | None,
                      vol_spike: float, prev_price: float,
                      quality_score: int = 0, quality_flags: list = None) -> str:
    q = market["question"][:90] + ("…" if len(market["question"]) > 90 else "")
    cat = market.get("category", "unknown")
    vol_str = f"{vol_spike:.1f}× avg volume" if vol_spike > 1 else ""
    amb_str = ""
    if ambient_vol:
        amb_flag = " ⚠ HIGH RETAIL VOL" if ambient_vol >= AMBIENT_VOL_HIGH else ""
        amb_str = f"\nAmbient vol: {ambient_vol:.1f} pts/day{amb_flag}"
    url = market.get("url", "")
    url_tag = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""
    score_str = f"\n\n<b>Quality score: {quality_score}/100</b>"
    if quality_flags:
        score_str += f"\n{' · '.join(quality_flags)}"
    return (
        f"📉 <b>Price drop alert — {cat}</b>\n"
        f"<i>{q}</i>\n\n"
        f"Drop:    <b>{prev_price:.1f}¢ → {market['yes_price']:.1f}¢  (−{drop:.1f} pts)</b>\n"
        f"Volume:  ${market['volume_24h']:,.0f} 24h  {vol_str}\n"
        f"Liquidity: ${market['liquidity']:,.0f}"
        f"{amb_str}"
        f"{score_str}"
        f"{url_tag}"
    )


def format_spike_alert(market: dict, rise: float, ambient_vol: float | None,
                       vol_spike: float, prev_price: float,
                       quality_score: int = 0, quality_flags: list = None) -> str:
    q = market["question"][:90] + ("…" if len(market["question"]) > 90 else "")
    cat = market.get("category", "unknown")
    amb_str = ""
    if ambient_vol:
        amb_flag = " ⚠ HIGH RETAIL VOL" if ambient_vol >= AMBIENT_VOL_HIGH else ""
        amb_str = f"\nAmbient vol: {ambient_vol:.1f} pts/day{amb_flag}"
    url = market.get("url", "")
    url_tag = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""
    score_str = f"\n\n<b>Quality score: {quality_score}/100</b>"
    if quality_flags:
        score_str += f"\n{' · '.join(quality_flags)}"
    return (
        f"📈 <b>Price spike alert — {cat}</b>\n"
        f"<i>{q}</i>\n\n"
        f"Spike:   <b>{prev_price:.1f}¢ → {market['yes_price']:.1f}¢  (+{rise:.1f} pts)</b>\n"
        f"Volume:  ${market['volume_24h']:,.0f} 24h  {vol_spike:.1f}× avg volume\n"
        f"Liquidity: ${market['liquidity']:,.0f}\n"
        f"<i>Consider buying NO if spike is sentiment-driven speculation.</i>"
        f"{amb_str}"
        f"{score_str}"
        f"{url_tag}"
    )


def format_ambient_alert(market: dict, ambient_vol: float) -> str:
    q = market["question"][:90] + ("…" if len(market["question"]) > 90 else "")
    url = market.get("url", "")
    url_tag = f'\n<a href="{url}">Open on Polymarket</a>' if url else ""
    return (
        f"🔀 <b>High ambient volatility — retail signal</b>\n"
        f"<i>{q}</i>\n\n"
        f"Avg daily move: <b>{ambient_vol:.1f} pts/day</b>  (threshold: {AMBIENT_VOL_HIGH}+)\n"
        f"Category: {market.get('category','unknown')}\n"
        f"Current price: {market['yes_price']:.1f}¢  ·  Liquidity: ${market['liquidity']:,.0f}\n"
        f"<i>Pre-qualify as a fade candidate when news hits this market.</i>"
        f"{url_tag}"
    )


# ── Correlated market lag detector ────────────────────────────────────────────

def _extract_keywords(question: str, max_kw: int = 8) -> set[str]:
    """Extract meaningful keywords from a market question."""
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", question).lower().split()
    kws = [w for w in words if w not in STOPWORDS and len(w) >= 3]
    # Prioritise longer words — more distinctive
    kws.sort(key=len, reverse=True)
    return set(kws[:max_kw])


def find_lagging_correlated(market: dict, direction: str) -> list[dict]:
    """
    Find markets in the DB that are topically correlated with the alerted
    market but haven't repriced yet in the expected direction.

    direction: "DROP" — look for correlated markets still holding high
               "SPIKE" — look for correlated markets still holding low

    Returns list of dicts sorted by lag magnitude (biggest opportunity first).
    """
    try:
        trigger_kws  = _extract_keywords(market["question"])
        trigger_cid  = market["condition_id"]
        trigger_price = market["yes_price"]

        if len(trigger_kws) < 2:
            return []

        # Pull all markets + their latest price from DB in one query
        with db.conn() as c:
            rows = c.execute("""
                SELECT m.condition_id, m.question, m.url, m.liquidity,
                       p.price as current_price
                FROM markets m
                JOIN price_history p ON p.condition_id = m.condition_id
                JOIN (
                    SELECT condition_id, MAX(polled_at) AS latest
                    FROM price_history
                    GROUP BY condition_id
                ) t ON t.condition_id = p.condition_id
                    AND p.polled_at = t.latest
                WHERE m.condition_id != ?
                  AND m.liquidity >= ?
                  AND (m.category IS NULL OR m.category != 'sports')
                  AND p.price > 1
                  AND p.price < 99
            """, (trigger_cid, MIN_LIQUIDITY)).fetchall()

        lagging = []

        for row in rows:
            cid      = row["condition_id"]
            question = row["question"]
            price    = row["current_price"]
            liq      = row["liquidity"]

            # Skip fixture-style markets
            q_lower = question.lower()
            if any(kw in q_lower for kw in FIXTURE_KEYWORDS):
                continue

            # Check keyword overlap
            candidate_kws = _extract_keywords(question)
            overlap = trigger_kws & candidate_kws
            if len(overlap) < CORR_MIN_OVERLAP:
                continue

            # Fetch 24h ago price to compute this market's delta
            prev = db.price_n_hours_ago(cid, hours=24)
            if prev is None:
                continue

            delta = price - prev  # positive = rose, negative = fell

            # "Lagging" means: the correlated market has NOT moved in the
            # direction the trigger market moved, beyond the lag threshold.
            #
            # DROP trigger: correlated market should have dropped too.
            #   If it hasn't dropped >CORR_LAG_THRESHOLD, it's lagging.
            # SPIKE trigger: correlated market should have risen too.
            #   If it hasn't risen >CORR_LAG_THRESHOLD, it's lagging.

            if direction == "DROP" and delta > -CORR_LAG_THRESHOLD:
                # Still holding high — lagging drop candidate
                lag_pts = abs(delta - (-CORR_LAG_THRESHOLD))  # how much it should still fall
                lagging.append({
                    "question":  question,
                    "url":       row["url"] or "",
                    "price":     round(price, 1),
                    "prev":      round(prev, 1),
                    "delta":     round(delta, 1),
                    "lag_pts":   round(lag_pts, 1),
                    "overlap":   sorted(overlap),
                    "liquidity": liq,
                })

            elif direction == "SPIKE" and delta < CORR_LAG_THRESHOLD:
                # Still holding low — lagging spike candidate
                lag_pts = abs(CORR_LAG_THRESHOLD - delta)
                lagging.append({
                    "question":  question,
                    "url":       row["url"] or "",
                    "price":     round(price, 1),
                    "prev":      round(prev, 1),
                    "delta":     round(delta, 1),
                    "lag_pts":   round(lag_pts, 1),
                    "overlap":   sorted(overlap),
                    "liquidity": liq,
                })

        # Sort by liquidity desc (most tradeable first), cap results
        lagging.sort(key=lambda x: x["liquidity"], reverse=True)
        return lagging[:CORR_MAX_RESULTS]

    except Exception as e:
        log.error(f"[correlate] Error finding lagging markets: {e}")
        return []


def format_lag_section(lagging: list[dict], direction: str,
                       trigger_question: str) -> str:
    """Format a Telegram follow-up message listing lagging correlated markets."""
    arrow  = "📉" if direction == "DROP" else "📈"
    action = "haven't dropped yet" if direction == "DROP" else "haven't spiked yet"
    trade  = "NO" if direction == "DROP" else "YES"   # what to buy on the lag

    lines = [
        f"🔗 <b>Correlated markets — {action}</b>\n"
        f"<i>Trigger: {trigger_question[:70]}</i>\n"
    ]

    for m in lagging:
        delta_str = f"{m['delta']:+.1f}¢" if m['delta'] != 0 else "flat"
        kw_str    = ", ".join(m["overlap"][:3])
        url_tag   = f'<a href="{m["url"]}">→</a> ' if m["url"] else ""
        lines.append(
            f"{arrow} {url_tag}<b>{m['price']:.1f}¢</b>  ({delta_str} 24h)  "
            f"liq ${m['liquidity']:,.0f}\n"
            f"   <i>{m['question'][:75]}</i>\n"
            f"   keywords: {kw_str}"
        )

    lines.append(f"\n<i>Consider {trade} on any of the above if thesis holds.</i>")
    return "\n\n".join(lines)


# ── Core scan ─────────────────────────────────────────────────────────────────

def _days_to_resolution(end_date_str: str) -> int | None:
    """Return days until market resolves, or None if no end date."""
    if not end_date_str:
        return None
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        delta = end.replace(tzinfo=None) - datetime.utcnow()
        return max(0, delta.days)
    except Exception:
        return None


def _alert_quality_score(market: dict, analysis: dict, alert_type: str) -> tuple[int, list[str]]:
    """
    Score 0-100. Higher = more likely to be a genuine overcorrection worth evaluating.
    Returns (score, flags) where flags are reasons the score is high or low.
    """
    score = 0
    flags = []

    price = market.get("yes_price", 50)
    ambient_vol = analysis.get("ambient_vol") or 0
    vol_spike = analysis.get("vol_spike", 1.0)
    liquidity = market.get("liquidity", 0)
    category = market.get("category", "")
    drop = abs(analysis.get("drop", 0)) if alert_type == "DROP" else abs(analysis.get("rise", 0))
    days = _days_to_resolution(market.get("end_date", ""))

    # Liquidity (max 20 pts)
    if liquidity >= 10000:
        score += 20; flags.append("high liquidity")
    elif liquidity >= 5000:
        score += 15
    elif liquidity >= 2000:
        score += 8
    else:
        flags.append("⚠ thin liquidity")

    # Volume spike strength (max 20 pts)
    if vol_spike >= 5.0:
        score += 20; flags.append(f"{vol_spike:.1f}× vol spike")
    elif vol_spike >= 3.0:
        score += 15; flags.append(f"{vol_spike:.1f}× vol spike")
    elif vol_spike >= 1.8:
        score += 8

    # Ambient volatility — high = retail dominated = better fades (max 15 pts)
    if ambient_vol >= 7:
        score += 15; flags.append("high retail vol")
    elif ambient_vol >= 4:
        score += 10
    elif ambient_vol >= 2:
        score += 5
    else:
        flags.append("low ambient vol")

    # Move magnitude (max 15 pts)
    if drop >= 30:
        score += 15; flags.append(f"{drop:.0f}pt move")
    elif drop >= 20:
        score += 10; flags.append(f"{drop:.0f}pt move")
    elif drop >= 15:
        score += 5

    # Category (max 15 pts) — politics/macro fade best
    if category in ("politics", "macro"):
        score += 15
    elif category == "science":
        score += 8
    elif category == "crypto":
        score += 5; flags.append("crypto — noisy")
    else:
        flags.append(f"category: {category}")

    # Days to resolution (max 15 pts) — sweet spot 14-90 days
    if days is None:
        score += 8  # unknown, assume ok
    elif 14 <= days <= 90:
        score += 15
    elif 7 <= days < 14:
        score += 8; flags.append(f"short window ({days}d)")
    elif days > 90:
        score += 10
    else:
        score -= 10; flags.append(f"⚠ only {days}d remaining")

    # Price location — extremes are harder to fade
    if alert_type == "DROP" and price < 5:
        score -= 15; flags.append("⚠ near zero — time decay risk")
    elif alert_type == "SPIKE" and price > 95:
        score -= 15; flags.append("⚠ near ceiling")

    return max(0, min(100, score)), flags


def _is_tradeable(market: dict) -> bool:
    if market.get("category") == "sports":
        return False
    if market["liquidity"] < MIN_LIQUIDITY:
        return False
    if market["volume_24h"] < MIN_VOLUME_24H:
        return False
    q = market.get("question", "").lower()
    if any(kw in q for kw in FIXTURE_KEYWORDS):
        return False
    days = _days_to_resolution(market.get("end_date", ""))
    if days is not None and days < MIN_DAYS_TO_RES:
        return False
    return True


def analyse_market(market: dict) -> dict:
    cid     = market["condition_id"]
    price   = market["yes_price"]
    vol_24h = market["volume_24h"]

    prev_price  = db.price_n_hours_ago(cid, hours=24)
    ambient_vol = db.ambient_volatility(cid, days=30)
    vol_spike_r = db.volume_spike(cid, vol_24h) if vol_24h else 1.0

    drop = (prev_price - price) if prev_price else 0
    rise = (price - prev_price) if prev_price else 0

    alert_types = []

    if (prev_price is not None
            and rise >= SPIKE_THRESHOLD
            and vol_spike_r >= VOLUME_SPIKE_RATIO):
        alert_types.append("SPIKE")

    if (prev_price is not None
            and drop >= DROP_THRESHOLD
            and vol_spike_r >= VOLUME_SPIKE_RATIO):
        alert_types.append("DROP")
    elif (prev_price is not None
          and drop >= DROP_THRESHOLD + 5):
        alert_types.append("DROP")

    return {
        "condition_id": cid,
        "price":        price,
        "prev_price":   prev_price,
        "drop":         round(drop, 2),
        "rise":         round(rise, 2),
        "ambient_vol":  ambient_vol,
        "vol_spike":    vol_spike_r,
        "alert_types":  alert_types,
    }


def run_followup_check(dry_run: bool = False) -> None:
    """
    For every alert fired 1.5-3 hours ago, check current price vs alert price.
    If market moved further in alert direction (>3pts), send a THESIS WEAKENING message.
    If market recovered (>5pts back), send a THESIS STRENGTHENING message.
    """
    try:
        alerts = db.get_recent_alerts(hours_min=1.5, hours_max=3.0)
    except Exception as e:
        log.error(f"[followup] DB query failed: {e}")
        return

    for alert in alerts:
        cid = alert["condition_id"]
        alert_price = alert["price_at_alert"]
        alert_type = alert["alert_type"]

        if alert_price is None:
            continue

        current_price = db.latest_price(cid)
        if current_price is None:
            continue

        delta = current_price - alert_price  # positive = price rose since alert

        if alert_type == "DROP":
            continued_move = delta < -3    # kept dropping
            recovering = delta > 5         # bouncing back
        else:  # SPIKE
            continued_move = delta > 3     # kept rising
            recovering = delta < -5        # falling back

        if continued_move:
            msg = (
                f"⚠️ <b>Thesis weakening — {alert['category']}</b>\n"
                f"<i>{alert['question'][:80]}</i>\n\n"
                f"Alert price: {alert_price:.1f}¢  →  Now: {current_price:.1f}¢  "
                f"({delta:+.1f}pts since alert)\n"
                f"<i>Market continuing to move in alert direction — "
                f"may be structural repricing, not overcorrection.</i>\n"
                f'<a href="{alert["url"]}">Check market</a>'
            )
            if not dry_run:
                send_telegram(msg)
            else:
                log.info(f"[followup DRY RUN] Weakening: {alert['question'][:50]}")

        elif recovering:
            msg = (
                f"✅ <b>Thesis strengthening — {alert['category']}</b>\n"
                f"<i>{alert['question'][:80]}</i>\n\n"
                f"Alert price: {alert_price:.1f}¢  →  Now: {current_price:.1f}¢  "
                f"({delta:+.1f}pts since alert)\n"
                f"<i>Market recovering from the move — overcorrection thesis intact.</i>\n"
                f'<a href="{alert["url"]}">Check market</a>'
            )
            if not dry_run:
                send_telegram(msg)
            else:
                log.info(f"[followup DRY RUN] Strengthening: {alert['question'][:50]}")


def run_scan(dry_run: bool = False) -> dict:
    """
    Full scan cycle. Returns summary dict with counts.
    dry_run=True: analyse and log but don't send Telegram messages.
    """
    log.info("=== Scan cycle starting ===")
    start = time.time()

    markets_seen  = 0
    prices_stored = 0
    alerts_fired  = 0
    drop_markets  = []

    for market in fetch_all_active_markets():
        markets_seen += 1

        db.upsert_market(
            condition_id = market["condition_id"],
            question     = market["question"],
            slug         = market.get("slug", ""),
            url          = market.get("url", ""),
            token_id_yes = market.get("token_id_yes", ""),
            category     = market.get("category", "politics"),
            end_date     = market.get("end_date", ""),
        )

        db.record_price(
            condition_id = market["condition_id"],
            price        = market["yes_price"],
            volume_24h   = market["volume_24h"],
            liquidity    = market["liquidity"],
        )
        prices_stored += 1

        if not _is_tradeable(market):
            continue

        analysis = analyse_market(market)

        for alert_type in analysis["alert_types"]:
            if not db.alert_cooldown_passed(
                market["condition_id"], alert_type, ALERT_COOLDOWN_HRS
            ):
                log.debug(f"Cooldown active — skipping {alert_type} for {market['question'][:40]}")
                continue

            quality_score, quality_flags = _alert_quality_score(market, analysis, alert_type)
            log.info(f"[quality] {alert_type} score={quality_score} flags={quality_flags} "
                     f"for {market['question'][:40]}")

            if alert_type == "DROP":
                msg = format_drop_alert(
                    market,
                    analysis["drop"],
                    analysis["ambient_vol"],
                    analysis["vol_spike"],
                    analysis["prev_price"],
                    quality_score,
                    quality_flags,
                )
                drop_markets.append({
                    "question":      market["question"],
                    "url":           market.get("url", ""),
                    "price":         market["yes_price"],
                    "prev_price":    analysis["prev_price"],
                    "drop":          analysis["drop"],
                    "ambient_vol":   analysis["ambient_vol"],
                    "vol_spike":     analysis["vol_spike"],
                    "category":      market.get("category", ""),
                    "quality_score": quality_score,
                    "quality_flags": quality_flags,
                })
            elif alert_type == "SPIKE":
                msg = format_spike_alert(
                    market,
                    analysis["rise"],
                    analysis["ambient_vol"],
                    analysis["vol_spike"],
                    analysis["prev_price"],
                    quality_score,
                    quality_flags,
                )

            if not dry_run:
                if send_telegram(msg):
                    # ── Sheets export ──────────────────────────────────────
                    if alert_type == "DROP":
                        log_drop_alert(
                            market        = market,
                            drop_pts      = analysis["drop"],
                            prev_price    = analysis["prev_price"],
                            vol_spike     = analysis["vol_spike"],
                            ambient_vol   = analysis["ambient_vol"],
                            quality_score = quality_score,
                            quality_flags = quality_flags,
                        )
                    elif alert_type == "SPIKE":
                        log_spike_alert(
                            market        = market,
                            spike_pts     = analysis["rise"],
                            prev_price    = analysis["prev_price"],
                            vol_spike     = analysis["vol_spike"],
                            ambient_vol   = analysis["ambient_vol"],
                            quality_score = quality_score,
                            quality_flags = quality_flags,
                        )
                    # ── DB log ─────────────────────────────────────────────
                    db.log_alert(
                        market["condition_id"], alert_type,
                        price         = market["yes_price"],
                        drop          = analysis.get("drop"),
                        ambient_vol   = analysis.get("ambient_vol"),
                        quality_score = quality_score,
                        quality_flags = quality_flags,
                    )
                    alerts_fired += 1

                    # ── Correlated lag check ───────────────────────────────
                    lagging = find_lagging_correlated(market, alert_type)
                    if lagging:
                        lag_msg = format_lag_section(
                            lagging, alert_type, market["question"]
                        )
                        send_telegram(lag_msg)
                        log.info(
                            f"[correlate] {len(lagging)} lagging markets found "
                            f"for {market['question'][:40]}"
                        )

            else:
                log.info(f"[DRY RUN] Would alert {alert_type}: {market['question'][:60]}")
                log.info(f"[DRY RUN]   Quality score: {quality_score}/100  flags: {quality_flags}")
                # Still run correlation in dry-run so you can see what it finds
                lagging = find_lagging_correlated(market, alert_type)
                if lagging:
                    log.info(f"[DRY RUN] Correlated lag candidates ({len(lagging)}):")
                    for m in lagging:
                        log.info(f"  {m['price']:.1f}¢  {m['question'][:60]}")
                alerts_fired += 1

    run_followup_check(dry_run=dry_run)

    elapsed = round(time.time() - start, 1)
    summary = {
        "markets_scanned": markets_seen,
        "prices_stored":   prices_stored,
        "alerts_fired":    alerts_fired,
        "drop_candidates": len(drop_markets),
        "elapsed_secs":    elapsed,
        "drop_markets":    drop_markets,
    }
    log.info(
        f"=== Scan complete: {markets_seen} markets, "
        f"{alerts_fired} alerts, {elapsed}s ==="
    )
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket market scanner")
    parser.add_argument("--loop",     action="store_true", help="Run continuously")
    parser.add_argument("--lag",      action="store_true",
                        help="Find stale extreme markets (NO-buy opportunities)")
    parser.add_argument("--dry-run",  action="store_true", help="Analyse only, no Telegram")
    parser.add_argument("--stats",    action="store_true", help="Print DB stats and exit")
    parser.add_argument("--prune",    action="store_true", help="Prune old readings and exit")
    parser.add_argument("--report",   action="store_true",
                        help="Print current drop candidates as JSON and exit")
    parser.add_argument("--followup", action="store_true",
                        help="Run 2-hour followup check and exit")
    args = parser.parse_args()

    db.init_db()

    if args.followup:
        run_followup_check(dry_run=args.dry_run)
        return

    if args.stats:
        import json
        print(json.dumps(db.db_stats(), indent=2))
        return

    if args.prune:
        n = db.prune_old_readings(keep_days=PRUNE_DAYS)
        print(f"Pruned {n} readings.")
        return

    if args.report:
        summary = run_scan(dry_run=True)
        import json
        print(json.dumps(summary["drop_markets"], indent=2))
        return

    if args.lag:
        from db import stale_extreme_markets
        candidates = stale_extreme_markets(
            min_days_at_extreme=7,
            min_liquidity=2000,
            extreme_threshold=5.0
        )
        print(f"\n{'SIGNAL':<9}  {'PRICE':>7}  {'EDGE':>6}  "
              f"{'LIQUIDITY':>10}  QUESTION")
        print("─" * 85)
        for m in candidates:
            print(f"{m['signal']:<9}  {m['price']:>6.1f}¢  "
                  f"{m['edge_pts']:>5.1f}¢  "
                  f"${m['liquidity']:>9,.0f}  "
                  f"{m['question'][:50]}")
        print(f"\n{len(candidates)} stale extreme markets found\n")
        return

    if args.loop:
        log.info(f"Starting continuous scanner (interval: {POLL_INTERVAL}s)…")
        while True:
            try:
                run_scan(dry_run=args.dry_run)
            except Exception as e:
                log.error(f"Scan error: {e}", exc_info=True)
            log.info(f"Sleeping {POLL_INTERVAL}s…")
            time.sleep(POLL_INTERVAL)
    else:
        run_scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
