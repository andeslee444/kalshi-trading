"""Correlation & Dependency Engine — Portfolio Risk Modeling

Models portfolio-level risk to prevent concentration blowups.
Maps market tickers to factor groups, computes correlations,
fits copulas for tail dependence, and clusters correlated markets.

Provides three checks for capital_allocator.py:
  Check #6: Cluster concentration limit
  Check #7: Marginal VaR threshold
  Check #8: Tail-risk Kelly reduction
"""

import json
import math
import re
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("correlation-engine")


# === Factor Group Definitions ===
# Each factor represents an underlying economic variable.
# Markets sharing a factor are assumed highly correlated.

FACTOR_PREFIXES = {
    # Economics — each release is a separate factor
    "KXCPI": "CPI",
    "KXCORECPI": "CORE_CPI",
    "KXGDP": "GDP",
    "KXJOBS": "JOBS",
    "KXPCE": "PCE",
    # Crypto — each coin is a separate factor
    "KXBTC": "BTC",
    "KXETH": "ETH",
    # Entertainment
    "KXALBUM": "ALBUM",
    "KXBOX": "BOX_OFFICE",
    "KXMOVIE": "BOX_OFFICE",
    "KXFILM": "BOX_OFFICE",
}

# Weather city → factor (grouped by correlated regions)
WEATHER_CITY_FACTORS = {
    "HOU": "WEATHER_SOUTH_TX",
    "AUS": "WEATHER_SOUTH_TX",
    "NY": "WEATHER_NE",
    "PHI": "WEATHER_NE",
    "MIA": "WEATHER_SE",
    "LAX": "WEATHER_W",
    "CHI": "WEATHER_MW",
    "DEN": "WEATHER_MT",
}

# A priori intra-factor correlation (same factor group)
INTRA_FACTOR_CORRELATION = 0.90

# A priori inter-factor correlations (between different factors)
# Only include pairs with known economic relationships.
INTER_FACTOR_CORRELATIONS = {
    ("CPI", "CORE_CPI"): 0.85,
    ("CPI", "PCE"): 0.80,
    ("CPI", "GDP"): 0.30,
    ("CPI", "JOBS"): 0.25,
    ("BTC", "ETH"): 0.75,
    ("WEATHER_SOUTH_TX", "WEATHER_SE"): 0.40,
    ("WEATHER_NE", "WEATHER_MW"): 0.35,
}

# Default: unrelated factors have 0 correlation
DEFAULT_INTER_FACTOR_CORRELATION = 0.0


@dataclass
class CorrelationConfig:
    """Configuration for the correlation engine."""
    cluster_max_fraction: float = 0.15       # max 15% of equity per cluster
    marginal_var_limit_fraction: float = 0.05  # max 5% portfolio VaR contribution
    tail_dep_kelly_threshold: float = 0.15   # tail dependence threshold
    tail_dep_kelly_cut: float = 0.25         # 25% Kelly reduction when above threshold
    var_confidence: float = 0.99             # VaR confidence level
    min_trades_for_empirical: int = 10       # min trades to use empirical correlation
    empirical_weight_cap: float = 0.50       # max weight for empirical vs a priori
    copula_df: int = 5                       # Student-t copula degrees of freedom


class CorrelationEngine:
    """Portfolio risk engine with factor-based correlation modeling."""

    def __init__(self, config: Optional[CorrelationConfig] = None,
                 state_path: Optional[str] = None,
                 logger: Optional[logging.Logger] = None):
        self.config = config or CorrelationConfig()
        self.state_path = state_path
        self.log = logger or log
        self._correlation_cache: Dict[Tuple[str, str], float] = {}
        self._tail_dep: Dict[Tuple[str, str], float] = {}
        self._clusters: Dict[str, List[str]] = {}
        self._cluster_risk: Dict[str, int] = {}  # cluster_name -> risk_cents
        self._portfolio_var: float = 0.0
        self._last_updated: str = ""

    def ticker_to_factor(self, ticker: str) -> str:
        """Map a market ticker to its factor group.

        Examples:
          KXCPI-26MAY-T20 -> CPI
          KXHIGHHOU-26MAR3-T86 -> WEATHER_SOUTH_TX
          KXBTC-26MAR3-T95000 -> BTC
          KXUNKNOWN-FOO -> KXUNKNOWN (prefix as fallback)
        """
        ticker_upper = ticker.upper()

        # Check weather tickers first (KXHIGH{CITY}-...)
        if ticker_upper.startswith("KXHIGH"):
            rest = ticker_upper[6:]  # strip "KXHIGH"
            # Extract city code (everything before the first hyphen)
            city_code = rest.split("-")[0] if "-" in rest else rest
            return WEATHER_CITY_FACTORS.get(city_code, f"WEATHER_{city_code}")

        # Check known prefixes (longest match first)
        for prefix in sorted(FACTOR_PREFIXES.keys(), key=len, reverse=True):
            if ticker_upper.startswith(prefix):
                return FACTOR_PREFIXES[prefix]

        # Fallback: use ticker prefix (everything before first hyphen)
        prefix = ticker_upper.split("-")[0] if "-" in ticker_upper else ticker_upper
        return prefix

    def get_factor_correlation(self, factor_a: str, factor_b: str) -> float:
        """Get the a priori correlation between two factor groups."""
        if factor_a == factor_b:
            return INTRA_FACTOR_CORRELATION

        # Check both orderings
        key = (factor_a, factor_b)
        rev_key = (factor_b, factor_a)
        if key in INTER_FACTOR_CORRELATIONS:
            return INTER_FACTOR_CORRELATIONS[key]
        if rev_key in INTER_FACTOR_CORRELATIONS:
            return INTER_FACTOR_CORRELATIONS[rev_key]

        return DEFAULT_INTER_FACTOR_CORRELATION

    def get_ticker_correlation(self, ticker_a: str, ticker_b: str) -> float:
        """Get correlation between two tickers via their factor groups."""
        fa = self.ticker_to_factor(ticker_a)
        fb = self.ticker_to_factor(ticker_b)
        return self.get_factor_correlation(fa, fb)
