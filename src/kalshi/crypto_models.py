"""Crypto model stack — unified interface for probability estimation.

Provides GBM, Merton JD, Heston, AR(1) vol forecast, and BMA ensemble.
All models implement the same interface: estimate_prob(spot, strike, vol, time, params).
"""
import math
from probability import (
    crypto_price_probability,
    crypto_price_probability_jd,
    crypto_price_probability_heston,
)

# Regime-dependent jump intensity (replaces fixed lambda=1.0)
REGIME_JUMP_INTENSITY = {
    "low_vol": 0.1,
    "normal": 1.0,
    "high_vol": 3.0,
    "crisis": 10.0,
}

# Regime-dependent BMA weights: [GBM, JD, Heston]
REGIME_BMA_WEIGHTS = {
    "low_vol":  [0.50, 0.30, 0.20],
    "normal":   [0.35, 0.35, 0.30],
    "high_vol": [0.15, 0.40, 0.45],
    "crisis":   [0.05, 0.35, 0.60],
}

# Default Heston parameters (calibrated by calibrate-crypto.py)
DEFAULT_HESTON = {"v0": 0.25, "kappa": 2.0, "theta": 0.25, "xi": 0.3, "rho": -0.7}

# Default JD parameters
DEFAULT_JD = {"jump_mean": -0.05, "jump_std": 0.10, "max_jumps": 10}


def smooth_edge_threshold(prob, base=0.06, scale=0.09):
    """Smooth edge threshold — higher near prob=0.5, lower at extremes.

    Formula: base + scale * (0.5 - |prob - 0.5|)^2
    At prob=0.50: base + scale*0.25 (max uncertainty -> highest threshold)
    At prob=0.05: base + scale*0.0025 (high confidence -> near base)
    """
    proximity = 0.5 - abs(prob - 0.5)
    return base + scale * proximity ** 2


def horizon_kelly_fraction(minutes_to_settle):
    """Horizon-scaled Kelly fraction.

    Short horizons use smaller Kelly (more noise, less model confidence).
    Long horizons use larger Kelly (more data, better model accuracy).
    """
    if minutes_to_settle < 15:
        return 0.125   # 1/8 Kelly
    elif minutes_to_settle < 60:
        return 0.20    # 1/5 Kelly
    elif minutes_to_settle < 360:
        return 0.25    # 1/4 Kelly
    elif minutes_to_settle < 1440:
        return 0.333   # 1/3 Kelly
    else:
        return 0.50    # 1/2 Kelly


def horizon_vol_weights(minutes_to_settle):
    """Horizon-dependent IV/RV weighting.

    Short horizons trust RV more (captures recent dynamics).
    Long horizons trust IV more (forward-looking).

    Returns (w_iv, w_rv) that sum to 1.0.
    """
    # w_rv = 30 / (30 + T), w_iv = T / (30 + T)
    # At T=5 min: ~86% RV. At T=30 min: 50/50. At T=1440: ~98% IV.
    w_rv = 30.0 / (30.0 + minutes_to_settle)
    w_iv = minutes_to_settle / (30.0 + minutes_to_settle)
    return w_iv, w_rv


class AR1VolForecast:
    """Simple AR(1) volatility forecaster.

    sigma_{t+1} = alpha + beta * sigma_t

    Maintains a rolling window of vol observations and estimates
    AR(1) parameters via simple regression.
    """

    def __init__(self, alpha=None, beta=None, long_run=0.50, min_observations=3):
        self._observations = []
        self._alpha = alpha
        self._beta = beta
        self._long_run = long_run
        self._min_obs = min_observations

    def update(self, vol):
        """Record a new vol observation."""
        self._observations.append(vol)
        # Keep last 100 observations
        if len(self._observations) > 100:
            self._observations = self._observations[-100:]

    def forecast(self):
        """Forecast next-period vol using AR(1).

        Returns float (forecasted vol) or None if insufficient data.
        """
        if len(self._observations) < self._min_obs:
            return None

        obs = self._observations
        if self._alpha is not None and self._beta is not None:
            # Use provided parameters
            return max(0.01, self._alpha + self._beta * obs[-1])

        # Estimate AR(1) via OLS: sigma_t = alpha + beta * sigma_{t-1}
        x = obs[:-1]  # sigma_{t-1}
        y = obs[1:]    # sigma_t
        x_mean = sum(x) / len(x)
        y_mean = sum(y) / len(y)
        num = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
        den = sum((xi - x_mean) ** 2 for xi in x)
        if den < 1e-12:
            return self._long_run
        beta = num / den
        alpha = y_mean - beta * x_mean
        # Clamp beta to [0, 0.999] for stationarity
        beta = max(0.0, min(0.999, beta))
        return max(0.01, alpha + beta * obs[-1])


class EnsembleModel:
    """Bayesian Model Average ensemble of GBM, JD, and Heston.

    Blends model probabilities with regime-dependent weights.
    """

    def __init__(self, jd_params=None, heston_params=None, bma_weights=None):
        self._jd_params = jd_params or dict(DEFAULT_JD)
        self._heston_params = heston_params or dict(DEFAULT_HESTON)
        self._bma_weights = bma_weights or dict(REGIME_BMA_WEIGHTS)

    @classmethod
    def from_calibration(cls, calibration_dict, asset="BTC"):
        """Build an EnsembleModel from calibration output.

        Args:
            calibration_dict: Parsed config/crypto-calibration.json
            asset: Which asset's calibration to use (default "BTC")

        Returns:
            EnsembleModel with calibrated weights and params.
        """
        assets = calibration_dict.get("assets", {})
        asset_cal = assets.get(asset, {})

        # Extract calibrated BMA weights
        bma_weights = None
        cal_weights = asset_cal.get("ensemble_weights")
        if cal_weights and len(cal_weights) == 3 and sum(cal_weights) > 0.99:
            # Apply calibrated weights to all regimes (override normal, keep regime structure)
            bma_weights = dict(REGIME_BMA_WEIGHTS)
            bma_weights["normal"] = cal_weights

        # Extract calibrated Heston params
        heston_params = None
        cal_heston = asset_cal.get("heston_params")
        if cal_heston:
            heston_params = dict(DEFAULT_HESTON)
            heston_params.update(cal_heston)

        # Extract calibrated JD params
        jd_params = None
        cal_lambda = asset_cal.get("jd_lambda")
        if cal_lambda is not None:
            jd_params = dict(DEFAULT_JD)

        return cls(jd_params=jd_params, heston_params=heston_params, bma_weights=bma_weights)

    def estimate_prob(self, current_price, threshold, direction="above",
                      time_horizon_minutes=1440, vol=0.50, regime="normal",
                      drift_pct=0.0, heston_params=None, use_ou=False,
                      ou_half_life_minutes=120):
        """Compute ensemble probability via BMA.

        Returns float in [0.001, 0.999].
        """
        weights = self._bma_weights.get(regime, self._bma_weights["normal"])

        # GBM
        p_gbm = crypto_price_probability(
            current_price=current_price, threshold=threshold,
            direction=direction, time_horizon_minutes=time_horizon_minutes,
            realized_vol_pct=vol, drift_pct=drift_pct,
            use_ou=use_ou, ou_half_life_minutes=ou_half_life_minutes,
        )

        # JD with regime-dependent lambda
        lam = REGIME_JUMP_INTENSITY.get(regime, 1.0)
        p_jd = crypto_price_probability_jd(
            current_price=current_price, threshold=threshold,
            direction=direction, time_horizon_minutes=time_horizon_minutes,
            realized_vol_pct=vol, drift_pct=drift_pct,
            jump_intensity=lam, **self._jd_params,
        )

        # Heston
        hp = heston_params or self._heston_params
        # Use vol^2 as v0 if not explicitly provided
        hp_with_v0 = dict(hp)
        if "v0" not in hp_with_v0 or hp_with_v0["v0"] is None:
            hp_with_v0["v0"] = vol ** 2
        p_heston = crypto_price_probability_heston(
            current_price=current_price, threshold=threshold,
            direction=direction, time_horizon_minutes=time_horizon_minutes,
            drift_pct=drift_pct, **hp_with_v0,
        )

        # BMA blend
        ensemble = weights[0] * p_gbm + weights[1] * p_jd + weights[2] * p_heston
        return max(0.001, min(0.999, ensemble))

    def estimate_bracket_prob(self, current_price, low_threshold, high_threshold,
                              time_horizon_minutes=1440, vol=0.50, regime="normal",
                              drift_pct=0.0, heston_params=None):
        """P(low <= S_T < high) = P(above low) - P(above high)."""
        p_low = self.estimate_prob(
            current_price, low_threshold, "above",
            time_horizon_minutes, vol, regime, drift_pct, heston_params,
        )
        p_high = self.estimate_prob(
            current_price, high_threshold, "above",
            time_horizon_minutes, vol, regime, drift_pct, heston_params,
        )
        return max(0.0, p_low - p_high)

    def _jd_prob(self, current_price, threshold, direction="above",
                 time_horizon_minutes=1440, vol=0.50, regime="normal",
                 drift_pct=0.0):
        """Direct access to regime-aware JD probability (for testing)."""
        lam = REGIME_JUMP_INTENSITY.get(regime, 1.0)
        return crypto_price_probability_jd(
            current_price=current_price, threshold=threshold,
            direction=direction, time_horizon_minutes=time_horizon_minutes,
            realized_vol_pct=vol, drift_pct=drift_pct,
            jump_intensity=lam, **self._jd_params,
        )
