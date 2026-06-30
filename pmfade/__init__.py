"""pmfade — Polymarket strategy-portfolio signal + calibration harness."""
__version__ = "0.1.0"

# Load .env (repo root) on package import so EVERY entry point — run_portfolio.py
# AND `python -m pmfade.status` / `pmfade.calibrate` — sees the same config
# (notably PMFADE_DB). setdefault means real env vars (e.g. systemd's
# Environment=) always win over the file.
import os as _os
from pathlib import Path as _Path

_envf = _Path(__file__).resolve().parent.parent / ".env"
if _envf.exists():
    for _line in _envf.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip())
