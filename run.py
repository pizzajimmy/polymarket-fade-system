"""
run.py — Production entrypoint.

Threads:
  1. scanner    — scans Gamma every cycle, fires live alerts to TG_CHAT_ID
  2. alert_bot  — monitors positions.json hourly via CLOB API
"""

import threading
import time
import os

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scanner   import run_scanner
from alert_bot import run_alert_bot


def run_thread(name: str, fn):
    """Wrap a loop function in a daemon thread with crash-restart."""
    def wrapper():
        while True:
            try:
                print(f"[{name}] Starting")
                fn()
            except Exception as e:
                print(f"[{name}] CRASHED: {e} — restarting in 30s")
                time.sleep(30)
    t = threading.Thread(target=wrapper, name=name, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    print("[run] Polymarket fade system starting")
    print(f"[run] Live alerts → chat {os.getenv('TG_CHAT_ID', 'NOT SET')}")

    threads = [
        run_thread("scanner",   run_scanner),
        run_thread("alert_bot", run_alert_bot),
    ]

    # Keep main thread alive
    while True:
        alive = [t.name for t in threads if t.is_alive()]
        dead  = [t.name for t in threads if not t.is_alive()]
        if dead:
            print(f"[run] WARNING — dead threads: {dead}")
        time.sleep(300)
