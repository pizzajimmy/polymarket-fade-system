"""
run.py — Production entrypoint.

Threads:
  1. scanner    — scans Gamma every cycle, fires live alerts to TG_CHAT_ID
  2. alert_bot  — monitors positions.json hourly via CLOB API
"""

import threading
import subprocess
import sys
import time
import os

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
from scanner import run_scan, POLL_INTERVAL


def run_scanner():
    """Loop wrapper for run_scan so it behaves like a long-running thread."""
    while True:
        try:
            run_scan()
        except Exception as e:
            print(f"[scanner] scan error: {e}")
        time.sleep(POLL_INTERVAL)


def run_alert_bot():
    """Run alert_bot.py as a subprocess — avoids import name dependency."""
    while True:
        try:
            subprocess.run([sys.executable, "alert_bot.py"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[alert_bot] exited with code {e.returncode} — restarting in 30s")
        except Exception as e:
            print(f"[alert_bot] error: {e} — restarting in 30s")
        time.sleep(30)


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

    # Initialise DB before any thread touches it
    db.init_db()
    print("[run] Database initialised")

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
