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
    Includes a variance ceiling to prevent post-crash vol explosion.
    """

    # Maximum annualized vol (200%). Prevents GARCH from producing
    # unreasonably high vol after flash crashes, which would cause the
    # model to skip all trades for hours.
    MAX_ANNUALIZED_VOL = 2.0

    def __init__(self, omega=0.000002, alpha=0.10, beta=0.85, min_observations=10):
        self._omega = omega
        self._alpha = alpha
        self._beta = beta
        self._min_obs = min_observations
        self._returns = []
        self._intervals = []  # observation intervals in seconds
        self._sigma2 = None  # Current conditional variance
        self._sigma2_history = []  # For vol-of-vol computation

    def _max_sigma2(self):
        """Compute maximum per-observation variance from the annualized vol ceiling.

        Returns the per-step variance ceiling. Uses median interval to convert
        from annualized to per-step, or falls back to assuming 5-min intervals.
        """
        median_dt = self._median_interval_seconds()
        if median_dt and median_dt > 0:
            intervals_per_year = 365.25 * 86400 / median_dt
        else:
            # Assume 5-min intervals (crypto default scan) as fallback
            intervals_per_year = 365.25 * 24 * 12
        return (self.MAX_ANNUALIZED_VOL ** 2) / intervals_per_year

    def update(self, log_return, interval_seconds=None):
        """Update with a new log return observation.

        Args:
            log_return: Log return since last observation.
            interval_seconds: Actual time since last observation. If None, not tracked.
        """
        self._returns.append(log_return)
        if interval_seconds is not None:
            self._intervals.append(interval_seconds)
        if len(self._returns) < self._min_obs:
            return

        if self._sigma2 is None:
            # Initialize with sample variance — do NOT apply recursion yet
            self._sigma2 = sum(r ** 2 for r in self._returns) / len(self._returns)
            self._sigma2 = max(1e-10, self._sigma2)
            # Apply ceiling only if we have interval data to properly calibrate it
            if self._intervals:
                self._sigma2 = min(self._sigma2, self._max_sigma2())
            self._sigma2_history.append(self._sigma2)
            return

        # GARCH(1,1) recursion
        self._sigma2 = (
            self._omega
            + self._alpha * log_return ** 2
            + self._beta * self._sigma2
        )
        self._sigma2 = max(1e-10, self._sigma2)  # floor
        # Ceiling: prevent post-crash vol explosion (only when interval tracked)
        if self._intervals:
            self._sigma2 = min(self._sigma2, self._max_sigma2())
        self._sigma2_history.append(self._sigma2)
        if len(self._sigma2_history) > 500:
            self._sigma2_history = self._sigma2_history[-500:]

    def _median_interval_seconds(self):
        """Return median observation interval, or None if not tracked."""
        if not self._intervals:
            return None
        sorted_intervals = sorted(self._intervals)
        return sorted_intervals[len(sorted_intervals) // 2]

    def forecast_vol(self, annualize_factor=None, use_actual_interval=False):
        """Forecast next-period volatility.

        Args:
            annualize_factor: If provided, multiply by sqrt(factor). If None and
                use_actual_interval is False, returns per-observation vol.
            use_actual_interval: If True, compute annualize_factor from tracked
                observation intervals. Falls back to annualize_factor if no
                intervals tracked.

        Returns:
            float (annualized vol capped at MAX_ANNUALIZED_VOL, or per-obs vol)
            or None if insufficient data.
        """
        if self._sigma2 is None:
            return None
        vol = math.sqrt(self._sigma2)

        annualized = False
        if use_actual_interval:
            median_dt = self._median_interval_seconds()
            if median_dt and median_dt > 0:
                intervals_per_year = 365.25 * 86400 / median_dt
                vol *= math.sqrt(intervals_per_year)
                annualized = True
            elif annualize_factor is not None:
                vol *= math.sqrt(annualize_factor)
                annualized = True
        elif annualize_factor is not None:
            vol *= math.sqrt(annualize_factor)
            annualized = True

        # Cap annualized vol to prevent post-crash explosion
        if annualized:
            vol = min(vol, self.MAX_ANNUALIZED_VOL)

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

        # Skip DCC update until all GARCH forecasters are warmed up
        all_warmed = all(
            self._garch[a].forecast_vol() is not None
            for a in self._assets if a in self._garch
        )
        if not all_warmed:
            return  # Don't count as DCC update — Q matrix not being updated

        # Build standardized residuals
        eps = np.zeros(self._n)
        for asset, ret in returns_dict.items():
            idx = self._asset_idx.get(asset)
            if idx is not None:
                vol = self._garch[asset].forecast_vol()
                if vol and vol > 0:
                    eps[idx] = ret / vol
                else:
                    eps[idx] = 0.0

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
