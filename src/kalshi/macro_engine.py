"""Macro/geopolitics sentiment engine for CPI/GDP/Jobs bias adjustments.

Fetches data from FRED API, Truflation, and curated blog/RSS feeds.
Produces MacroSignal with quantified bias adjustments for economics bot.
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict

import requests

_log = logging.getLogger("macro_engine")


@dataclass
class MacroSignal:
    """Aggregated macro signal with CPI bias and confidence."""
    cpi_bias: float = 0.0          # +/- adjustment to CPI nowcast (pp)
    confidence: float = 0.0         # 0-1, how confident we are in the bias
    tips_breakeven: Optional[float] = None
    umich_expectations: Optional[float] = None
    gdpnow: Optional[float] = None
    truflation_cpi: Optional[float] = None
    sentiment_score: float = 0.0    # -1 to +1 from blog/RSS
    sources_available: int = 0
    sources_total: int = 6
    timestamp: str = ""


class FREDClient:
    """Fetch economic data from FRED API (free, no auth required)."""

    BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

    # FRED series IDs
    SERIES = {
        "tips_breakeven_10y": "T10YIE",       # 10-Year Breakeven Inflation Rate
        "tips_breakeven_5y": "T5YIE",         # 5-Year Breakeven Inflation Rate
        "umich_expectations": "MICH",          # UMich Inflation Expectations
        "gdpnow": "GDPNOW",                   # Atlanta Fed GDPNow
    }

    def __init__(self, api_key: str = ""):
        # FRED allows limited requests without a key, but key is recommended
        # For now, use the free tier (no key needed for basic series)
        self.api_key = api_key

    def _parse_observation(self, data: dict) -> Optional[float]:
        """Parse the latest observation value from a FRED API response."""
        observations = data.get("observations", [])
        if not observations:
            return None
        latest = observations[-1]
        value_str = latest.get("value", ".")
        if value_str == "." or not value_str:
            return None
        try:
            return float(value_str)
        except (ValueError, TypeError):
            return None
