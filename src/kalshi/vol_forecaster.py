"""Volatility forecasting — GARCH(1,1), DCC correlation, intraday seasonality.

Provides forward-looking vol estimates and dynamic cross-asset correlation
for use by the crypto model stack and portfolio Kelly sizing.
"""
import math
import numpy as np

# Intraday vol profile (hour_utc -> multiplier)
# Based on typical crypto vol patterns:
# - US market open/close: high vol
# - Asia open: elevated vol
# - Early morning UTC (3-6am): lowest vol
_HOURLY_VOL_PROFILE = {
    0: 1.15,  # Asia open (9am JST)
    1: 1.10,
    2: 1.05,
    3: 0.85,  # Quiet
    4: 0.80,
    5: 0.75,  # Lowest
    6: 0.80,
    7: 0.85,
    8: 0.90,
    9: 0.95,
    10: 1.00,  # Europe open
    11: 1.05,
    12: 1.05,
    13: 1.10,  # Pre-US
    14: 1.25,  # US market open (9:30 ET)
    15: 1.20,
    16: 1.15,
    17: 1.10,
    18: 1.05,
    19: 1.10,
    20: 1.15,  # US close
    21: 1.10,  # Post-close
    22: 1.05,
    23: 1.10,  # Asia pre-open
}

_WEEKEND_DISCOUNT = 0.80  # 20% lower vol on weekends


def intraday_vol_multiplier(hour_utc, day_of_week):
    """Return vol multiplier for given hour and day.

    Args:
        hour_utc: Hour in UTC (0-23)
        day_of_week: 0=Monday, 6=Sunday

    Returns:
        float: Multiplier in [0.5, 1.5]
    """
    base = _HOURLY_VOL_PROFILE.get(hour_utc % 24, 1.0)
    if day_of_week >= 5:  # Saturday or Sunday
        base *= _WEEKEND_DISCOUNT
    return max(0.5, min(1.5, base))


def correct_bid_ask_bounce(raw_rv, spread_cents, n_observations):
    """Remove bid-ask bounce variance from realized vol estimate.

    The bid-ask bounce induces artificial variance of approximately
    spread^2 / (4 * n) in the return series.

    Args:
        raw_rv: Raw realized volatility (annualized, decimal)
        spread_cents: Bid-ask spread in cents
        n_observations: Number of price observations used

    Returns:
        float: Corrected realized vol (>= 0)
    """
    if spread_cents <= 0 or n_observations <= 0:
        return raw_rv
    # Bounce variance (per observation, annualized)
    bounce_var = 2.0 * (spread_cents / 10000) ** 2 / max(1, n_observations)
    raw_var = raw_rv ** 2
    corrected_var = max(0.0, raw_var - bounce_var)
    return math.sqrt(corrected_var)


class GARCHForecaster:
    """GARCH(1,1) volatility forecaster.

    sigma^2_{t+1} = omega + alpha * epsilon^2_t + beta * sigma^2_t

    Uses simple recursive updates (no MLE fitting — that's in calibrate-crypto.py).
    """

    def __init__(self, omega=0.000002, alpha=0.10, beta=0.85, min_observations=10):
        self._omega = omega
        self._alpha = alpha
        self._beta = beta
        self._min_obs = min_observations
        self._returns = []
        self._sigma2 = None  # Current conditional variance
        self._sigma2_history = []  # For vol-of-vol computation

    def update(self, log_return):
        """Update with a new log return observation."""
        self._returns.append(log_return)
        if len(self._returns) < self._min_obs:
            return

        if self._sigma2 is None:
            # Initialize with sample variance
            self._sigma2 = sum(r ** 2 for r in self._returns) / len(self._returns)

        # GARCH(1,1) recursion
        self._sigma2 = (
            self._omega
            + self._alpha * log_return ** 2
            + self._beta * self._sigma2
        )
        self._sigma2 = max(1e-10, self._sigma2)  # floor
        self._sigma2_history.append(self._sigma2)
        if len(self._sigma2_history) > 500:
            self._sigma2_history = self._sigma2_history[-500:]

    def forecast_vol(self, annualize_factor=None):
        """Forecast next-period volatility.

        Args:
            annualize_factor: If provided, multiply by sqrt(factor). If None,
                returns per-observation vol (caller annualizes based on interval).

        Returns:
            float (annualized vol) or None if insufficient data.
        """
        if self._sigma2 is None:
            return None
        vol = math.sqrt(self._sigma2)
        if annualize_factor is not None:
            vol *= math.sqrt(annualize_factor)
        return vol

    def vol_of_vol(self):
        """Estimate vol-of-vol from conditional variance history.

        Returns float or None.
        """
        if len(self._sigma2_history) < 10:
            return None
        vols = [math.sqrt(s) for s in self._sigma2_history]
        mean_vol = sum(vols) / len(vols)
        variance = sum((v - mean_vol) ** 2 for v in vols) / (len(vols) - 1)
        return math.sqrt(variance)

    def heston_v0(self):
        """Current variance for Heston v0 parameter."""
        return self._sigma2 if self._sigma2 is not None else 0.25


class DCCCorrelation:
    """Dynamic Conditional Correlation model.

    Simplified DCC: uses exponentially weighted correlation updates
    rather than full DCC-GARCH (which requires MLE).

    Q_{t+1} = (1-lambda)*eps_t*eps_t' + lambda*Q_t
    R_t = diag(Q)^{-1/2} * Q * diag(Q)^{-1/2}
    """

    def __init__(self, assets, decay=0.94, min_observations=20):
        self._assets = list(assets)
        self._n = len(assets)
        self._decay = decay
        self._min_obs = min_observations
        self._count = 0
        self._Q = np.eye(self._n)  # Correlation matrix (running average)
        self._asset_idx = {a: i for i, a in enumerate(assets)}
        self._garch = {a: GARCHForecaster() for a in assets}

    def update(self, returns_dict):
        """Update with new returns for each asset.

        Args:
            returns_dict: {"BTC": 0.01, "ETH": 0.012, ...}
        """
        # Update per-asset GARCH
        for asset, ret in returns_dict.items():
            if asset in self._garch:
                self._garch[asset].update(ret)

        # Build standardized residuals
        eps = np.zeros(self._n)
        for asset, ret in returns_dict.items():
            idx = self._asset_idx.get(asset)
            if idx is not None:
                vol = self._garch[asset].forecast_vol()
                if vol and vol > 0:
                    eps[idx] = ret / vol
                else:
                    eps[idx] = ret / 0.02  # fallback

        self._count += 1
        # Exponentially weighted update
        outer = np.outer(eps, eps)
        self._Q = self._decay * self._Q + (1 - self._decay) * outer

    def correlation_matrix(self):
        """Return current correlation matrix.

        Returns np.ndarray of shape (n, n) or None if insufficient data.
        """
        if self._count < self._min_obs:
            return None
        # Normalize: R = diag(Q)^{-1/2} * Q * diag(Q)^{-1/2}
        diag = np.sqrt(np.diag(self._Q))
        diag[diag < 1e-10] = 1e-10  # prevent division by zero
        D_inv = np.diag(1.0 / diag)
        R = D_inv @ self._Q @ D_inv
        # Clamp to [-1, 1]
        R = np.clip(R, -1.0, 1.0)
        np.fill_diagonal(R, 1.0)
        return R

    def pair_correlation(self, asset_a, asset_b):
        """Get pairwise correlation between two assets.

        Returns float or None.
        """
        R = self.correlation_matrix()
        if R is None:
            return None
        i = self._asset_idx.get(asset_a)
        j = self._asset_idx.get(asset_b)
        if i is None or j is None:
            return None
        return float(R[i, j])
