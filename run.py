"""
run.py — production entrypoint
================================
Starts the market scanner and price alert bot as parallel threads
so they share the same SQLite database file.

Used by Railway (or any single-container deployment).
Start command: python run.py

For local development, run each script individually instead:
  python scanner.py --loop
  python alert_bot.py --loop
"""

import threading
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-14s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run")


def run_scanner():
    try:
        import db
        from scanner import run_scan
        import time
        interval = int(os.environ.get("POLL_INTERVAL_SECS", "1800"))
        db.init_db()
        log.info(f"Scanner started (interval: {interval}s)")
        while True:
            try:
                run_scan()
            except Exception as e:
                log.error(f"Scanner error: {e}", exc_info=True)
            time.sleep(interval)
    except Exception as e:
        log.critical(f"Scanner thread crashed: {e}", exc_info=True)


def run_alertbot():
    try:
        from alert_bot import run_loop
        import time
        # Small delay so scanner initialises the DB first
        time.sleep(5)
        log.info("Alert bot started")
        run_loop()
    except Exception as e:
        log.critical(f"Alert bot thread crashed: {e}", exc_info=True)


if __name__ == "__main__":
    log.info("Starting Polymarket Fade System…")

    scanner_thread = threading.Thread(
        target=run_scanner, name="scanner", daemon=True
    )
    alertbot_thread = threading.Thread(
        target=run_alertbot, name="alertbot", daemon=True
    )

    scanner_thread.start()
    alertbot_thread.start()

    # Keep main thread alive — if either child crashes, log it
    try:
        scanner_thread.join()
        alertbot_thread.join()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        sys.exit(0)
