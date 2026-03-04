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
import os
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
        if ticker_a.upper() == ticker_b.upper():
            return 1.0
        fa = self.ticker_to_factor(ticker_a)
        fb = self.ticker_to_factor(ticker_b)
        return self.get_factor_correlation(fa, fb)

    # === Cluster Risk Tracking ===

    def _get_super_cluster(self, factor: str) -> List[str]:
        """Get all factors that should be grouped with this one due to high correlation.

        Factors with inter-factor correlation >= 0.70 are merged into a "super cluster"
        for concentration limit purposes. Uses BFS for transitive closure — if A-B and
        B-C are both >= 0.70, then A, B, C all share a cluster.
        """
        MERGE_THRESHOLD = 0.70
        visited = set()
        queue = [factor]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for (fa, fb), corr in INTER_FACTOR_CORRELATIONS.items():
                if corr >= MERGE_THRESHOLD:
                    if fa == current and fb not in visited:
                        queue.append(fb)
                    elif fb == current and fa not in visited:
                        queue.append(fa)
        return sorted(visited)

    def _cluster_key(self, factor: str) -> str:
        """Get the canonical cluster key for a factor (handles super-clusters)."""
        members = self._get_super_cluster(factor)
        return "+".join(members)

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
            return (False, f"cluster {key} would reach ${projected/100:.0f} "
                          f"(limit ${max_cluster_risk/100:.0f} = "
                          f"{self.config.cluster_max_fraction*100:.0f}% of ${available_balance_cents/100:.0f})")

        return (True, "")

    # === Portfolio VaR ===

    def compute_portfolio_var(self, positions: List[Dict], confidence: Optional[float] = None) -> float:
        """Compute portfolio Value-at-Risk using variance-covariance method.

        Each position dict has:
          - ticker: market ticker
          - risk_cents: maximum loss in cents (cost basis)
          - loss_prob: probability of losing (1 - model probability of winning)

        For binary contracts:
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

        # z-score for confidence level
        from scipy.stats import norm
        z = norm.ppf(conf)

        var = z * portfolio_std
        self._portfolio_var = var
        return var

    # === Tail Dependence ===

    def compute_tail_dependence(self, factor_a: str, factor_b: str) -> float:
        """Compute lower tail dependence coefficient using Student-t copula.

        For a bivariate t-copula with correlation rho and nu degrees of freedom:
          lambda = 2 * t_{nu+1}(-sqrt((nu+1)(1-rho)/(1+rho)))

        Returns lambda in [0, 1]. Higher = more tail dependence = more crisis correlation.
        """
        rho = self.get_factor_correlation(factor_a, factor_b)

        if abs(rho) < 1e-6:
            return 0.0

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
            return 1.0 - self.config.tail_dep_kelly_cut

        return 1.0

    # === Marginal VaR ===

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

    # === State Persistence ===

    def save_state(self) -> None:
        """Persist correlation state to disk."""
        if not self.state_path:
            return

        state = {
            "cluster_risk": self._cluster_risk,
            "portfolio_var": self._portfolio_var,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

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

    def reset_daily(self) -> None:
        """Reset daily risk tracking."""
        self._cluster_risk.clear()
        self._portfolio_var = 0.0
