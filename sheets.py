"""
sheets.py — Google Sheets export for raw alerts.

Appends one row per alert to the "Raw Alerts" sheet in your
tracking spreadsheet. Called from scanner.py after every
DROP or SPIKE alert fires.

Auth: Service account JSON stored as env var.
      No OAuth flow needed — works headlessly on Railway.

Setup:
  1. Create a Google Cloud project → enable Sheets API
  2. Create a Service Account → download JSON key
  3. Share your Google Sheet with the service account email
  4. Set env vars:
       GOOGLE_SERVICE_ACCOUNT_JSON = <paste entire JSON as one line>
       GOOGLE_SHEET_ID             = <from sheet URL>

Column order matches Raw Alerts sheet (set up by Code.gs):
  A  Timestamp       B  Market Name     C  URL
  D  Category        E  Direction       F  Price Before
  G  Price After     H  Change (pts)    I  Volume 24h
  J  Vol Multiple    K  Liquidity ($)   L  Ambient Vol
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("scanner.sheets")

_service = None   # lazy-initialised Google Sheets service

# NZT = UTC+13 (NZDT) or UTC+12 (NZST) — use fixed offset to avoid tzdata dependency
NZT_OFFSET = timedelta(hours=13)   # NZDT (daylight saving, Oct–Apr)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_service():
    """Lazy-init and cache the Sheets API service object."""
    global _service
    if _service is not None:
        return _service

    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — Sheets export disabled.")
        return None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_dict = json.loads(sa_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        log.info("Google Sheets service initialised.")
        return _service

    except ImportError:
        log.warning(
            "google-auth / google-api-python-client not installed. "
            "Add to requirements.txt: google-auth>=2.0.0 google-api-python-client>=2.0.0"
        )
        return None
    except Exception as e:
        log.error(f"Failed to initialise Sheets service: {e}")
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def log_alert(
    market: dict,
    direction: str,          # "DROP" or "SPIKE"
    price_before: float,
    price_after: float,
    change_pts: float,
    vol_spike: float,
    ambient_vol: float | None,
) -> bool:
    """
    Append one row to the Raw Alerts sheet.

    Returns True on success, False on any failure.
    Never raises — the scanner must not crash due to a Sheets error.
    """
    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            log.warning("GOOGLE_SHEET_ID not set — skipping Sheets export.")
            return False

        svc = _get_service()
        if svc is None:
            return False

        # Fixed UTC+13 offset avoids tzdata dependency on Railway
        now_nzt = datetime.now(timezone(NZT_OFFSET))
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

        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Raw Alerts!A:L",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

        log.info(f"[sheets] Logged {direction}: {market.get('question','')[:50]}")
        return True

    except Exception as e:
        log.error(f"[sheets] Failed to write row: {e}")
        return False


# ── Convenience wrappers matching scanner.py alert types ─────────────────────

def log_drop_alert(market: dict, drop_pts: float,
                   prev_price: float, vol_spike: float,
                   ambient_vol: float | None) -> bool:
    """Log a DROP alert. price_before > price_after."""
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
    """Log a SPIKE alert. price_after > price_before."""
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
