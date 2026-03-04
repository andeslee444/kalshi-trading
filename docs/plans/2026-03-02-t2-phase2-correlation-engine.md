# T2-Phase 2: Correlation & Dependency Layer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Model portfolio-level risk to prevent CPI-style concentration blowups (the economics bot autonomously placed $1,720 — 34% of equity — in two ~99% correlated CPI bets). Build a correlation engine that enforces cluster concentration limits, marginal VaR checks, and tail-risk Kelly reductions as new checks in the capital allocator.

**Architecture:** New module `src/kalshi/correlation_engine.py` maps every market ticker to a factor group (CPI, BTC, weather-by-region, etc.), computes pairwise correlations using a priori structure refined by empirical trade outcomes, fits a Student-t copula for tail dependence, and clusters markets hierarchically. The engine provides three new checks for `capital_allocator.py`: (6) cluster concentration limit, (7) marginal VaR threshold, (8) tail-risk Kelly reduction. State persists in `data/correlation-state.json`.

**Tech Stack:** Python 3, `scipy` (new dependency — t-distribution, optimization, hierarchical clustering), `math`, `statistics`, JSON persistence via existing `_atomic_write_json`.

**Branch:** `track2/quant-infrastructure` (from `main`)

---

### Task 1: Create Branch and Add scipy Dependency

**Files:**
- Modify: `requirements.txt`

**Step 1: Create the feature branch**

```bash
git checkout -b track2/quant-infrastructure main
```

**Step 2: Add scipy to requirements.txt**

Add `scipy>=1.11.0` to `requirements.txt`.

**Step 3: Install the dependency**

Run: `pip install scipy>=1.11.0`

**Step 4: Verify scipy imports**

Run: `python3 -c "import scipy; print(scipy.__version__)"`
Expected: Version 1.11+ prints.

**Step 5: Commit**

```bash
git add requirements.txt
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "chore: add scipy dependency for correlation engine (t-copula, clustering, VaR)"
```

---

### Task 2: Factor Structure and Market Mapping — Tests

**Files:**
- Create: `tests/test_correlation_engine.py`

**Context:** Every Kalshi market ticker maps to a "factor group" — the underlying economic variable that drives its outcome. CPI T2.0 and CPI T2.1 share the same factor (the May CPI print). BTC and ETH share a crypto factor. Weather markets in Houston and Austin share a South Texas weather factor. The factor mapping is the foundation for all correlation/clustering logic.

**Step 1: Write tests for factor mapping**

```python
"""Tests for correlation_engine.py — portfolio risk modeling."""

import json
import math
import os
import tempfile
import pytest
from unittest.mock import MagicMock


class TestFactorMapping:
    """Test ticker → factor group mapping."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine
        self.engine = CorrelationEngine()

    def test_cpi_tickers(self):
        assert self.engine.ticker_to_factor("KXCPI-26MAY-T20") == "CPI"
        assert self.engine.ticker_to_factor("KXCPI-26MAY-T21") == "CPI"

    def test_core_cpi_tickers(self):
        assert self.engine.ticker_to_factor("KXCORECPI-26JUN-T23") == "CORE_CPI"

    def test_gdp_tickers(self):
        assert self.engine.ticker_to_factor("KXGDP-26Q1-T20") == "GDP"

    def test_jobs_tickers(self):
        assert self.engine.ticker_to_factor("KXJOBS-26MAR-T200K") == "JOBS"

    def test_btc_tickers(self):
        assert self.engine.ticker_to_factor("KXBTC-26MAR3-T95000") == "BTC"

    def test_eth_tickers(self):
        assert self.engine.ticker_to_factor("KXETH-26MAR3-T3500") == "ETH"

    def test_weather_south_tx(self):
        assert self.engine.ticker_to_factor("KXHIGHHOU-26MAR3-T86") == "WEATHER_SOUTH_TX"
        assert self.engine.ticker_to_factor("KXHIGHAUS-26MAR3-T75") == "WEATHER_SOUTH_TX"

    def test_weather_northeast(self):
        assert self.engine.ticker_to_factor("KXHIGHNY-26MAR3-T55") == "WEATHER_NE"
        assert self.engine.ticker_to_factor("KXHIGHPHI-26MAR3-T50") == "WEATHER_NE"

    def test_weather_southeast(self):
        assert self.engine.ticker_to_factor("KXHIGHMIA-26MAR3-T85") == "WEATHER_SE"

    def test_weather_west(self):
        assert self.engine.ticker_to_factor("KXHIGHLAX-26MAR3-T75") == "WEATHER_W"

    def test_weather_midwest(self):
        assert self.engine.ticker_to_factor("KXHIGHCHI-26MAR3-T50") == "WEATHER_MW"

    def test_weather_mountain(self):
        assert self.engine.ticker_to_factor("KXHIGHDEN-26MAR3-T60") == "WEATHER_MT"

    def test_album_tickers(self):
        assert self.engine.ticker_to_factor("KXALBUM-ARTIST-T100K") == "ALBUM"

    def test_boxoffice_tickers(self):
        assert self.engine.ticker_to_factor("KXBOX-MOVIE-T50M") == "BOX_OFFICE"
        assert self.engine.ticker_to_factor("KXMOVIE-FILM-T100M") == "BOX_OFFICE"

    def test_unknown_ticker_returns_ticker_prefix(self):
        """Unknown tickers should return the prefix as a unique factor."""
        factor = self.engine.ticker_to_factor("KXNEWMARKET-26MAR-T50")
        assert factor == "KXNEWMARKET"

    def test_same_factor_means_correlated(self):
        """Two CPI tickers should have the same factor (= high correlation)."""
        f1 = self.engine.ticker_to_factor("KXCPI-26MAY-T20")
        f2 = self.engine.ticker_to_factor("KXCPI-26MAY-T21")
        assert f1 == f2

    def test_different_factor_means_independent(self):
        """CPI and BTC should have different factors."""
        f1 = self.engine.ticker_to_factor("KXCPI-26MAY-T20")
        f2 = self.engine.ticker_to_factor("KXBTC-26MAR3-T95000")
        assert f1 != f2
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestFactorMapping -v`
Expected: FAIL — `correlation_engine` module does not exist yet.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add factor mapping tests for correlation engine"
```

---

### Task 3: Factor Structure and Market Mapping — Implementation

**Files:**
- Create: `src/kalshi/correlation_engine.py`

**Step 1: Implement the factor mapping**

```python
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
    "PHIL": "WEATHER_NE",
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
          KXCPI-26MAY-T20 → CPI
          KXHIGHHOU-26MAR3-T86 → WEATHER_SOUTH_TX
          KXBTC-26MAR3-T95000 → BTC
          KXUNKNOWN-FOO → KXUNKNOWN (prefix as fallback)
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
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestFactorMapping -v`
Expected: All 19 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add factor mapping for correlation engine

Maps market tickers to factor groups (CPI, BTC, weather-by-region, etc.)
with a priori intra-factor correlation of 0.90 and known cross-factor
correlations (CPI/CORE_CPI=0.85, BTC/ETH=0.75, etc.)."
```

---

### Task 4: Cluster Risk Tracking — Tests

**Files:**
- Modify: `tests/test_correlation_engine.py`

**Context:** The cluster risk tracker maintains running totals of risk (in cents) per factor cluster. When a trade is recorded, risk is added to the cluster. The `check_cluster_limit` method compares accumulated cluster risk against `cluster_max_fraction * available_balance`. This is the core mechanism that prevents CPI-style concentration.

**Step 1: Write cluster risk tests**

```python
class TestClusterRisk:
    """Test cluster risk tracking and concentration limits."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(cluster_max_fraction=0.15))

    def test_record_trade_adds_to_cluster(self):
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        cluster_risk = self.engine.get_cluster_risk("CPI")
        assert cluster_risk == 50000

    def test_same_cluster_accumulates(self):
        """Two CPI trades should accumulate in the same cluster."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.record_trade("KXCPI-26MAY-T21", risk_cents=30000)
        assert self.engine.get_cluster_risk("CPI") == 80000

    def test_different_clusters_independent(self):
        """CPI and BTC trades should be in separate clusters."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.record_trade("KXBTC-26MAR3-T95000", risk_cents=20000)
        assert self.engine.get_cluster_risk("CPI") == 50000
        assert self.engine.get_cluster_risk("BTC") == 20000

    def test_check_cluster_limit_allows(self):
        """Under limit: $500 CPI risk with $5000 balance → 10% < 15% limit."""
        allowed, reason = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T22", proposed_risk_cents=10000, available_balance_cents=500000
        )
        assert allowed

    def test_check_cluster_limit_blocks(self):
        """Over limit: $800 CPI risk with $5000 balance → 16% > 15% limit."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=70000)
        allowed, reason = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=10000, available_balance_cents=500000
        )
        assert not allowed
        assert "cluster" in reason.lower()

    def test_cluster_limit_prevents_cpi_concentration(self):
        """The CPI blowup scenario: $1720 in CPI on $5090 balance = 33.8%.
        Should have been blocked after ~$763 (15% of $5090)."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=76000)
        allowed, _ = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=1000, available_balance_cents=509000
        )
        assert not allowed

    def test_correlated_factors_share_cluster(self):
        """CPI and CORE_CPI should share risk since they are correlated > 0.70."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=40000)
        self.engine.record_trade("KXCORECPI-26JUN-T23", risk_cents=40000)
        # If combined exposure is checked, 80k on 500k = 16% > 15%
        allowed, _ = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=1000, available_balance_cents=500000
        )
        assert not allowed

    def test_weather_region_clusters(self):
        """Houston and Austin weather should share WEATHER_SOUTH_TX cluster."""
        self.engine.record_trade("KXHIGHHOU-26MAR3-T86", risk_cents=30000)
        self.engine.record_trade("KXHIGHAUS-26MAR3-T75", risk_cents=30000)
        assert self.engine.get_cluster_risk("WEATHER_SOUTH_TX") == 60000

    def test_reset_daily(self):
        """Daily reset should clear all cluster risk."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.reset_daily()
        assert self.engine.get_cluster_risk("CPI") == 0
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestClusterRisk -v`
Expected: FAIL — `record_trade`, `get_cluster_risk`, `check_cluster_limit` not yet implemented.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add cluster risk tracking and concentration limit tests"
```

---

### Task 5: Cluster Risk Tracking — Implementation

**Files:**
- Modify: `src/kalshi/correlation_engine.py`

**Step 1: Add cluster risk methods to CorrelationEngine**

Add after the `get_ticker_correlation` method:

```python
    def _get_super_cluster(self, factor: str) -> List[str]:
        """Get all factors that should be grouped with this one due to high correlation.

        Factors with inter-factor correlation >= 0.70 are merged into a "super cluster"
        for concentration limit purposes. This prevents gaming the limit by splitting
        risk across CPI and CORE_CPI (which are 85% correlated).
        """
        MERGE_THRESHOLD = 0.70
        cluster = [factor]
        for (fa, fb), corr in INTER_FACTOR_CORRELATIONS.items():
            if corr >= MERGE_THRESHOLD:
                if fa == factor and fb not in cluster:
                    cluster.append(fb)
                elif fb == factor and fa not in cluster:
                    cluster.append(fa)
        return sorted(cluster)

    def _cluster_key(self, factor: str) -> str:
        """Get the canonical cluster key for a factor (handles super-clusters)."""
        members = self._get_super_cluster(factor)
        return "+".join(members)  # e.g., "CPI+CORE_CPI+PCE"

    def record_trade(self, ticker: str, risk_cents: int) -> None:
        """Record risk from a trade into the appropriate cluster."""
        factor = self.ticker_to_factor(ticker)
        key = self._cluster_key(factor)
        self._cluster_risk[key] = self._cluster_risk.get(key, 0) + risk_cents

    def get_cluster_risk(self, factor: str) -> int:
        """Get total risk in the cluster containing this factor."""
        key = self._cluster_key(factor)
        return self._cluster_risk.get(key, 0)

    def check_cluster_limit(self, ticker: str, proposed_risk_cents: int,
                            available_balance_cents: int) -> Tuple[bool, str]:
        """Check #6: Cluster concentration limit.

        Returns (allowed, reason). Blocks if adding this trade would push
        the cluster above cluster_max_fraction of available balance.
        """
        factor = self.ticker_to_factor(ticker)
        key = self._cluster_key(factor)
        current_risk = self._cluster_risk.get(key, 0)
        max_cluster_risk = int(available_balance_cents * self.config.cluster_max_fraction)
        projected = current_risk + proposed_risk_cents

        if projected > max_cluster_risk:
            return (False, f"cluster {key} would reach {projected/100:.0f} "
                          f"(limit ${max_cluster_risk/100:.0f} = "
                          f"{self.config.cluster_max_fraction*100:.0f}% of ${available_balance_cents/100:.0f})")

        return (True, "")

    def reset_daily(self) -> None:
        """Reset daily risk tracking."""
        self._cluster_risk.clear()
        self._portfolio_var = 0.0
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestClusterRisk -v`
Expected: All 10 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add cluster risk tracking with super-cluster merging

Factors with >=70% correlation are merged into super-clusters for
concentration limits. CPI+CORE_CPI+PCE share a cluster. This directly
prevents the $1720 CPI concentration blowup (34% → capped at 15%)."
```

---

### Task 6: Portfolio VaR Computation — Tests

**Files:**
- Modify: `tests/test_correlation_engine.py`

**Context:** Portfolio VaR measures the maximum loss at a confidence level (99%) over one day. For a portfolio of binary contracts, the VaR computation uses the factor correlation matrix to model joint loss scenarios. This is simpler than continuous-asset VaR because positions are bounded (max loss = cost basis).

**Step 1: Write VaR tests**

```python
class TestPortfolioVaR:
    """Test portfolio VaR computation."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(var_confidence=0.99))

    def test_single_position_var(self):
        """VaR of a single position = its max loss * loss probability."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.10},
        ]
        var = self.engine.compute_portfolio_var(positions)
        assert var > 0
        assert var <= 50000  # VaR cannot exceed max loss

    def test_uncorrelated_positions_diversify(self):
        """Two uncorrelated positions should have lower VaR than sum."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var = self.engine.compute_portfolio_var(positions)
        # VaR should be less than 60000 (sum) due to diversification
        assert var < 60000

    def test_correlated_positions_no_diversification(self):
        """Two perfectly correlated positions: VaR ~ sum of individual VaRs."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXCPI-26MAY-T21", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var_correlated = self.engine.compute_portfolio_var(positions)

        uncorr_positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var_uncorr = self.engine.compute_portfolio_var(uncorr_positions)

        # Correlated VaR should be higher than uncorrelated VaR
        assert var_correlated > var_uncorr

    def test_empty_portfolio_var_zero(self):
        var = self.engine.compute_portfolio_var([])
        assert var == 0.0

    def test_var_increases_with_position_size(self):
        """Doubling position size should increase VaR."""
        pos_small = [{"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 10000, "loss_prob": 0.30}]
        pos_large = [{"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 20000, "loss_prob": 0.30}]
        assert self.engine.compute_portfolio_var(pos_large) > self.engine.compute_portfolio_var(pos_small)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestPortfolioVaR -v`
Expected: FAIL — `compute_portfolio_var` not yet implemented.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add portfolio VaR computation tests"
```

---

### Task 7: Portfolio VaR Computation — Implementation

**Files:**
- Modify: `src/kalshi/correlation_engine.py`

**Context:** For binary contracts, VaR computation can use a variance-covariance approach:
1. Each position has a potential loss = risk_cents and a probability of loss = loss_prob
2. The portfolio variance = sum of all pairwise covariances
3. VaR at confidence α = z_α * sqrt(portfolio_variance)

This is simpler than full Monte Carlo and gives a reasonable approximation for our use case.

**Step 1: Add VaR computation**

Add to `CorrelationEngine` class:

```python
    def compute_portfolio_var(self, positions: List[Dict], confidence: Optional[float] = None) -> float:
        """Compute portfolio Value-at-Risk using variance-covariance method.

        Each position dict has:
          - ticker: market ticker
          - risk_cents: maximum loss in cents (cost basis)
          - loss_prob: probability of losing (1 - model probability of winning)

        For binary contracts:
          - Expected loss per position = risk * loss_prob
          - Variance per position = risk^2 * loss_prob * (1 - loss_prob)
          - Covariance(i,j) = corr(i,j) * std(i) * std(j)

        Returns VaR in cents (positive number = potential loss).
        """
        if not positions:
            return 0.0

        conf = confidence or self.config.var_confidence

        n = len(positions)
        # Compute standard deviation of loss for each position
        stds = []
        for pos in positions:
            risk = pos["risk_cents"]
            p_loss = pos["loss_prob"]
            # Bernoulli variance
            std = risk * math.sqrt(p_loss * (1 - p_loss))
            stds.append(std)

        # Build variance-covariance sum
        portfolio_variance = 0.0
        for i in range(n):
            for j in range(n):
                corr = self.get_ticker_correlation(
                    positions[i]["ticker"], positions[j]["ticker"]
                )
                portfolio_variance += corr * stds[i] * stds[j]

        portfolio_std = math.sqrt(max(0.0, portfolio_variance))

        # z-score for confidence level using inverse error function
        # For 99%: z ≈ 2.326
        from scipy.stats import norm
        z = norm.ppf(conf)

        var = z * portfolio_std
        self._portfolio_var = var
        return var
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestPortfolioVaR -v`
Expected: All 5 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add portfolio VaR computation using variance-covariance method

Uses factor-based correlation matrix to compute joint loss distribution.
Bernoulli variance for binary contracts, z-score scaling at 99% confidence."
```

---

### Task 8: Marginal VaR Check — Tests

**Files:**
- Modify: `tests/test_correlation_engine.py`

**Context:** Marginal VaR measures how much a new position would increase portfolio VaR. If adding a position increases VaR by more than `marginal_var_limit_fraction * available_balance`, the trade is blocked.

**Step 1: Write marginal VaR tests**

```python
class TestMarginalVaR:
    """Test marginal VaR threshold check."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(
            marginal_var_limit_fraction=0.05
        ))

    def test_first_trade_allowed(self):
        """First trade should always be allowed (marginal VaR from 0)."""
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXBTC-26MAR3-T95000",
            proposed_risk_cents=5000,
            loss_prob=0.30,
            current_positions=[],
            available_balance_cents=500000,
        )
        assert allowed

    def test_large_correlated_trade_blocked(self):
        """Adding a large correlated position should be blocked."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXCPI-26MAY-T21",
            proposed_risk_cents=50000,
            loss_prob=0.20,
            current_positions=existing,
            available_balance_cents=500000,
        )
        # Marginal VaR of another highly correlated CPI position should be large
        # Whether it's blocked depends on the specific numbers
        # At 5% of 500000 = 25000 limit, adding 50000 correlated risk may exceed
        assert not allowed

    def test_small_uncorrelated_trade_allowed(self):
        """A small uncorrelated position should be allowed."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXBTC-26MAR3-T95000",
            proposed_risk_cents=5000,
            loss_prob=0.30,
            current_positions=existing,
            available_balance_cents=500000,
        )
        assert allowed
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestMarginalVaR -v`
Expected: FAIL — `check_marginal_var` not yet implemented.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add marginal VaR check tests"
```

---

### Task 9: Marginal VaR Check — Implementation

**Files:**
- Modify: `src/kalshi/correlation_engine.py`

**Step 1: Implement marginal VaR check**

Add to `CorrelationEngine` class:

```python
    def check_marginal_var(self, ticker: str, proposed_risk_cents: int,
                           loss_prob: float, current_positions: List[Dict],
                           available_balance_cents: int) -> Tuple[bool, str]:
        """Check #7: Marginal VaR threshold.

        Computes VaR before and after adding the proposed position.
        Blocks if the marginal increase exceeds marginal_var_limit_fraction * balance.
        """
        var_before = self.compute_portfolio_var(current_positions)

        new_position = {
            "ticker": ticker,
            "risk_cents": proposed_risk_cents,
            "loss_prob": loss_prob,
        }
        var_after = self.compute_portfolio_var(current_positions + [new_position])

        marginal_var = var_after - var_before
        max_marginal = int(available_balance_cents * self.config.marginal_var_limit_fraction)

        if marginal_var > max_marginal:
            return (False, f"marginal VaR ${marginal_var/100:.0f} exceeds limit "
                          f"${max_marginal/100:.0f} ({self.config.marginal_var_limit_fraction*100:.0f}% of balance)")

        return (True, "")
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestMarginalVaR -v`
Expected: All 3 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add marginal VaR check for capital allocator

Computes incremental VaR from adding a new position. Blocks trades
where marginal VaR exceeds 5% of available balance."
```

---

### Task 10: Tail Dependence and Kelly Reduction — Tests

**Files:**
- Modify: `tests/test_correlation_engine.py`

**Context:** Tail dependence measures the probability that two assets both experience extreme losses simultaneously. A high tail dependence (> 0.15) between a new position and existing portfolio implies the portfolio is fragile in crisis scenarios. In this case, we reduce Kelly sizing by 25%.

For a bivariate Student-t copula with correlation ρ and ν degrees of freedom:
  λ = 2 * t_{ν+1}(-sqrt((ν+1)(1-ρ)/(1+ρ)))

where t_{ν+1} is the Student-t CDF with ν+1 degrees of freedom.

**Step 1: Write tail dependence tests**

```python
class TestTailDependence:
    """Test tail dependence computation and Kelly reduction."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(
            tail_dep_kelly_threshold=0.15,
            tail_dep_kelly_cut=0.25,
            copula_df=5,
        ))

    def test_high_correlation_high_tail_dep(self):
        """Intra-factor correlation (0.90) should have high tail dependence."""
        td = self.engine.compute_tail_dependence("CPI", "CPI")
        assert td > 0.15  # Should trigger Kelly reduction

    def test_zero_correlation_zero_tail_dep(self):
        """Uncorrelated factors should have ~0 tail dependence."""
        td = self.engine.compute_tail_dependence("CPI", "BTC")
        assert td < 0.05

    def test_moderate_correlation_moderate_tail_dep(self):
        """BTC/ETH at 0.75 correlation should have moderate tail dependence."""
        td = self.engine.compute_tail_dependence("BTC", "ETH")
        assert 0.05 < td < 0.50

    def test_kelly_multiplier_no_existing_positions(self):
        """No existing positions → full Kelly (multiplier = 1.0)."""
        mult = self.engine.get_tail_risk_multiplier("KXBTC-26MAR3-T95000", [])
        assert mult == 1.0

    def test_kelly_multiplier_with_correlated_position(self):
        """Adding to a factor with high tail dep → 75% Kelly."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        mult = self.engine.get_tail_risk_multiplier("KXCPI-26MAY-T21", existing)
        assert mult == pytest.approx(1.0 - 0.25, rel=0.01)  # 0.75

    def test_kelly_multiplier_with_uncorrelated_position(self):
        """Adding to uncorrelated factor → full Kelly."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        mult = self.engine.get_tail_risk_multiplier("KXBTC-26MAR3-T95000", existing)
        assert mult == 1.0

    def test_tail_dep_formula_known_values(self):
        """Verify tail dependence formula against known analytical values.

        For ν=5, ρ=0.9:
        λ = 2 * t_6(-sqrt(6 * 0.1 / 1.9)) ≈ 2 * t_6(-0.562) ≈ 2 * 0.297 ≈ 0.594
        (approximate — depends on exact t-distribution value)
        """
        td = self.engine.compute_tail_dependence("CPI", "CPI")  # ρ=0.90
        assert td > 0.30  # Known to be substantial for high corr + low df
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestTailDependence -v`
Expected: FAIL — methods not yet implemented.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add tail dependence and Kelly reduction tests"
```

---

### Task 11: Tail Dependence and Kelly Reduction — Implementation

**Files:**
- Modify: `src/kalshi/correlation_engine.py`

**Step 1: Implement tail dependence computation**

Add to `CorrelationEngine` class:

```python
    def compute_tail_dependence(self, factor_a: str, factor_b: str) -> float:
        """Compute lower tail dependence coefficient using Student-t copula.

        For a bivariate t-copula with correlation ρ and ν degrees of freedom:
          λ = 2 * t_{ν+1}(-sqrt((ν+1)(1-ρ)/(1+ρ)))

        where t_{ν+1}(x) is the Student-t CDF with ν+1 degrees of freedom.

        Returns λ ∈ [0, 1]. Higher = more tail dependence = more crisis correlation.
        """
        rho = self.get_factor_correlation(factor_a, factor_b)

        if abs(rho) < 1e-6:
            return 0.0  # No correlation → no tail dependence

        nu = self.config.copula_df

        from scipy.stats import t as t_dist

        arg = -math.sqrt((nu + 1) * (1 - rho) / (1 + rho))
        lam = 2.0 * t_dist.cdf(arg, df=nu + 1)

        return max(0.0, min(1.0, lam))

    def get_tail_risk_multiplier(self, ticker: str,
                                  current_positions: List[Dict]) -> float:
        """Check #8: Tail-risk Kelly reduction.

        Returns a multiplier in [0.75, 1.0] to apply to Kelly fraction.
        If any existing position shares high tail dependence with the proposed
        ticker, reduce Kelly by tail_dep_kelly_cut (default 25%).
        """
        if not current_positions:
            return 1.0

        new_factor = self.ticker_to_factor(ticker)

        max_tail_dep = 0.0
        for pos in current_positions:
            existing_factor = self.ticker_to_factor(pos["ticker"])
            td = self.compute_tail_dependence(new_factor, existing_factor)
            max_tail_dep = max(max_tail_dep, td)

        if max_tail_dep > self.config.tail_dep_kelly_threshold:
            return 1.0 - self.config.tail_dep_kelly_cut  # 0.75 by default

        return 1.0
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestTailDependence -v`
Expected: All 7 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Student-t copula tail dependence and Kelly reduction

Uses bivariate t-copula formula λ = 2*t_{ν+1}(-√((ν+1)(1-ρ)/(1+ρ))).
When tail dependence > 0.15 between new trade and existing positions,
Kelly fraction is reduced by 25%."
```

---

### Task 12: State Persistence — Tests

**Files:**
- Modify: `tests/test_correlation_engine.py`

**Step 1: Write state persistence tests**

```python
class TestStatePersistence:
    """Test save/load of correlation engine state."""

    def test_save_and_load_cluster_risk(self):
        from correlation_engine import CorrelationEngine
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = f.name

        engine1 = CorrelationEngine(state_path=state_path)
        engine1.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        engine1.record_trade("KXBTC-26MAR3-T95000", risk_cents=20000)
        engine1.save_state()

        engine2 = CorrelationEngine(state_path=state_path)
        engine2.load_state()
        assert engine2.get_cluster_risk("CPI") == 50000
        assert engine2.get_cluster_risk("BTC") == 20000

        os.unlink(state_path)

    def test_load_missing_file_is_noop(self):
        from correlation_engine import CorrelationEngine
        engine = CorrelationEngine(state_path="/tmp/nonexistent_corr.json")
        engine.load_state()  # Should not raise
        assert engine.get_cluster_risk("CPI") == 0

    def test_save_without_path_is_noop(self):
        from correlation_engine import CorrelationEngine
        engine = CorrelationEngine(state_path=None)
        engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        engine.save_state()  # Should not raise
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_correlation_engine.py::TestStatePersistence -v`
Expected: FAIL — `save_state`, `load_state` not yet implemented.

**Step 3: Commit**

```bash
git add tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add state persistence tests for correlation engine"
```

---

### Task 13: State Persistence — Implementation

**Files:**
- Modify: `src/kalshi/correlation_engine.py`

**Step 1: Implement save/load**

Add to `CorrelationEngine` class:

```python
    def save_state(self) -> None:
        """Persist correlation state to disk."""
        if not self.state_path:
            return

        state = {
            "cluster_risk": self._cluster_risk,
            "portfolio_var": self._portfolio_var,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # Use atomic write to prevent corruption
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, self.state_path)

    def load_state(self) -> None:
        """Load correlation state from disk."""
        if not self.state_path:
            return

        try:
            with open(self.state_path, "r") as f:
                state = json.load(f)
            self._cluster_risk = state.get("cluster_risk", {})
            self._portfolio_var = state.get("portfolio_var", 0.0)
            self._last_updated = state.get("last_updated", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # Start fresh
```

**Step 2: Run tests**

Run: `pytest tests/test_correlation_engine.py::TestStatePersistence -v`
Expected: All 3 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add state persistence for correlation engine

Atomic JSON write to data/correlation-state.json with cluster risk,
portfolio VaR, and timestamp."
```

---

### Task 14: Capital Allocator Integration — Tests

**Files:**
- Modify: `tests/test_allocator.py`

**Context:** The allocator needs three new checks after the existing check #5 (per-ticker concentration). These delegate to the correlation engine:
- Check #6: Cluster concentration (via `check_cluster_limit`)
- Check #7: Marginal VaR (via `check_marginal_var`)
- Check #8: Tail risk Kelly reduction (via `get_tail_risk_multiplier`)

Check #8 doesn't block trades — it reduces the Kelly fraction. This is applied as a multiplier to the bankroll returned in `BudgetResponse`.

**Step 1: Write allocator integration tests**

Add to `tests/test_allocator.py`:

```python
class TestCorrelationIntegration:
    """Test capital allocator integration with correlation engine."""

    def _make_allocator(self, balance=500000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        mock_client.get.return_value = {"market_positions": []}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_cluster_limit_blocks_concentration(self):
        """Allocator should block trades that exceed cluster concentration limit."""
        alloc = self._make_allocator(balance=500000)
        # Record enough CPI trades to hit 15% cluster limit ($75000)
        for i in range(8):
            alloc.record_trade("source-monitor", f"KXCPI-26MAY-T2{i}", 10000, edge=0.10)
        # 80000 > 75000 (15% of 500000) — next CPI trade should be blocked
        result = alloc.request_budget("economics", "KXCPI-26MAY-T25", edge=0.10, confidence=0.90)
        assert not result.approved
        assert "cluster" in result.reason.lower()

    def test_uncorrelated_trade_allowed_despite_cluster_risk(self):
        """BTC trade should be allowed even if CPI cluster is full."""
        alloc = self._make_allocator(balance=500000)
        for i in range(8):
            alloc.record_trade("economics", f"KXCPI-26MAY-T2{i}", 10000, edge=0.10)
        result = alloc.request_budget("crypto", "KXBTC-26MAR3-T95000", edge=0.10, confidence=0.80)
        assert result.approved

    def test_tail_risk_reduces_bankroll(self):
        """When tail dependence is high, bankroll for Kelly should be reduced."""
        alloc = self._make_allocator(balance=500000)
        alloc.record_trade("economics", "KXCPI-26MAY-T20", 20000, edge=0.10)
        result = alloc.request_budget("economics", "KXCPI-26MAY-T21", edge=0.10, confidence=0.85)
        if result.approved:
            # Bankroll should be reduced by tail risk multiplier
            # Full bankroll = 500000, with 25% cut = 375000
            assert result.bankroll_cents < 500000
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_allocator.py::TestCorrelationIntegration -v`
Expected: FAIL — allocator doesn't have correlation checks yet.

**Step 3: Commit**

```bash
git add tests/test_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add correlation engine integration tests for capital allocator"
```

---

### Task 15: Capital Allocator Integration — Implementation

**Files:**
- Modify: `src/kalshi/capital_allocator.py`

**Context:** Add three new checks to `_request_budget_inner()` after the existing check #5c (region exposure). The correlation engine instance is created in `PortfolioAllocator.__init__()` and shares the same state directory.

**Step 1: Import and initialize correlation engine**

At the top of `capital_allocator.py`, add:

```python
from correlation_engine import CorrelationEngine, CorrelationConfig
```

In `PortfolioAllocator.__init__()`, after the existing initialization:

```python
        # Correlation engine for portfolio risk checks
        corr_state = str(Path(self._state_path).parent / "correlation-state.json") if self._state_path else None
        self._correlation_engine = CorrelationEngine(state_path=corr_state, logger=self.log)
        self._correlation_engine.load_state()
```

**Step 2: Add checks to `_request_budget_inner`**

After check #5c (region exposure, around line 540), before the constraints dict (line 542), add:

```python
        # 5d. Cluster concentration check (correlation engine)
        cluster_ok, cluster_reason = self._correlation_engine.check_cluster_limit(
            ticker, bot_max_cost_cents, available_balance
        )
        if not cluster_ok:
            return BudgetResponse(False, reason=cluster_reason)

        # 5e. Marginal VaR check (requires current positions)
        current_positions = self._get_positions_for_var()
        var_ok, var_reason = self._correlation_engine.check_marginal_var(
            ticker, bot_max_cost_cents, loss_prob=max(0.01, 1.0 - confidence),
            current_positions=current_positions,
            available_balance_cents=available_balance,
        )
        if not var_ok:
            return BudgetResponse(False, reason=var_reason)
```

After the allocation computation (after line 576), before the final return, add tail risk adjustment:

```python
        # 8. Tail-risk Kelly reduction
        tail_mult = self._correlation_engine.get_tail_risk_multiplier(ticker, current_positions)
        if tail_mult < 1.0:
            bankroll = int(bankroll * tail_mult)
            self.log.info("Tail risk reduction: %s mult=%.2f -> bankroll $%.2f", ticker, tail_mult, bankroll / 100)
```

**Step 3: Add record_trade integration**

In the existing `record_trade()` method, add:

```python
        # Update correlation engine cluster risk
        self._correlation_engine.record_trade(ticker, risk_cents)
        self._correlation_engine.save_state()
```

**Step 4: Add helper for VaR position data**

Add to `PortfolioAllocator`:

```python
    def _get_positions_for_var(self) -> list:
        """Get current positions formatted for VaR computation."""
        positions = []
        for ticker, info in self._traded_tickers.items():
            risk = info.get("risk_cents", 0) if isinstance(info, dict) else 0
            edge = info.get("edge", 0.10) if isinstance(info, dict) else 0.10
            loss_prob = max(0.01, 1.0 - (0.5 + edge))  # rough estimate from edge
            positions.append({
                "ticker": ticker,
                "risk_cents": risk,
                "loss_prob": loss_prob,
            })
        return positions
```

**Step 5: Add reset_daily integration**

In the existing daily reset logic, add:

```python
        self._correlation_engine.reset_daily()
```

**Step 6: Run tests**

Run: `pytest tests/test_allocator.py -v`
Expected: All tests pass (existing + new correlation integration tests).

**Step 7: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

**Step 8: Commit**

```bash
git add src/kalshi/capital_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate correlation engine into capital allocator

Add three new checks to budget allocation:
- Check #6: Cluster concentration limit (15% per correlated group)
- Check #7: Marginal VaR threshold (5% of balance)
- Check #8: Tail-risk Kelly reduction (25% cut when tail dep > 0.15)

CPI+CORE_CPI+PCE share a super-cluster — the $1720 concentration
blowup would now be blocked after $763."
```

---

### Task 16: Configuration

**Files:**
- Modify: `config/bots-config.json`

**Step 1: Add correlation engine config**

Add a new top-level section to `config/bots-config.json`:

```json
  "correlation": {
    "clusterMaxFraction": 0.15,
    "marginalVarLimitFraction": 0.05,
    "tailDepKellyThreshold": 0.15,
    "tailDepKellyCut": 0.25,
    "varConfidence": 0.99,
    "copulaDf": 5,
    "minTradesForEmpirical": 10,
    "empiricalWeightCap": 0.50
  }
```

**Step 2: Load config in correlation engine initialization**

In `capital_allocator.py`, update the correlation engine initialization to read from config:

```python
        corr_config_raw = self._load_config().get("correlation", {})
        corr_config = CorrelationConfig(
            cluster_max_fraction=corr_config_raw.get("clusterMaxFraction", 0.15),
            marginal_var_limit_fraction=corr_config_raw.get("marginalVarLimitFraction", 0.05),
            tail_dep_kelly_threshold=corr_config_raw.get("tailDepKellyThreshold", 0.15),
            tail_dep_kelly_cut=corr_config_raw.get("tailDepKellyCut", 0.25),
            var_confidence=corr_config_raw.get("varConfidence", 0.99),
            copula_df=corr_config_raw.get("copulaDf", 5),
        )
        corr_state = str(Path(self._state_path).parent / "correlation-state.json") if self._state_path else None
        self._correlation_engine = CorrelationEngine(config=corr_config, state_path=corr_state, logger=self.log)
```

**Step 3: Run tests**

Run: `pytest tests/ -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add config/bots-config.json src/kalshi/capital_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add correlation engine config to bots-config.json

Configurable cluster limits, VaR thresholds, copula parameters, and
Kelly reduction settings. Defaults match design doc values."
```

---

### Task 17: Dashboard Integration

**Files:**
- Modify: `scripts/dashboard.py` (or `scripts/dashboard-server.py`)

**Context:** The dashboard should expose correlation engine state for monitoring. Add a new API endpoint `/api/correlation` that returns cluster risk, portfolio VaR, and factor correlations.

**Step 1: Add correlation API endpoint**

Find the dashboard FastAPI app and add:

```python
@app.get("/api/correlation")
async def get_correlation():
    """Return correlation engine state for monitoring."""
    state_path = DATA_DIR / "correlation-state.json"
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"cluster_risk": {}, "portfolio_var": 0, "last_updated": ""}
```

**Step 2: Run dashboard to verify endpoint works**

Run: `python3 scripts/dashboard-server.py &`
Then: `curl http://localhost:3456/api/correlation`
Expected: JSON response with cluster risk data.

**Step 3: Commit**

```bash
git add scripts/dashboard-server.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add /api/correlation dashboard endpoint"
```

---

### Task 18: Final Verification and Merge

**Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

**Step 2: Verify the CPI blowup scenario is prevented**

Write and run a quick verification script:

```python
"""Verify CPI concentration blowup is prevented."""
from correlation_engine import CorrelationEngine, CorrelationConfig

engine = CorrelationEngine(config=CorrelationConfig(cluster_max_fraction=0.15))

# Simulate the actual CPI blowup: $1720 in CPI on $5090 balance
balance = 509000  # cents
max_cpi = int(balance * 0.15)  # $763.50

print(f"Balance: ${balance/100:,.0f}")
print(f"Max CPI cluster: ${max_cpi/100:,.0f} ({0.15*100:.0f}%)")

# First $760 should be allowed
engine.record_trade("KXCPI-26MAY-T20", 38000)
engine.record_trade("KXCPI-26MAY-T21", 38000)

ok, reason = engine.check_cluster_limit("KXCPI-26MAY-T22", 1000, balance)
print(f"After $760: allowed={ok} ({reason})")
assert not ok, "Should be blocked!"

print("\nCPI concentration blowup PREVENTED.")
```

Run: `python3 -c "...the above code..."`
Expected: "CPI concentration blowup PREVENTED."

**Step 3: Count tests**

Run: `pytest tests/test_correlation_engine.py tests/test_allocator.py -v --co`
Expected: 40+ test cases total.

**Step 4: Merge to main**

```bash
git checkout main
git merge track2/quant-infrastructure
```

**Step 5: Push**

```bash
git push origin main
```

---

### Summary of Changes

| File | Change |
|---|---|
| `requirements.txt` | Add `scipy>=1.11.0` |
| `src/kalshi/correlation_engine.py` | **New:** ~300 lines. Factor mapping, cluster risk, VaR, copula tail dependence, state persistence |
| `src/kalshi/capital_allocator.py` | Add checks #6 (cluster), #7 (marginal VaR), #8 (tail Kelly). ~50 lines of changes |
| `config/bots-config.json` | Add `correlation` config section |
| `tests/test_correlation_engine.py` | **New:** ~200 lines, 35+ tests across 6 test classes |
| `tests/test_allocator.py` | Add `TestCorrelationIntegration` class, 3+ tests |
| `scripts/dashboard-server.py` | Add `/api/correlation` endpoint |

### Design Decisions

1. **A priori factor structure over empirical-only correlation:** We don't have enough trade history to compute reliable empirical correlations (need 30+ co-occurring trades per pair). The factor mapping with known economic relationships provides immediate protection. Empirical refinement is deferred to a future phase when we have more data.

2. **Super-cluster merging at 70% threshold:** CPI and CORE_CPI (85% correlated) are merged into a single cluster for concentration limits. This prevents gaming the limit by splitting across tightly correlated markets.

3. **scipy dependency:** Required for `scipy.stats.t` (copula tail dependence) and `scipy.stats.norm.ppf` (VaR). The particle filter (T2-Phase 1) avoided scipy using pure Python, but copula fitting genuinely needs it. The ~30MB dependency increase is acceptable for production.

4. **Variance-covariance VaR (not Monte Carlo):** Binary contracts have bounded losses, so the Bernoulli variance model is adequate. Monte Carlo would be more accurate for tail scenarios but adds complexity without proportional value at our portfolio size.

5. **Tail dependence → Kelly reduction (not trade blocking):** Check #8 reduces sizing rather than blocking trades entirely. This preserves the option to trade while acknowledging tail risk. The 25% cut is conservative but meaningful.
