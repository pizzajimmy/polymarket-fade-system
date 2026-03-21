"""
pretrade.py — combined pre-trade check
========================================
Runs slippage simulation and cross-platform price lookup together.
This is the single command you run before every trade entry.

Usage:
  python pretrade.py TOKEN_ID USDC_AMOUNT PM_PRICE QUESTION
  python pretrade.py TOKEN_ID 250 28 "Will the Fed cut rates in June?"
  python pretrade.py --market SLUG 250 28 "Will the Fed cut rates in June?"
  python pretrade.py TOKEN_ID 250 28 "fed rate cut" --category macro

Output:
  1. Order book summary + slippage simulation
  2. Cross-platform prices + weighted fair value
  3. Composite pre-trade verdict

The verdict answers: is this worth entering at this size?
"""

import sys
import argparse
import logging
import os

from slippage import (
    fetch_book, book_summary, simulate_fill,
    size_recommendation, token_id_from_slug,
    SLIP_OK, SLIP_WARN,
)
from sources import (
    fetch_all_sources, weighted_fair_value,
    gap_interpretation, print_source_report,
    extract_keywords,
)

logging.basicConfig(
    level=logging.WARNING,   # quiet by default — only errors
    format="%(levelname)s  %(message)s",
)

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"


# ── Composite verdict ─────────────────────────────────────────────────────────

def composite_verdict(slip_pts: float | None,
                      slip_error: str | None,
                      fv: float | None,
                      pm_price: float,
                      recommended_usdc: float,
                      requested_usdc: float,
                      found_sources: int) -> dict:
    """
    Combines slippage and source signals into one pre-trade verdict.

    Returns:
      action:   GO | REDUCE | PASS
      reasons:  list of bullet points
      kelly_entry: the price to use in the Kelly calculator
    """
    reasons  = []
    warnings = []
    blockers = []

    # ── Slippage gate ─────────────────────────────────────────────────────────
    if slip_error:
        blockers.append(f"Order book error: {slip_error}")
        kelly_entry = pm_price
    elif slip_pts is None:
        warnings.append("Could not simulate slippage — check token_id")
        kelly_entry = pm_price
    elif slip_pts > SLIP_WARN:
        blockers.append(
            f"Slippage {slip_pts:+.1f}pts exceeds {SLIP_WARN}pt threshold — "
            f"edge is consumed by fill cost"
        )
        kelly_entry = pm_price + slip_pts
    elif slip_pts > SLIP_OK:
        warnings.append(
            f"Slippage {slip_pts:+.1f}pts — size halved to ${recommended_usdc:,.0f}"
        )
        kelly_entry = pm_price + slip_pts
    else:
        reasons.append(f"Slippage {slip_pts:+.1f}pts — acceptable")
        kelly_entry = pm_price + slip_pts

    # ── Fair value gate ───────────────────────────────────────────────────────
    if fv is None:
        warnings.append("No cross-platform prices found — using your own estimate as fair value")
    else:
        edge = fv - kelly_entry
        if edge >= 12:
            reasons.append(f"Strong edge: fair value {fv}¢ vs entry ~{kelly_entry:.1f}¢ (+{edge:.1f}pts)")
        elif edge >= 6:
            reasons.append(f"Decent edge: fair value {fv}¢ vs entry ~{kelly_entry:.1f}¢ (+{edge:.1f}pts)")
        elif edge >= 3:
            warnings.append(f"Thin edge after slippage: {edge:.1f}pts — verify your estimate")
        else:
            blockers.append(
                f"No edge after slippage: fair value {fv}¢ barely above fill price {kelly_entry:.1f}¢"
            )

    # ── Source confirmation ───────────────────────────────────────────────────
    if found_sources == 0:
        warnings.append("Zero cross-platform confirmations — proceed with extra caution")
    elif found_sources == 1:
        warnings.append("Only one cross-platform confirmation — ideally want 2+")
    else:
        reasons.append(f"{found_sources} cross-platform sources confirmed")

    # ── Final action ──────────────────────────────────────────────────────────
    if blockers:
        action = "PASS"
    elif warnings and not reasons:
        action = "PASS"
    elif len(warnings) >= 2:
        action = "REDUCE"
    elif recommended_usdc < requested_usdc * 0.9:
        action = "REDUCE"
    else:
        action = "GO"

    return {
        "action":          action,
        "reasons":         reasons,
        "warnings":        warnings,
        "blockers":        blockers,
        "kelly_entry":     round(kelly_entry, 1),
        "recommended_usdc": recommended_usdc,
    }


def print_verdict(verdict: dict, fv: float | None, pm_price: float):
    action = verdict["action"]
    colors = {"GO": GREEN, "REDUCE": YELLOW, "PASS": RED}
    col = colors.get(action, "")

    print(f"\n{BOLD}{'═'*62}{RESET}")
    print(f"  Pre-trade verdict:  {BOLD}{col}{action}{RESET}")
    print(f"{'═'*62}")

    for r in verdict["reasons"]:
        print(f"  {GREEN}✓{RESET}  {r}")
    for w in verdict["warnings"]:
        print(f"  {YELLOW}⚠{RESET}  {w}")
    for b in verdict["blockers"]:
        print(f"  {RED}✗{RESET}  {b}")

    print(f"\n  {'─'*58}")
    ke = verdict["kelly_entry"]
    print(f"  Kelly calculator inputs:")
    print(f"    Entry price (VWAP):  {BOLD}{ke}¢{RESET}  {DIM}← use this, not displayed price{RESET}")
    if fv:
        print(f"    Fair value:          {BOLD}{fv}¢{RESET}")
        print(f"    Edge:                {BOLD}{fv - ke:+.1f} pts{RESET}")
    print(f"    Position size:       {BOLD}${verdict['recommended_usdc']:,.0f}{RESET}")
    print(f"\n{'═'*62}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Combined pre-trade check: slippage + cross-platform prices"
    )
    parser.add_argument("token_id", nargs="?",
                        help="YES token ID from clobTokenIds[0]")
    parser.add_argument("usdc",     type=float, nargs="?",
                        help="USDC amount you intend to trade")
    parser.add_argument("pm_price", type=float, nargs="?",
                        help="Current Polymarket price in cents")
    parser.add_argument("question", nargs="?",
                        help="Market question (for cross-platform search)")
    parser.add_argument("--market", "-m", metavar="SLUG",
                        help="Resolve token_id from market slug")
    parser.add_argument("--category", "-c",
                        choices=["politics", "crypto", "macro", "sports", "science"],
                        default="politics")
    parser.add_argument("--kalshi-token", metavar="TOKEN",
                        help="Kalshi API token (optional)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    # Need at minimum: token_id/slug + usdc + pm_price
    if not args.pm_price:
        parser.print_help()
        print("\nExample:")
        print('  python pretrade.py TOKEN_ID 250 28 "Will the Fed cut rates in June?"')
        print('  python pretrade.py --market fed-rate-cut 250 28 "fed rate cut"')
        sys.exit(0)

    # Resolve token_id
    question = args.question or ""
    token_id = args.token_id or ""

    if args.market:
        try:
            token_id, resolved_q = token_id_from_slug(args.market)
            if not question:
                question = resolved_q
        except Exception as e:
            print(f"Could not resolve market slug: {e}")
            sys.exit(1)

    pm_price = args.pm_price
    usdc     = args.usdc

    # ── 1. Slippage simulation ────────────────────────────────────────────────
    print(f"\n{CYAN}{'─'*62}")
    print(f"  Checking order book slippage…{RESET}")

    slip_pts, slip_error = None, None
    recommended_usdc     = usdc
    summary_data         = {}

    if token_id:
        try:
            book     = fetch_book(token_id)
            summary  = book_summary(book)
            sim      = simulate_fill(book.get("asks", []), usdc)
            rec      = size_recommendation(sim, usdc)

            summary_data     = summary
            recommended_usdc = rec.get("recommended_usdc", usdc) or usdc

            if "error" in sim:
                slip_error = sim["error"]
            else:
                slip_pts = sim.get("slippage_pts")

            # Print book summary
            print(f"\n  Mid price:  {BOLD}{summary['mid_price_cents']}¢{RESET}"
                  f"  spread {summary['spread_pts']}pts")
            print(f"  Depth (5 levels):  bids ${summary['bid_depth_5lvl']:,.0f}"
                  f"  /  asks ${summary['ask_depth_5lvl']:,.0f}")
            if slip_pts is not None:
                from slippage import color_slippage
                print(f"  Slippage for ${usdc:,.0f}:  {color_slippage(slip_pts)} pts"
                      f"  →  VWAP {sim['vwap_cents']}¢")
            if rec["action"] == "REDUCE":
                print(f"  {YELLOW}Size reduced to ${recommended_usdc:,.0f}{RESET}")
            elif rec["action"] == "PASS":
                print(f"  {RED}Book too thin — consider passing{RESET}")

        except Exception as e:
            slip_error = str(e)
            print(f"  {RED}Order book fetch failed: {e}{RESET}")
    else:
        print(f"  {YELLOW}No token_id — skipping slippage check{RESET}")

    # ── 2. Cross-platform prices ──────────────────────────────────────────────
    print(f"\n{CYAN}{'─'*62}")
    print(f"  Searching cross-platform prices…{RESET}")

    fv           = None
    found_count  = 0

    if question:
        fetch_result = fetch_all_sources(
            question     = question,
            kalshi_token = args.kalshi_token or "",
            category     = args.category,
        )
        fv          = weighted_fair_value(fetch_result["results"], args.category)
        found_count = fetch_result["found_count"]
        print_source_report(fetch_result, pm_price=pm_price, category=args.category)
    else:
        print(f"  {YELLOW}No question provided — skipping cross-platform lookup{RESET}")
        print(f"  {DIM}Pass the question as the 4th argument to enable this.{RESET}\n")

    # ── 3. Composite verdict ──────────────────────────────────────────────────
    verdict = composite_verdict(
        slip_pts         = slip_pts,
        slip_error       = slip_error,
        fv               = fv,
        pm_price         = pm_price,
        recommended_usdc = recommended_usdc,
        requested_usdc   = usdc,
        found_sources    = found_count,
    )

    print_verdict(verdict, fv, pm_price)


if __name__ == "__main__":
    main()
