"""
sheets.py — Google Sheets export for raw alerts.

Uses raw HTTP + subprocess openssl for JWT signing.
Zero external dependencies beyond requests — no cryptography,
no google-api-python-client, works on any Python version.

Setup:
  1. Google Cloud → enable Sheets API → Service Account → download JSON key
  2. Share your sheet with the service account email (Editor)
  3. Railway env vars:
       GOOGLE_SERVICE_ACCOUNT_JSON = <entire JSON key, one line>
       GOOGLE_SHEET_ID             = <from sheet URL>
"""

import os
import json
import time
import base64
import hashlib
import logging
import tempfile
import subprocess
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("scanner.sheets")

NZT_OFFSET = timedelta(hours=13)   # NZDT (UTC+13, Oct–Apr)
_token_cache: dict = {"token": None, "expires_at": 0}


# ── JWT signing via openssl subprocess ───────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_with_openssl(private_key_pem: str, message: bytes) -> bytes | None:
    """
    RSA-SHA256 sign using the system openssl binary.
    Writes the key to a temp file, signs, then deletes immediately.
    openssl is always present on Railway (Linux/nixpacks).
    """
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False, prefix="sa_key_"
        ) as f:
            f.write(private_key_pem)
            tmp = f.name

        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", tmp],
            input=message,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.error(f"[sheets] openssl sign failed: {result.stderr.decode()}")
            return None
        return result.stdout

    except FileNotFoundError:
        log.error("[sheets] openssl not found — not available on this system")
        return None
    except Exception as e:
        log.error(f"[sheets] openssl signing error: {e}")
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _get_access_token(sa: dict) -> str | None:
    """Exchange service account creds for a Bearer token. Caches until expiry."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    try:
        iat = int(now)
        exp = iat + 3600

        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({
            "iss":   sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud":   "https://oauth2.googleapis.com/token",
            "iat":   iat,
            "exp":   exp,
        }).encode())

        signing_input = f"{header}.{claims}".encode()
        signature = _sign_with_openssl(sa["private_key"], signing_input)

        if signature is None:
            return None

        jwt_token = f"{header}.{claims}.{_b64url(signature)}"

        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  jwt_token,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        _token_cache["token"]      = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
        log.info("[sheets] Access token obtained successfully.")
        return _token_cache["token"]

    except Exception as e:
        log.error(f"[sheets] Failed to get access token: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def log_alert(
    market: dict,
    direction: str,
    price_before: float,
    price_after: float,
    change_pts: float,
    vol_spike: float,
    ambient_vol: float | None,
    quality_score: int = 0,
    quality_flags: list = None,
    prev_scan_price: float = None,
    book_snapshot: dict = None,
) -> bool:
    """Append one row to Raw Alerts sheet. Never raises."""
    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            log.warning("[sheets] GOOGLE_SHEET_ID not set — skipping.")
            return False

        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not sa_json:
            log.warning("[sheets] GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping.")
            return False

        sa    = json.loads(sa_json)
        token = _get_access_token(sa)
        if not token:
            return False

        timestamp = datetime.now(timezone(NZT_OFFSET)).strftime("%Y-%m-%d %H:%M:%S NZT")
        flags_str = ", ".join(quality_flags) if quality_flags else ""
        ob = book_snapshot or {}

        row = [
            timestamp,
            market.get("question", ""),
            market.get("url", ""),
            market.get("category", ""),
            direction,
            round(price_before, 2),
            round(price_after, 2),
            round(change_pts, 2),
            round(market.get("volume_24h", 0), 2),
            round(vol_spike, 2),
            round(market.get("liquidity", 0), 2),
            round(ambient_vol, 2) if ambient_vol is not None else "",
            quality_score,
            flags_str,
            round(prev_scan_price, 2) if prev_scan_price is not None else "",  # O
            # Order book snapshot (P, Q, R)
            round(ob.get("best_bid_cents"), 1) if ob.get("best_bid_cents") is not None else "",
            round(ob.get("best_ask_cents"), 1) if ob.get("best_ask_cents") is not None else "",
            round(ob.get("spread_pts"), 2) if ob.get("spread_pts") is not None else "",
        ]

        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            f"/values/Raw%20Alerts!A:R:append"
            f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        )
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"values": [row]},
            timeout=10,
        )
        r.raise_for_status()
        log.info(f"[sheets] Logged {direction}: {market.get('question','')[:50]}")
        return True

    except Exception as e:
        log.error(f"[sheets] Failed to write row: {e}")
        return False


# ── Convenience wrappers ──────────────────────────────────────────────────────

def log_drop_alert(market: dict, drop_pts: float,
                   prev_price: float, vol_spike: float,
                   ambient_vol: float | None,
                   quality_score: int = 0,
                   quality_flags: list = None,
                   prev_scan_price: float = None,
                   book_snapshot: dict = None) -> bool:
    price_after  = market.get("yes_price", 0)
    price_before = price_after + drop_pts
    return log_alert(market, "DROP", price_before, price_after,
                     -abs(drop_pts), vol_spike, ambient_vol,
                     quality_score, quality_flags, prev_scan_price,
                     book_snapshot)


def log_spike_alert(market: dict, spike_pts: float,
                    prev_price: float, vol_spike: float,
                    ambient_vol: float | None,
                    quality_score: int = 0,
                    quality_flags: list = None,
                    prev_scan_price: float = None,
                    book_snapshot: dict = None) -> bool:
    price_after  = market.get("yes_price", 0)
    price_before = price_after - spike_pts
    return log_alert(market, "SPIKE", price_before, price_after,
                     abs(spike_pts), vol_spike, ambient_vol,
                     quality_score, quality_flags, prev_scan_price,
                     book_snapshot)
