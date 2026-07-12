"""
news_classify.py — input-change classification (handoff §6.3), key-optional.
============================================================================
The core discrimination question: did the news that moved this market change
one of its STRUCTURAL INPUTS (a signed deal, a scheduled vote, an actual
indictment = information — never fade), or is it sentiment (a rumor, an
outrage cycle = fade candidate)?

Implementation is a single Claude API call with a strict-JSON prompt —
raw HTTP via requests (no SDK; the collector stays requests+stdlib). It runs
ONLY when ANTHROPIC_API_KEY is set, ONLY at pending-fade decision time
(a few calls/day at most, fractions of a cent each on Haiku), and FAILS
OPEN: any error, missing key, or confidence below the floor degrades to the
existing operator-in-the-loop path. The classifier can suppress a fade on
high-confidence input_changed; it never upgrades one.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from . import config as C

log = logging.getLogger("pmfade.news_classify")

API_URL = "https://api.anthropic.com/v1/messages"


@dataclass
class Classification:
    input_changed: bool
    which_input: Optional[str]
    direction: Optional[str]
    confidence: float
    rationale: str


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


_PROMPT = """You are a discrimination gate in a prediction-market fade system.
A market's price moved sharply. Decide whether recent news changed a STRUCTURAL
INPUT of the market (information — the move should NOT be faded) or is
sentiment/rumor/noise (the move is a fade candidate).

Market question: {question}
Move: {direction} (price went {ref:.0f}c -> {price:.0f}c over ~24h)
Anchor family: {family}
Structural inputs the family model uses:
{inputs}

Recent headlines:
{headlines}

Rules:
- A signed deal, a scheduled/held vote, an actual indictment/charge, a death,
  an official announcement = input_changed true.
- Rumors of talks, "sources say", speculation, commentary, outrage cycles,
  polls/odds coverage = input_changed false.
- If headlines are unrelated to the market question, input_changed false with
  low confidence.

Respond with ONLY a JSON object, no prose, no code fences:
{{"input_changed": true|false, "which_input": "<name or null>",
  "direction": "toward_yes"|"toward_no"|null,
  "confidence": 0.0-1.0, "rationale": "<one sentence>"}}"""


def classify(question: str, direction: str, ref_price: float, price: float,
             family: Optional[str], inputs: Optional[dict],
             headlines: list[dict]) -> Optional[Classification]:
    """None => unavailable/failed (fail open to the operator path)."""
    if not available() or not headlines:
        return None
    prompt = _PROMPT.format(
        question=question, direction=direction, ref=ref_price, price=price,
        family=family or "(none matched)",
        inputs=("\n".join(f"- {k}: {v}" for k, v in (inputs or {}).items())
                or "- (no family model; general judgment)"),
        headlines="\n".join(f"- {h['title']} ({h.get('source','')})"
                            for h in headlines[:8]))
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": C.NEWS_CLASSIFIER_MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        d = json.loads(text)
        return Classification(
            input_changed=bool(d.get("input_changed")),
            which_input=d.get("which_input"),
            direction=d.get("direction"),
            confidence=float(d.get("confidence", 0.0)),
            rationale=str(d.get("rationale", ""))[:200])
    except Exception as e:
        log.warning("classifier failed (fail-open to operator): %s", e)
        return None
