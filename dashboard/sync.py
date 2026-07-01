"""
sync.py — pull a fresh snapshot of the collector's SQLite DB from the VPS.
=========================================================================
The database lives on the always-on VPS; this copies it down over SSH so the
local Streamlit app can read it. Prefers a clean `VACUUM INTO` snapshot (a
single consistent file); falls back to copying the db + WAL directly if sqlite3
isn't installed on the server. Uses your existing SSH key — no passwords.

  python sync.py root@1.2.3.4
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

REMOTE_DB = "/var/lib/pmfade/pmfade.db"
SNAP      = "/tmp/pmfade_snap.db"
LOCAL_DB  = Path(__file__).with_name("pmfade.db")

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def _clear_local_wal(local_db: Path):
    # a stale local WAL/SHM would be replayed onto a fresh snapshot and corrupt
    # the view — remove them before writing a new copy.
    for ext in ("-wal", "-shm"):
        p = Path(f"{local_db}{ext}")
        if p.exists():
            p.unlink()


def pull(server: str, local_db: Path = LOCAL_DB, timeout: int = 60) -> tuple[bool, str]:
    """Return (ok, message)."""
    if not server:
        return False, "No server set — enter it as user@host (e.g. root@1.2.3.4)."
    local_db = Path(local_db)

    try:
        _clear_local_wal(local_db)

        # 1) clean snapshot via VACUUM INTO
        remote = f"sqlite3 {REMOTE_DB} \"VACUUM INTO '{SNAP}'\""
        r = subprocess.run(["ssh", *_SSH_OPTS, server, remote],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            c = subprocess.run(["scp", *_SSH_OPTS, f"{server}:{SNAP}", str(local_db)],
                               capture_output=True, text=True, timeout=timeout)
            if c.returncode == 0:
                return True, "Synced a clean snapshot."
            return False, f"scp failed: {c.stderr.strip()[:200]}"

        # 2) fallback: raw copy of db (+ wal/shm best-effort)
        c = subprocess.run(["scp", *_SSH_OPTS, f"{server}:{REMOTE_DB}", str(local_db)],
                           capture_output=True, text=True, timeout=timeout)
        if c.returncode != 0:
            return False, f"scp failed: {c.stderr.strip()[:200]}"
        for ext in ("-wal", "-shm"):
            subprocess.run(["scp", *_SSH_OPTS, f"{server}:{REMOTE_DB}{ext}", f"{local_db}{ext}"],
                           capture_output=True, text=True, timeout=timeout)
        return True, "Synced (raw copy — install sqlite3 on the server for cleaner snapshots)."

    except subprocess.TimeoutExpired:
        return False, "Timed out reaching the server. Check the address and your connection."
    except FileNotFoundError:
        return False, "ssh/scp not found. Install the OpenSSH client (built into Windows 10+)."
    except Exception as e:
        return False, f"Sync error: {e}"


if __name__ == "__main__":
    ok, msg = pull(sys.argv[1] if len(sys.argv) > 1 else "")
    print(msg)
    sys.exit(0 if ok else 1)
