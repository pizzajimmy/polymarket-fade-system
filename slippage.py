"""
slippage.py — CLOB slippage simulator
=======================================
Fetches the live Polymarket order book and simulates your fill
before you enter a trade. Tells you the true average entry price
(VWAP), slippage in points, and a sized recommendation.

Usage:
  python slippage.py TOKEN_ID USDC_AMOUNT
  python slippage.py TOKEN_ID              ← shows book summary only
  python slippage.py --market SLUG USDC    ← looks up token_id from slug

Examples:
  python slippage.py 71321045abc... 250
  python slippage.py --market fed-rate-cut-june 500
"""

import sys
import json
import argparse
import logging
import requests

log = logging.getLogger("slippage")

CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
TIMEOUT    = 10

# Decision thresholds
SLIP_OK     = 2.0   # pts — proceed at full size
SLIP_WARN   = 4.0   # pts — halve the size
# above SLIP_WARN = pass on the trade


# ── API calls ─────────────────────────────────────────────────────────────────

def fetch_book(token_id: str) -> dict:
    """Fetch raw order book from CLOB API."""
    r = requests.get(
        f"{CLOB_BASE}/book",
        params={"token_id": token_id},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def token_id_from_slug(slug: str) -> tuple[str, str]:
    """
    Resolve a market slug to its YES token_id and question.
    Returns (token_id, question).
    """
    r = requests.get(
        f"{GAMMA_BASE}/markets",
        params={"slug": slug},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"No market found for slug: {slug}")
    market = data[0] if isinstance(data, list) else data
    token_ids = market.get("clobTokenIds", [])
    if not token_ids:
        raise ValueError(f"Market has no clobTokenIds: {slug}")
    return token_ids[0], market.get("question", "")


# ── Core simulation ───────────────────────────────────────────────────────────

def simulate_fill(asks: list, usdc_amount: float) -> dict:
    """
    Walk the ask side of the book and compute VWAP for a given USDC spend.

    asks: list of [price_str, usdc_size_str] sorted cheapest first
    usdc_amount: dollars to spend

    Returns dict with vwap, slippage, shares, etc.
    """
    if not asks:
        return {"error": "empty_book"}

    filled  = 0.0
    shares  = 0.0
    levels_consumed = 0

    for price_s, size_s in asks:
        price = float(price_s)
        size  = float(size_s)      # USDC available at this level
        take  = min(size, usdc_amount - filled)

        shares  += take / price
        filled  += take
        levels_consumed += 1

        if filled >= usdc_amount:
            break

    if filled < usdc_amount * 0.90:
        return {
            "error":         "insufficient_liquidity",
            "fillable_usdc": round(filled, 2),
            "requested_usdc": usdc_amount,
        }

    best_ask = float(asks[0][0])
    vwap     = filled / shares if shares else best_ask

    return {
        "best_ask_cents":  round(best_ask * 100, 2),
        "vwap_cents":      round(vwap * 100, 2),
        "slippage_pts":    round((vwap - best_ask) * 100, 2),
        "shares":          round(shares, 1),
        "filled_usdc":     round(filled, 2),
        "levels_consumed": levels_consumed,
    }


def book_summary(book: dict) -> dict:
    """Summarise the full order book — spread, depth, imbalance."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    best_bid = float(bids[0][0]) * 100 if bids else None
    best_ask = float(asks[0][0]) * 100 if asks else None
    mid      = round((best_bid + best_ask) / 2, 2) if best_bid and best_ask else None
    spread   = round(best_ask - best_bid, 2) if best_bid and best_ask else None

    bid_depth = sum(float(s) for _, s in bids[:5])   # top 5 levels
    ask_depth = sum(float(s) for _, s in asks[:5])
    imbalance = round(bid_depth / (bid_depth + ask_depth), 2) if (bid_depth + ask_depth) > 0 else None

    return {
        "mid_price_cents": mid,
        "best_bid_cents":  best_bid,
        "best_ask_cents":  best_ask,
        "spread_pts":      spread,
        "bid_depth_5lvl":  round(bid_depth, 0),
        "ask_depth_5lvl":  round(ask_depth, 0),
        "bid_ask_imbalance": imbalance,
        "bid_levels":      len(bids),
        "ask_levels":      len(asks),
    }


# ── Decision logic ────────────────────────────────────────────────────────────

def size_recommendation(sim: dict, requested_usdc: float) -> dict:
    """
    Given simulation results, return a recommended position size
    and a clear action string.
    """
    if "error" in sim:
        if sim["error"] == "insufficient_liquidity":
            fillable = sim.get("fillable_usdc", 0)
            return {
                "action":     "REDUCE",
                "reason":     f"Book can only absorb ${fillable:.0f} of your ${requested_usdc:.0f}",
                "recommended_usdc": fillable * 0.8,   # leave 20% buffer
            }
        return {"action": "PASS", "reason": sim["error"], "recommended_usdc": 0}

    slip = sim["slippage_pts"]

    if slip <= SLIP_OK:
        return {
            "action":          "PROCEED",
            "reason":          f"Slippage {slip:+.1f}pts — within acceptable range",
            "recommended_usdc": requested_usdc,
            "true_entry_cents": sim["vwap_cents"],
        }
    elif slip <= SLIP_WARN:
        reduced = round(requested_usdc * 0.5, 0)
        return {
            "action":          "REDUCE",
            "reason":          f"Slippage {slip:+.1f}pts — halve position size",
            "recommended_usdc": reduced,
            "true_entry_cents": sim["vwap_cents"],
        }
    else:
        return {
            "action":          "PASS",
            "reason":          f"Slippage {slip:+.1f}pts — market too thin, edge consumed",
            "recommended_usdc": 0,
            "true_entry_cents": sim["vwap_cents"],
        }


# ── Formatted output ──────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"


def color_slippage(pts: float) -> str:
    if pts <= SLIP_OK:
        return f"{GREEN}{pts:+.2f}{RESET}"
    elif pts <= SLIP_WARN:
        return f"{YELLOW}{pts:+.2f}{RESET}"
    return f"{RED}{pts:+.2f}{RESET}"


def color_action(action: str) -> str:
    colors = {"PROCEED": GREEN, "REDUCE": YELLOW, "PASS": RED}
    return f"{BOLD}{colors.get(action,'')}{action}{RESET}"


def print_report(token_id: str, question: str,
                 summary: dict, sim: dict | None,
                 rec: dict | None, requested_usdc: float | None):

    print(f"\n{BOLD}{'─'*60}{RESET}")
    if question:
        print(f"{BOLD}{question[:70]}{RESET}")
    print(f"{DIM}Token: {token_id[:20]}…{RESET}")
    print(f"{'─'*60}")

    # Book summary
    print(f"\n{CYAN}Order book{RESET}")
    print(f"  Mid price    {BOLD}{summary['mid_price_cents']}¢{RESET}"
          f"  (bid {summary['best_bid_cents']}¢ / ask {summary['best_ask_cents']}¢)")
    print(f"  Spread       {summary['spread_pts']} pts")
    bid_d  = f"${summary['bid_depth_5lvl']:,.0f}"
    ask_d  = f"${summary['ask_depth_5lvl']:,.0f}"
    imb    = summary['bid_ask_imbalance']
    imb_note = ""
    if imb is not None:
        if imb > 0.6:
            imb_note = f"  {GREEN}(strong bid support){RESET}"
        elif imb < 0.4:
            imb_note = f"  {RED}(ask-heavy — sellers dominate){RESET}"
    print(f"  Depth (5lvl) bids {bid_d}  /  asks {ask_d}{imb_note}")
    print(f"  Levels       {summary['bid_levels']} bids  /  {summary['ask_levels']} asks")

    if sim is None:
        print()
        return

    # Fill simulation
    print(f"\n{CYAN}Fill simulation  (${requested_usdc:,.0f} USDC){RESET}")

    if "error" in sim:
        if sim["error"] == "insufficient_liquidity":
            print(f"  {RED}Insufficient liquidity — can only fill ${sim['fillable_usdc']:,.0f} of ${requested_usdc:,.0f}{RESET}")
        else:
            print(f"  {RED}Error: {sim['error']}{RESET}")
    else:
        print(f"  Best ask     {sim['best_ask_cents']}¢")
        print(f"  Avg fill     {BOLD}{sim['vwap_cents']}¢{RESET}")
        print(f"  Slippage     {color_slippage(sim['slippage_pts'])} pts")
        print(f"  Shares       {sim['shares']:,.0f}")
        print(f"  Levels eaten {sim['levels_consumed']}")

    # Recommendation
    if rec:
        print(f"\n{CYAN}Recommendation{RESET}")
        action_str = color_action(rec['action'])
        print(f"  {action_str}  —  {rec['reason']}")
        if rec.get("recommended_usdc") and rec["action"] != "PASS":
            print(f"  Use ${rec['recommended_usdc']:,.0f} USDC")
        if rec.get("true_entry_cents"):
            print(f"  True entry:  {rec['true_entry_cents']}¢  "
                  f"{DIM}(use this in Kelly calc, not displayed price){RESET}")

    print(f"\n{'─'*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Simulate fill slippage before entering a Polymarket trade"
    )
    parser.add_argument("token_id", nargs="?", help="YES token ID (clobTokenIds[0])")
    parser.add_argument("usdc", nargs="?", type=float, help="USDC amount to spend")
    parser.add_argument("--market", "-m", metavar="SLUG",
                        help="Resolve token_id from market slug")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted report")
    parser.add_argument("--sizes", action="store_true",
                        help="Show slippage table for multiple sizes")
    args = parser.parse_args()

    # Resolve token_id
    token_id, question = "", ""
    if args.market:
        try:
            token_id, question = token_id_from_slug(args.market)
        except Exception as e:
            print(f"Error resolving slug '{args.market}': {e}", file=sys.stderr)
            sys.exit(1)
    elif args.token_id:
        token_id = args.token_id
    else:
        parser.print_help()
        sys.exit(0)

    # Fetch book
    try:
        book = fetch_book(token_id)
    except Exception as e:
        print(f"Failed to fetch order book: {e}", file=sys.stderr)
        sys.exit(1)

    summary = book_summary(book)
    asks    = book.get("asks", [])

    if args.json:
        output = {"summary": summary}
        if args.usdc:
            sim = simulate_fill(asks, args.usdc)
            rec = size_recommendation(sim, args.usdc)
            output["simulation"] = sim
            output["recommendation"] = rec
        print(json.dumps(output, indent=2))
        return

    if args.sizes:
        # Multi-size slippage table
        print(f"\n{BOLD}Slippage table — {question[:55] or token_id[:25]}{RESET}")
        print(f"{'SIZE':>8}  {'VWAP':>8}  {'SLIP':>8}  {'SHARES':>9}  ACTION")
        print("─" * 55)
        for size in [50, 100, 250, 500, 750, 1000, 2000]:
            sim = simulate_fill(asks, size)
            if "error" in sim:
                err = sim["error"].replace("_", " ")
                print(f"${size:>7}  {'—':>8}  {'—':>8}  {'—':>9}  {RED}{err}{RESET}")
                break
            rec = size_recommendation(sim, size)
            sl  = color_slippage(sim["slippage_pts"])
            ac  = color_action(rec["action"])
            print(f"${size:>7}  {sim['vwap_cents']:>7}¢  {sl:>17}  {sim['shares']:>9,.0f}  {ac}")
        print()
        return

    # Standard report
    sim, rec = None, None
    if args.usdc:
        sim = simulate_fill(asks, args.usdc)
        rec = size_recommendation(sim, args.usdc)

    print_report(token_id, question, summary, sim, rec, args.usdc)


if __name__ == "__main__":
    main()
