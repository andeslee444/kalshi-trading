"""Strategy engine: Bayesian edge estimation, dual-tail longshot exploitation,
correlation-aware sizing, and confidence-scaled Kelly.

All math uses only the ``math`` module (no scipy/numpy).
"""

import json
import logging
import math
from pathlib import Path
from collections import namedtuple

from probability import (
    LONGSHOT_BIAS_PARAMS,
    classify_ticker_category,
    longshot_edge,
    quarter_kelly,
    quarter_kelly_sell,
    kalshi_fee_cents,
)

_log = logging.getLogger("strategy_engine")


# ───────────────────────────────────────────────────────────────────
# Data types
# ───────────────────────────────────────────────────────────────────

EdgeEstimate = namedtuple(
    "EdgeEstimate",
    ["mu_edge", "sigma_edge", "confidence_ratio", "category", "price_bucket", "n_observations"],
)


# ───────────────────────────────────────────────────────────────────
# Bayesian Kelly multiplier (confidence-scaled Kelly)
# ───────────────────────────────────────────────────────────────────

def bayesian_kelly_multiplier(confidence_ratio):
    """Confidence-scaled Kelly multiplier.

    confidence_ratio = mu_edge / sigma_edge

    - confidence_ratio <= 0:   return 0.0
    - confidence_ratio < 1.0:  return confidence_ratio^2  (quadratic penalty)
    - confidence_ratio < 2.0:  return confidence_ratio / 2.0
    - confidence_ratio >= 2.0: return 1.0 (capped)
    """
    if confidence_ratio <= 0:
        return 0.0
    if confidence_ratio <= 1.0:
        return confidence_ratio ** 2
    if confidence_ratio < 2.0:
        return confidence_ratio / 2.0
    return 1.0


# ───────────────────────────────────────────────────────────────────
# Price bucket definitions
# ───────────────────────────────────────────────────────────────────

PRICE_BUCKETS = ["1-5", "6-10", "11-15", "16-20", "21-30"]
BUCKET_MIDPRICES = {"1-5": 3, "6-10": 8, "11-15": 13, "16-20": 18, "21-30": 25}

# Default kappa (shrinkage strength) -- 30 observations to equal the prior
DEFAULT_KAPPA = 30

# Default prior sigma for edge uncertainty (before any data)
DEFAULT_PRIOR_SIGMA = 0.10


# ───────────────────────────────────────────────────────────────────
# BayesianEdgeEstimator
# ───────────────────────────────────────────────────────────────────

class BayesianEdgeEstimator:
    """Hierarchical Bayesian edge model with online learning.

    Maintains per-category, per-price-bucket win/loss counts.  Uses
    Becker (2025) longshot bias parameters as the prior and updates
    via moment-matched posterior shrinkage after each settlement.
    """

    def __init__(self, params_path=None, kappa=DEFAULT_KAPPA):
        self._kappa = kappa
        self._category_state = {}  # category -> {buckets: {bucket: {wins, losses}}, empirical_alpha, empirical_delta}
        if params_path and Path(params_path).exists():
            self.load_params(params_path)

    # ── Bucket mapping ──

    @staticmethod
    def _price_bucket(price_cents):
        """Map 1-30c to a price bucket name."""
        if price_cents <= 5:
            return "1-5"
        if price_cents <= 10:
            return "6-10"
        if price_cents <= 15:
            return "11-15"
        if price_cents <= 20:
            return "16-20"
        return "21-30"

    # ── State helpers ──

    def _ensure_category(self, category):
        """Ensure category state exists with default structure."""
        if category not in self._category_state:
            self._category_state[category] = {
                "buckets": {b: {"wins": 0, "losses": 0} for b in PRICE_BUCKETS},
                "empirical_alpha": None,
                "empirical_delta": None,
            }

    def _total_observations(self, category):
        """Total win + loss observations across all buckets for a category."""
        if category not in self._category_state:
            return 0
        buckets = self._category_state[category]["buckets"]
        return sum(b["wins"] + b["losses"] for b in buckets.values())

    # ── Edge estimation ──

    def estimate_edge(self, price_cents, category, hours_to_close=999):
        """Estimate longshot edge with Bayesian posterior.

        Returns EdgeEstimate(mu_edge, sigma_edge, confidence_ratio, ...).
        With 0 observations, returns Becker prior edge.
        """
        if price_cents <= 0 or price_cents > 99:
            bucket = self._price_bucket(max(1, min(30, price_cents)))
            return EdgeEstimate(0.0, DEFAULT_PRIOR_SIGMA, 0.0, category, bucket, 0)

        self._ensure_category(category)
        n_obs = self._total_observations(category)
        bucket = self._price_bucket(min(price_cents, 30))

        # Get prior from Becker params
        prior_alpha, prior_delta = LONGSHOT_BIAS_PARAMS.get(
            category, LONGSHOT_BIAS_PARAMS["default"]
        )

        # Compute posterior via shrinkage
        weight = n_obs / (n_obs + self._kappa)  # 0 at n=0, converges to 1

        state = self._category_state[category]
        emp_alpha = state.get("empirical_alpha")
        emp_delta = state.get("empirical_delta")

        if emp_alpha is not None and emp_delta is not None:
            post_alpha = weight * emp_alpha + (1 - weight) * prior_alpha
            post_delta = weight * emp_delta + (1 - weight) * prior_delta
        else:
            post_alpha = prior_alpha
            post_delta = prior_delta

        # Time decay: same formula as longshot_edge()
        time_factor = min(1.0, 0.5 + 0.5 * min(hours_to_close, 24) / 24)

        # Overpricing and edge
        overpricing_ratio = post_alpha * math.exp(-post_delta * price_cents) * time_factor
        implied_prob = price_cents / 100.0
        mu_edge = max(0.0, implied_prob * overpricing_ratio)

        # Posterior uncertainty shrinks with data
        sigma_edge = DEFAULT_PRIOR_SIGMA / math.sqrt(1 + n_obs / self._kappa)

        # Confidence ratio
        conf_ratio = mu_edge / sigma_edge if sigma_edge > 0 else 0.0

        return EdgeEstimate(
            mu_edge=mu_edge,
            sigma_edge=sigma_edge,
            confidence_ratio=conf_ratio,
            category=category,
            price_bucket=bucket,
            n_observations=n_obs,
        )

    def estimate_edge_buy(self, yes_price_cents, category, hours_to_close=999):
        """Estimate buy-side longshot edge (YES 70-99c, NO-side is the longshot).

        Computes NO-side overpricing and returns edge for buying YES.
        """
        no_price = 100 - yes_price_cents
        if no_price <= 0 or no_price > 30:
            bucket = self._price_bucket(max(1, min(30, no_price)))
            return EdgeEstimate(0.0, DEFAULT_PRIOR_SIGMA, 0.0, category, bucket, 0)

        # Use the sell-side model on the NO price
        return self.estimate_edge(no_price, category, hours_to_close)

    # ── Online update ──

    def update_posterior(self, category, price_cents, won):
        """Update posterior after a settlement.

        won: True if the seller won (YES expired worthless), False if buyer won.
        """
        self._ensure_category(category)
        bucket = self._price_bucket(min(max(price_cents, 1), 30))
        bd = self._category_state[category]["buckets"][bucket]
        if won:
            bd["wins"] += 1
        else:
            bd["losses"] += 1

        # Refit empirical alpha/delta via moment matching
        bucket_data = self._category_state[category]["buckets"]
        result = self._moment_match_alpha_delta(bucket_data)
        if result is not None:
            self._category_state[category]["empirical_alpha"] = result[0]
            self._category_state[category]["empirical_delta"] = result[1]

    # ── Moment matching (grid search) ──

    def _moment_match_alpha_delta(self, bucket_data):
        """Fit alpha/delta from bucket win/loss data via grid search.

        For each bucket with data, compute empirical seller win rate.
        The Becker model predicts:
            seller_win_rate = 1 - implied_prob * (1 - alpha * exp(-delta * midprice))

        Grid search over alpha in [0.1, 0.9] step 0.05 and
        delta in [0.05, 0.30] step 0.01 to minimize weighted SSE.

        Returns (alpha, delta) or None if fewer than 2 buckets have data.
        """
        # Collect buckets with data
        active = {}
        for bucket_name, data in bucket_data.items():
            total = data["wins"] + data["losses"]
            if total > 0:
                active[bucket_name] = (data["wins"] / total, total)

        if len(active) < 2:
            return None

        best_alpha = None
        best_delta = None
        best_sse = float("inf")

        # Grid search
        alpha_range = [round(0.1 + 0.05 * i, 2) for i in range(17)]  # 0.10 to 0.90
        delta_range = [round(0.05 + 0.01 * i, 2) for i in range(26)]  # 0.05 to 0.30

        for alpha in alpha_range:
            for delta in delta_range:
                sse = 0.0
                for bucket_name, (obs_win_rate, n) in active.items():
                    mid = BUCKET_MIDPRICES[bucket_name]
                    implied = mid / 100.0
                    pred_win_rate = 1.0 - implied * (1.0 - alpha * math.exp(-delta * mid))
                    pred_win_rate = max(0.0, min(1.0, pred_win_rate))
                    sse += n * (pred_win_rate - obs_win_rate) ** 2
                if sse < best_sse:
                    best_sse = sse
                    best_alpha = alpha
                    best_delta = delta

        return (best_alpha, best_delta)

    # ── Persistence ──

    def save_params(self, path):
        """Save category state to JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "kappa": self._kappa,
            "categories": self._category_state,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)

    def load_params(self, path):
        """Load category state from JSON file."""
        path = Path(path)
        if not path.exists():
            return
        data = json.loads(path.read_text())
        self._kappa = data.get("kappa", DEFAULT_KAPPA)
        self._category_state = data.get("categories", {})


# ───────────────────────────────────────────────────────────────────
# Dual-tail longshot functions
# ───────────────────────────────────────────────────────────────────

def longshot_edge_sell(yes_price_cents, ticker="", hours_to_close=999, estimator=None):
    """Sell-side longshot edge.  Range: 1-30c (expanded from 15c).

    If estimator (BayesianEdgeEstimator) is provided, uses Bayesian
    posterior; otherwise falls back to the original longshot_edge().
    """
    if yes_price_cents <= 0 or yes_price_cents > 30:
        return 0.0

    if estimator is not None:
        category = classify_ticker_category(ticker)
        result = estimator.estimate_edge(yes_price_cents, category, hours_to_close)
        return result.mu_edge

    # Fallback: original Becker point estimate
    return longshot_edge(yes_price_cents, ticker=ticker, hours_to_close=hours_to_close)


def longshot_edge_buy(yes_price_cents, ticker="", hours_to_close=999, estimator=None):
    """Buy-side longshot edge.  Range: YES 70-99c (NO-side is 1-30c longshot).

    Computes NO price and applies Becker model symmetrically.
    Returns edge for buying YES.
    """
    if yes_price_cents < 70 or yes_price_cents > 99:
        return 0.0

    no_price = 100 - yes_price_cents  # 1-30c range

    if estimator is not None:
        category = classify_ticker_category(ticker)
        result = estimator.estimate_edge(no_price, category, hours_to_close)
        return result.mu_edge

    # Fallback: Becker model on the NO price
    return longshot_edge(no_price, ticker=ticker, hours_to_close=hours_to_close)


# ───────────────────────────────────────────────────────────────────
# Correlation-aware sizing
# ───────────────────────────────────────────────────────────────────

# Category correlation priors (from design doc Section 4)
CATEGORY_CORRELATIONS = {
    "same_sport_same_night": 0.20,
    "same_sport_diff_night": 0.07,
    "cross_sport": 0.03,
    "entertainment_same_week": 0.15,
    "weather_same_region": 0.40,
    "cross_category": 0.02,
}

# Default intra-category rho by market category
_INTRA_CATEGORY_RHO = {
    "sports": 0.15,
    "entertainment": 0.15,
    "politics": 0.10,
    "weather": 0.30,
    "economics": 0.10,
    "crypto": 0.12,
    "default": 0.10,
    "cross_category": 0.02,
}


class CorrelationAwareSizer:
    """Copula-based Kelly sizing with category caps.

    Reduces Kelly fraction when multiple trades in the same category
    are correlated, and enforces hard risk limits per category.
    """

    def __init__(self, daily_budget_cents, category_cap_pct=0.30, single_trade_cap_pct=0.05):
        self._daily_budget = daily_budget_cents
        self._category_cap_pct = category_cap_pct
        self._single_trade_cap_pct = single_trade_cap_pct
        self._risk_by_category = {}  # category -> total risk cents today

    def get_intra_category_rho(self, category):
        """Return default intra-category pairwise correlation."""
        return _INTRA_CATEGORY_RHO.get(category, _INTRA_CATEGORY_RHO["default"])

    @staticmethod
    def effective_n(n_trades, rho):
        """Effective number of independent trades under equicorrelation.

        effective_n = n / (1 + (n - 1) * rho)
        For n <= 1, returns n (no reduction).
        """
        if n_trades <= 1:
            return float(n_trades)
        return n_trades / (1.0 + (n_trades - 1) * rho)

    def kelly_scale(self, n_trades, rho):
        """Kelly scaling factor for correlated trades.

        kelly_scale = sqrt(effective_n / n)
        Returns 1.0 for single trade.
        """
        if n_trades <= 1:
            return 1.0
        eff = self.effective_n(n_trades, rho)
        return math.sqrt(eff / n_trades)

    def check_category_cap(self, category, proposed_risk_cents):
        """Check if proposed trade would exceed category cap.

        Returns True if within cap, False if exceeded.
        """
        cap = self._daily_budget * self._category_cap_pct
        current = self._risk_by_category.get(category, 0)
        return (current + proposed_risk_cents) <= cap

    def record_trade(self, category, risk_cents):
        """Record risk for a completed trade."""
        self._risk_by_category[category] = self._risk_by_category.get(category, 0) + risk_cents

    def reset_daily(self):
        """Clear daily tracking at start of new day."""
        self._risk_by_category.clear()

    def size_trade(self, edge_estimate, n_concurrent_same_category, bankroll_cents,
                   max_cost_cents, side="sell"):
        """Size a trade with copula and Bayesian Kelly adjustments.

        side: "sell" for sell-side longshot, "buy" for buy-side longshot
        Returns (contracts, risk_cents, details_dict).
        """
        rho = self.get_intra_category_rho(edge_estimate.category)
        copula_scale = self.kelly_scale(n_concurrent_same_category, rho)
        bayes_mult = bayesian_kelly_multiplier(edge_estimate.confidence_ratio)

        fee_cents = kalshi_fee_cents(
            edge_estimate.mu_edge * 100 if side == "sell" else 50
        )

        if side == "sell":
            # Use the price_bucket midprice as a rough estimate for sizing
            mid = BUCKET_MIDPRICES.get(edge_estimate.price_bucket, 5)
            contracts, risk, details = quarter_kelly_sell(
                edge_estimate.mu_edge, mid, max_cost_cents,
                bankroll_cents=bankroll_cents, fee_cents=fee_cents,
                return_details=True,
            )
        else:
            mid = BUCKET_MIDPRICES.get(edge_estimate.price_bucket, 5)
            buy_price = 100 - mid
            contracts, risk, details = quarter_kelly(
                edge_estimate.mu_edge, buy_price, max_cost_cents,
                bankroll_cents=bankroll_cents, fee_cents=fee_cents,
                return_details=True,
            )

        # Apply scaling
        if contracts > 0:
            adjusted = max(1, int(contracts * copula_scale * bayes_mult))
        else:
            adjusted = 0

        if side == "sell":
            risk_per = 100 - BUCKET_MIDPRICES.get(edge_estimate.price_bucket, 5)
        else:
            risk_per = 100 - BUCKET_MIDPRICES.get(edge_estimate.price_bucket, 5)

        adjusted_risk = adjusted * risk_per if risk_per > 0 else 0

        return (adjusted, adjusted_risk, {
            **details,
            "copula_scale": round(copula_scale, 4),
            "bayesian_multiplier": round(bayes_mult, 4),
            "raw_contracts": contracts,
        })
