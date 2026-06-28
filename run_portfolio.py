"""
run_portfolio.py — entrypoint for the strategy-portfolio harness.

  python run_portfolio.py run            # one scan cycle
  python run_portfolio.py run --dry-run  # cycle, but no Telegram pushes
  python run_portfolio.py loop           # continuous (POLL_INTERVAL_SECS)
  python run_portfolio.py stats          # DB summary
"""

import sys
import os
import time
import json
import logging
import argparse
from pathlib import Path

# Load .env (local dev) before anything reads config.
_envf = Path(__file__).with_name(".env")
if _envf.exists():
    for line in _envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from pmfade import store, config as C
from pmfade import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    p = argparse.ArgumentParser(description="Polymarket strategy-portfolio harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="one scan cycle")
    pr.add_argument("--dry-run", action="store_true", help="no Telegram pushes")
    sub.add_parser("loop", help="run continuously")
    sub.add_parser("stats", help="DB summary")
    args = p.parse_args()

    store.init_db()

    if args.cmd == "stats":
        print(json.dumps(store.db_stats(), indent=2))
        return

    if args.cmd == "run":
        summary = engine.run_cycle(dry_run=args.dry_run)
        print(json.dumps(summary, indent=2))
        return

    if args.cmd == "loop":
        logging.getLogger().info("continuous loop — interval %ss", C.POLL_INTERVAL_SECS)
        while True:
            try:
                engine.run_cycle(dry_run=False)
            except Exception as e:
                logging.getLogger().error("cycle crashed: %s", e, exc_info=True)
            time.sleep(C.POLL_INTERVAL_SECS)


if __name__ == "__main__":
    main()
