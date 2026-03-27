"""
sheets.py — Google Sheets export for raw alerts.

Uses raw HTTP requests to the Sheets API v4 — no google-api-python-client
dependency, works on any Python version Railway runs.

Auth: Service account JSON → manual JWT → Bearer token exchange.
      All done with stdlib (json, hmac) + requests.

Setup:
  1. Create a Google Cloud project → enable Sheets API
  2. Create a Service Account → download JSON key
  3. Share your Google Sheet with the service account email (Editor)
  4. Set Railway env vars:
       GOOGLE_SERVICE_ACCOUNT_JSON = <entire JSON key file, one line>
       GOOGLE_SHEET_ID             = <from sheet URL>

Column order (Raw Alerts sheet):
  A Timestamp  B Market Name  C URL  D Category  E Direction
  F Price Before  G Price After  H Change (pts)  I Volume 24h
  J Vol Multiple  K Liquidity  L Ambient Vol
"""

import os
import json
import time
import math
import hmac
import hashlib
import base64
import logging
import requests
from datetime import datetime, timezone, timedelta

log = logging.getLogger("scanner.sheets")

# NZT = UTC+13 (NZDT, Oct–Apr) — fixed offset avoids tzdata dependency
NZT_OFFSET = timedelta(hours=13)

# Cache the access token so we don't re-auth on every alert
_token_cache: dict = {"token": None, "expires_at": 0}


# ── JWT / OAuth ───────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _get_access_token(sa: dict) -> str | None:
    """
    Exchange service account credentials for a short-lived Bearer token.
    Caches the token until 60s before expiry.
    """
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    try:
        import json as _json

        # Build JWT header + claim
        header  = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        iat     = int(now)
        exp     = iat + 3600
        claims  = _b64url(json.dumps({
            "iss":   sa["client_email"],
            "scope": "https://www.googleapis.com/auth/spreadsheets",
            "aud":   "https://oauth2.googleapis.com/token",
            "iat":   iat,
            "exp":   exp,
        }).encode())

        signing_input = f"{header}.{claims}".encode()

        # Sign with RSA-SHA256 using the private key
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            private_key = serialization.load_pem_private_key(
                sa["private_key"].encode(), password=None
            )
            signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        except ImportError:
            log.error("[sheets] 'cryptography' package not installed — add to requirements.txt")
            return None

        jwt_token = f"{header}.{claims}.{_b64url(signature)}"

        # Exchange JWT for access token
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  jwt_token,
            },
            timeout=10,
        )
        r.raise_for_status()
        token_data = r.json()
        token = token_data["access_token"]

        _token_cache["token"]      = token
        _token_cache["expires_at"] = now + token_data.get("expires_in", 3600) - 60
        return token

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
) -> bool:
    """
    Append one row to the Raw Alerts sheet.
    Returns True on success, False on any failure. Never raises.
    """
    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            log.warning("[sheets] GOOGLE_SHEET_ID not set — skipping export.")
            return False

        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not sa_json:
            log.warning("[sheets] GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping export.")
            return False

        sa    = json.loads(sa_json)
        token = _get_access_token(sa)
        if not token:
            return False

        now_nzt   = datetime.now(timezone(NZT_OFFSET))
        timestamp = now_nzt.strftime("%Y-%m-%d %H:%M:%S NZT")

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
        ]

        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            f"/values/Raw%20Alerts!A:L:append"
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
                   ambient_vol: float | None) -> bool:
    price_after  = market.get("yes_price", 0)
    price_before = price_after + drop_pts
    return log_alert(
        market       = market,
        direction    = "DROP",
        price_before = price_before,
        price_after  = price_after,
        change_pts   = -abs(drop_pts),
        vol_spike    = vol_spike,
        ambient_vol  = ambient_vol,
    )


def log_spike_alert(market: dict, spike_pts: float,
                    prev_price: float, vol_spike: float,
                    ambient_vol: float | None) -> bool:
    price_after  = market.get("yes_price", 0)
    price_before = price_after - spike_pts
    return log_alert(
        market       = market,
        direction    = "SPIKE",
        price_before = price_before,
        price_after  = price_after,
        change_pts   = abs(spike_pts),
        vol_spike    = vol_spike,
        ambient_vol  = ambient_vol,
    )
