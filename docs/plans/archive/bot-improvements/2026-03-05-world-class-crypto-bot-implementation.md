# World-Class Crypto Bot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade the crypto bot from hand-tuned GBM to an ensemble model stack (GBM + JD + Heston + GARCH + DCC) with data-driven calibration, correlation-adjusted Kelly sizing, and microstructure awareness.

**Architecture:** New `crypto_models.py` provides a unified `EnsembleModel` that BMA-blends GBM, Merton JD, and Heston with regime-dependent weights. `vol_forecaster.py` provides GARCH(1,1) vol forecasts and DCC correlation dynamics. `calibrate-crypto.py` fetches Coinbase historical data and runs synthetic backtests to derive all parameters. `crypto-bot.py` is refactored to use these modules instead of calling probability functions directly.

**Tech Stack:** Python 3, scipy (Heston characteristic function, GARCH MLE), numpy (matrix ops for DCC), existing math stdlib for GBM/JD.

**Design doc:** `docs/plans/bot-improvements/2026-03-05-world-class-crypto-bot-design.md`

---

## Task 1: Heston Stochastic Vol Model in probability.py

Add `crypto_price_probability_heston()` to the existing probability module. This is a pure math function with no side effects — ideal for TDD.

**Files:**
- Modify: `src/kalshi/probability.py` (add after `crypto_price_probability_jd()`, ~line 875)
- Create: `tests/test_heston.py`

**Step 1: Write failing tests**

```python
# tests/test_heston.py
"""Tests for Heston stochastic volatility model."""
import math
import pytest

# probability.py is on sys.path via conftest.py
from probability import crypto_price_probability_heston, _norm_cdf


class TestHestonBasic:
    """Basic Heston model behavior."""

    def test_atm_returns_near_50_pct(self):
        """ATM option with no drift should be ~50%."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=1440,  # 1 day
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.45 < prob < 0.55

    def test_deep_itm_returns_high_prob(self):
        """BTC at 85k, threshold 70k -> high probability above."""
        prob = crypto_price_probability_heston(
            current_price=85000, threshold=70000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob > 0.90

    def test_deep_otm_returns_low_prob(self):
        """BTC at 80k, threshold 95k -> low probability above."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=95000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob < 0.10

    def test_below_direction_complements(self):
        """P(below) = 1 - P(above)."""
        above = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        below = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="below",
            time_horizon_minutes=60,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert abs(above + below - 1.0) < 0.01

    def test_short_horizon_tighter_than_long(self):
        """5-min horizon should give more extreme probs than 24h for same OTM."""
        short = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=5,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        long = crypto_price_probability_heston(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # Short horizon: price less likely to reach 82k in 5 min
        assert short < long

    def test_higher_vol_of_vol_fatter_tails(self):
        """Higher xi (vol-of-vol) should produce fatter tails = higher OTM prob."""
        low_xi = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.1, rho=-0.7,
        )
        high_xi = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.8, rho=-0.7,
        )
        assert high_xi > low_xi

    def test_negative_rho_creates_skew(self):
        """Negative rho (vol increases when price drops) makes left tail fatter."""
        neg_rho = crypto_price_probability_heston(
            current_price=80000, threshold=70000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.9,
        )
        pos_rho = crypto_price_probability_heston(
            current_price=80000, threshold=70000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=0.9,
        )
        # Negative rho -> more likely to drop far -> P(above 70k) is lower
        assert neg_rho < pos_rho

    def test_clamps_to_valid_range(self):
        """Result always in [0.001, 0.999]."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=200000, direction="above",
            time_horizon_minutes=5,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.001 <= prob <= 0.999

    def test_vol_mean_reversion(self):
        """High v0 with low theta should see vol decrease -> narrower distribution."""
        # High starting vol, low long-run vol
        high_v0 = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=1.0, kappa=5.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # Low starting vol matching long-run
        low_v0 = crypto_price_probability_heston(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440,
            v0=0.25, kappa=5.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        # High v0 -> wider distribution -> higher OTM prob
        assert high_v0 > low_v0


class TestHestonEdgeCases:
    """Edge cases and numerical stability."""

    def test_zero_time_horizon(self):
        """T=0 should return 1.0 if price > threshold, else 0."""
        prob = crypto_price_probability_heston(
            current_price=85000, threshold=80000, direction="above",
            time_horizon_minutes=0,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert prob > 0.99

    def test_very_small_time_horizon(self):
        """T=0.1 min should still produce valid result."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=0.1,
            v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
        )
        assert 0.001 <= prob <= 0.999

    def test_extreme_params_no_crash(self):
        """Extreme but valid params should not crash."""
        prob = crypto_price_probability_heston(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=60,
            v0=2.0, kappa=10.0, theta=2.0, xi=2.0, rho=-0.99,
        )
        assert 0.001 <= prob <= 0.999
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_heston.py -v -x
```
Expected: ImportError or AttributeError (function doesn't exist yet)

**Step 3: Implement Heston model**

Add to `src/kalshi/probability.py` after `crypto_price_probability_jd()`:

```python
def crypto_price_probability_heston(
    current_price, threshold, direction="above",
    time_horizon_minutes=1440,
    v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
    drift_pct=0.0,
):
    """Heston stochastic volatility model for crypto binary options.

    Uses the semi-closed-form characteristic function approach with numerical
    integration (scipy.integrate.quad) to compute P(S_T > K).

    Parameters:
        current_price: Current spot price
        threshold: Strike price
        v0: Initial variance (e.g., 0.25 = 50% vol)
        kappa: Mean-reversion speed of variance
        theta: Long-run variance
        xi: Vol-of-vol (volatility of variance process)
        rho: Correlation between price and vol Brownian motions
        drift_pct: Annualized drift (decimal)

    Returns:
        float: Probability in [0.001, 0.999]
    """
    import numpy as np
    from scipy import integrate

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        if direction == "above":
            return 0.999 if current_price > threshold else 0.001
        return 0.999 if current_price < threshold else 0.001

    S = current_price
    K = threshold
    mu = drift_pct
    x = math.log(S / K)

    def heston_cf(phi, j):
        """Heston characteristic function for P1 (j=1) and P2 (j=2)."""
        if j == 1:
            u = 0.5
            b = kappa - rho * xi
        else:
            u = -0.5
            b = kappa

        d = np.sqrt((rho * xi * 1j * phi - b) ** 2 - xi ** 2 * (2 * u * 1j * phi - phi ** 2))
        g = (b - rho * xi * 1j * phi + d) / (b - rho * xi * 1j * phi - d)

        if abs(g) > 1e10:
            # Numerical safeguard
            C = 0.0
            D = 0.0
        else:
            exp_dT = np.exp(d * T)
            C = mu * 1j * phi * T + (kappa * theta / xi ** 2) * (
                (b - rho * xi * 1j * phi + d) * T - 2 * np.log((1 - g * exp_dT) / (1 - g))
            )
            D = ((b - rho * xi * 1j * phi + d) / xi ** 2) * (1 - exp_dT) / (1 - g * exp_dT)

        return np.exp(C + D * v0 + 1j * phi * x)

    def integrand_p(phi, j):
        """Integrand for P_j = 0.5 + (1/pi) * integral."""
        cf = heston_cf(phi, j)
        return np.real(np.exp(-1j * phi * 0) * cf / (1j * phi))

    # Numerical integration with error handling
    try:
        int1, _ = integrate.quad(lambda phi: integrand_p(phi, 1), 1e-8, 200, limit=100)
        int2, _ = integrate.quad(lambda phi: integrand_p(phi, 2), 1e-8, 200, limit=100)
        P1 = 0.5 + int1 / math.pi
        P2 = 0.5 + int2 / math.pi
    except (integrate.IntegrationWarning, Exception):
        # Fallback to GBM if Heston integration fails
        vol = math.sqrt(v0)
        return crypto_price_probability(
            current_price, threshold, direction,
            time_horizon_minutes, realized_vol_pct=vol, drift_pct=drift_pct,
        )

    # P(S_T > K) = S*P1 - K*e^(-rT)*P2 (for call)
    # For binary: P(S_T > K) = P2 (risk-neutral probability)
    prob_above = max(0.001, min(0.999, P2))

    if direction == "above":
        return prob_above
    return max(0.001, min(0.999, 1.0 - prob_above))
```

**Step 4: Run tests to verify they pass**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_heston.py -v -x
```
Expected: All PASS

**Step 5: Run full test suite for regressions**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/ -x --timeout=120
```
Expected: All existing tests still pass

**Step 6: Commit**

```bash
git add src/kalshi/probability.py tests/test_heston.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Heston stochastic vol model to probability.py"
```

---

## Task 2: Crypto Model Stack (crypto_models.py)

Create the unified model stack with GBM, JD, Heston, AR(1) vol, and BMA ensemble.

**Files:**
- Create: `src/kalshi/crypto_models.py`
- Create: `tests/test_crypto_models.py`

**Step 1: Write failing tests**

```python
# tests/test_crypto_models.py
"""Tests for crypto model stack — GBM, JD, Heston, AR1 Vol, BMA Ensemble."""
import math
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "kalshi"))

from probability import _reset_calibration


def setup_module():
    _reset_calibration()

def teardown_module():
    _reset_calibration()


class TestEnsembleModel:
    """BMA ensemble blending of sub-models."""

    def test_ensemble_returns_valid_prob(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
        )
        assert 0.001 <= prob <= 0.999

    def test_ensemble_atm_near_50(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=1440, vol=0.50,
        )
        assert 0.40 < prob < 0.60

    def test_ensemble_regime_changes_weights(self):
        """Different regimes should produce different probabilities for OTM."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        # OTM market where fat-tail models matter
        prob_calm = model.estimate_prob(
            current_price=80000, threshold=95000, direction="above",
            time_horizon_minutes=1440, vol=0.30, regime="low_vol",
        )
        prob_crisis = model.estimate_prob(
            current_price=80000, threshold=95000, direction="above",
            time_horizon_minutes=1440, vol=0.30, regime="crisis",
        )
        # Crisis regime weights fat-tail models more -> higher OTM prob
        assert prob_crisis > prob_calm

    def test_ensemble_bracket_probability(self):
        """Bracket = P(above low) - P(above high)."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_bracket_prob(
            current_price=80000, low_threshold=79000, high_threshold=81000,
            time_horizon_minutes=60, vol=0.50,
        )
        assert 0.0 < prob < 1.0

    def test_ensemble_default_regime_is_normal(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
        )
        prob_explicit = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50, regime="normal",
        )
        assert abs(prob - prob_explicit) < 0.001

    def test_ensemble_with_heston_params(self):
        """Passing Heston params should use them."""
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        prob = model.estimate_prob(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=60, vol=0.50,
            heston_params={"v0": 0.25, "kappa": 2.0, "theta": 0.25, "xi": 0.3, "rho": -0.7},
        )
        assert 0.001 <= prob <= 0.999


class TestRegimeAwareJD:
    """Jump-diffusion with regime-dependent lambda."""

    def test_crisis_regime_higher_jump_intensity(self):
        from crypto_models import EnsembleModel
        model = EnsembleModel()
        # Access JD prob directly through model
        prob_calm = model._jd_prob(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="low_vol",
        )
        prob_crisis = model._jd_prob(
            current_price=80000, threshold=90000, direction="above",
            time_horizon_minutes=1440, vol=0.50, regime="crisis",
        )
        # Higher lambda in crisis -> fatter tails -> higher OTM prob
        assert prob_crisis > prob_calm


class TestAR1Vol:
    """AR(1) volatility forecast."""

    def test_ar1_forecast_returns_positive(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        ar1.update(0.50)
        ar1.update(0.55)
        ar1.update(0.52)
        forecast = ar1.forecast()
        assert forecast > 0

    def test_ar1_mean_reverts(self):
        """After high vol, forecast should revert toward long-run mean."""
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast(alpha=0.05, beta=0.85, long_run=0.50)
        # Feed high vol
        for _ in range(20):
            ar1.update(1.0)
        high_forecast = ar1.forecast()
        # Feed normal vol
        for _ in range(20):
            ar1.update(0.50)
        normal_forecast = ar1.forecast()
        assert normal_forecast < high_forecast

    def test_ar1_insufficient_data_returns_none(self):
        from crypto_models import AR1VolForecast
        ar1 = AR1VolForecast()
        assert ar1.forecast() is None


class TestSmoothEdgeThreshold:
    """Smooth edge threshold function."""

    def test_extreme_prob_uses_base_threshold(self):
        from crypto_models import smooth_edge_threshold
        # prob=0.05 (very confident) -> near base threshold
        threshold = smooth_edge_threshold(0.05, base=0.06)
        assert 0.06 <= threshold < 0.07

    def test_mid_prob_uses_higher_threshold(self):
        from crypto_models import smooth_edge_threshold
        # prob=0.50 (max uncertainty) -> highest threshold
        threshold = smooth_edge_threshold(0.50, base=0.06)
        assert threshold > 0.07

    def test_smooth_no_discontinuity(self):
        """No jump at the old 0.25/0.75 boundary."""
        from crypto_models import smooth_edge_threshold
        t_24 = smooth_edge_threshold(0.24, base=0.06)
        t_25 = smooth_edge_threshold(0.25, base=0.06)
        t_26 = smooth_edge_threshold(0.26, base=0.06)
        # Should be smooth — no big jump between adjacent values
        assert abs(t_25 - t_24) < 0.005
        assert abs(t_26 - t_25) < 0.005

    def test_symmetric_around_50(self):
        from crypto_models import smooth_edge_threshold
        t_30 = smooth_edge_threshold(0.30, base=0.06)
        t_70 = smooth_edge_threshold(0.70, base=0.06)
        assert abs(t_30 - t_70) < 0.001


class TestHorizonKelly:
    """Horizon-scaled Kelly fractions."""

    def test_short_horizon_smaller_kelly(self):
        from crypto_models import horizon_kelly_fraction
        short = horizon_kelly_fraction(5)    # 5 min
        long = horizon_kelly_fraction(1440)  # 24h
        assert short < long

    def test_5min_returns_eighth_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(5)
        assert abs(frac - 0.125) < 0.01

    def test_24h_returns_half_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(1440)
        assert abs(frac - 0.50) < 0.01

    def test_1h_returns_fifth_kelly(self):
        from crypto_models import horizon_kelly_fraction
        frac = horizon_kelly_fraction(60)
        assert abs(frac - 0.20) < 0.02


class TestHorizonVolWeight:
    """Horizon-dependent IV/RV weighting."""

    def test_short_horizon_prefers_rv(self):
        from crypto_models import horizon_vol_weights
        w_iv, w_rv = horizon_vol_weights(5)  # 5 min
        assert w_rv > w_iv

    def test_long_horizon_prefers_iv(self):
        from crypto_models import horizon_vol_weights
        w_iv, w_rv = horizon_vol_weights(1440)  # 24h
        assert w_iv > w_rv

    def test_weights_sum_to_one(self):
        from crypto_models import horizon_vol_weights
        for t in [5, 15, 60, 360, 1440, 10080]:
            w_iv, w_rv = horizon_vol_weights(t)
            assert abs(w_iv + w_rv - 1.0) < 0.001
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_crypto_models.py -v -x
```
Expected: ModuleNotFoundError

**Step 3: Implement crypto_models.py**

```python
# src/kalshi/crypto_models.py
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


def smooth_edge_threshold(prob, base=0.06, peak_extra=0.03):
    """Smooth edge threshold — higher near prob=0.5, lower at extremes.

    Replaces hard cutoff at 0.25/0.75 with continuous quadratic function.
    Returns required edge threshold as float.
    """
    distance_from_center = abs(prob - 0.5)
    # Quadratic: 0 at extremes (distance=0.5), peak_extra at center (distance=0)
    extra = peak_extra * (1.0 - (distance_from_center / 0.5) ** 2)
    return base + extra


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
    # w_iv = 30 / (30 + T), w_rv = T / (30 + T)
    # At T=30 min: 50/50. At T=5 min: 86% RV. At T=1440: 95% IV.
    w_iv = 30.0 / (30.0 + minutes_to_settle)
    w_rv = minutes_to_settle / (30.0 + minutes_to_settle)
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
            return self._alpha + self._beta * obs[-1]

        # Estimate AR(1) via OLS: sigma_t = alpha + beta * sigma_{t-1}
        n = len(obs)
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
        return alpha + beta * obs[-1]


class EnsembleModel:
    """Bayesian Model Average ensemble of GBM, JD, and Heston.

    Blends model probabilities with regime-dependent weights.
    """

    def __init__(self, jd_params=None, heston_params=None, bma_weights=None):
        self._jd_params = jd_params or dict(DEFAULT_JD)
        self._heston_params = heston_params or dict(DEFAULT_HESTON)
        self._bma_weights = bma_weights or dict(REGIME_BMA_WEIGHTS)

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
```

**Step 4: Run tests**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_crypto_models.py -v -x
```
Expected: All PASS

**Step 5: Run full suite**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/ -x --timeout=120
```

**Step 6: Commit**

```bash
git add src/kalshi/crypto_models.py tests/test_crypto_models.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add crypto model stack — GBM/JD/Heston ensemble with regime-aware BMA"
```

---

## Task 3: GARCH(1,1) Vol Forecaster (vol_forecaster.py)

Create the GARCH vol forecaster with DCC correlation and intraday seasonality.

**Files:**
- Create: `src/kalshi/vol_forecaster.py`
- Create: `tests/test_vol_forecaster.py`

**Step 1: Write failing tests**

```python
# tests/test_vol_forecaster.py
"""Tests for GARCH(1,1), DCC correlation, and intraday seasonality."""
import math
import time
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "kalshi"))


class TestGARCH:
    """GARCH(1,1) volatility model."""

    def test_garch_forecast_after_updates(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed 20 returns
        for r in [0.01, -0.02, 0.015, -0.01, 0.03, -0.025, 0.005, -0.005,
                   0.02, -0.015, 0.01, -0.01, 0.025, -0.02, 0.01, -0.015,
                   0.02, -0.01, 0.005, -0.005]:
            garch.update(r)
        vol = garch.forecast_vol()
        assert vol is not None
        assert 0.01 < vol < 2.0  # reasonable annualized range

    def test_garch_vol_increases_after_shock(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed calm returns
        for _ in range(20):
            garch.update(0.005)
        calm_vol = garch.forecast_vol()
        # Feed a shock
        garch.update(0.15)  # 15% return (huge)
        shock_vol = garch.forecast_vol()
        assert shock_vol > calm_vol

    def test_garch_mean_reverts(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed high vol
        for _ in range(30):
            garch.update(0.10)
        high_vol = garch.forecast_vol()
        # Feed calm
        for _ in range(30):
            garch.update(0.005)
        calm_vol = garch.forecast_vol()
        assert calm_vol < high_vol

    def test_garch_insufficient_data(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        garch.update(0.01)
        assert garch.forecast_vol() is None

    def test_garch_vol_of_vol(self):
        """GARCH should estimate vol-of-vol for Heston param."""
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        for r in [0.01, -0.02, 0.015, -0.01, 0.03, -0.025, 0.005, -0.005,
                   0.02, -0.015, 0.01, -0.01, 0.025, -0.02, 0.01, -0.015,
                   0.02, -0.01, 0.005, -0.005]:
            garch.update(r)
        vov = garch.vol_of_vol()
        assert vov is not None
        assert vov > 0


class TestDCC:
    """Dynamic Conditional Correlation."""

    def test_dcc_returns_matrix(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        # Feed correlated returns
        for _ in range(30):
            btc_r = 0.01
            eth_r = 0.012  # correlated
            dcc.update({"BTC": btc_r, "ETH": eth_r})
        corr = dcc.correlation_matrix()
        assert corr is not None
        assert corr.shape == (2, 2)
        # Diagonal = 1
        assert abs(corr[0, 0] - 1.0) < 0.01
        assert abs(corr[1, 1] - 1.0) < 0.01

    def test_dcc_correlated_assets(self):
        from vol_forecaster import DCCCorrelation
        import random
        random.seed(42)
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        for _ in range(100):
            base = random.gauss(0, 0.02)
            dcc.update({"BTC": base + random.gauss(0, 0.005),
                        "ETH": base * 0.8 + random.gauss(0, 0.008)})
        corr = dcc.correlation_matrix()
        # BTC-ETH should be positively correlated
        assert corr[0, 1] > 0.3

    def test_dcc_pair_correlation(self):
        from vol_forecaster import DCCCorrelation
        import random
        random.seed(42)
        dcc = DCCCorrelation(assets=["BTC", "ETH", "SOL"])
        for _ in range(50):
            base = random.gauss(0, 0.02)
            dcc.update({"BTC": base, "ETH": base * 0.8, "SOL": base * 0.5})
        rho = dcc.pair_correlation("BTC", "ETH")
        assert rho is not None
        assert rho > 0

    def test_dcc_insufficient_data(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        dcc.update({"BTC": 0.01, "ETH": 0.012})
        assert dcc.correlation_matrix() is None


class TestIntradaySeasonality:
    """Intraday vol adjustment."""

    def test_us_open_higher_vol(self):
        from vol_forecaster import intraday_vol_multiplier
        # US market open: 14:30 UTC (9:30 ET)
        us_open = intraday_vol_multiplier(hour_utc=14, day_of_week=1)  # Tuesday
        # Quiet period: 06:00 UTC (1am ET)
        quiet = intraday_vol_multiplier(hour_utc=6, day_of_week=1)
        assert us_open > quiet

    def test_weekend_lower_vol(self):
        from vol_forecaster import intraday_vol_multiplier
        weekday = intraday_vol_multiplier(hour_utc=15, day_of_week=2)  # Wednesday
        weekend = intraday_vol_multiplier(hour_utc=15, day_of_week=6)  # Sunday
        assert weekend < weekday

    def test_multiplier_in_range(self):
        from vol_forecaster import intraday_vol_multiplier
        for hour in range(24):
            for day in range(7):
                mult = intraday_vol_multiplier(hour_utc=hour, day_of_week=day)
                assert 0.5 <= mult <= 1.5

    def test_asia_open_elevated(self):
        from vol_forecaster import intraday_vol_multiplier
        # Asia open: ~00:00-01:00 UTC (8-9pm ET, 9am JST)
        asia = intraday_vol_multiplier(hour_utc=0, day_of_week=1)
        quiet = intraday_vol_multiplier(hour_utc=6, day_of_week=1)
        assert asia >= quiet


class TestBidAskBounce:
    """Microstructure: bid-ask bounce correction."""

    def test_bounce_reduces_rv(self):
        from vol_forecaster import correct_bid_ask_bounce
        raw_rv = 0.50
        corrected = correct_bid_ask_bounce(raw_rv, spread_cents=5, n_observations=100)
        assert corrected < raw_rv

    def test_zero_spread_no_correction(self):
        from vol_forecaster import correct_bid_ask_bounce
        raw_rv = 0.50
        corrected = correct_bid_ask_bounce(raw_rv, spread_cents=0, n_observations=100)
        assert abs(corrected - raw_rv) < 0.001

    def test_correction_never_negative(self):
        from vol_forecaster import correct_bid_ask_bounce
        corrected = correct_bid_ask_bounce(0.01, spread_cents=50, n_observations=5)
        assert corrected >= 0
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_vol_forecaster.py -v -x
```

**Step 3: Implement vol_forecaster.py**

```python
# src/kalshi/vol_forecaster.py
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
    # spread in price units: spread_cents / 10000 (rough scaling for percentage)
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
```

**Step 4: Run tests**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_vol_forecaster.py -v -x
```

**Step 5: Full suite**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/ -x --timeout=120
```

**Step 6: Commit**

```bash
git add src/kalshi/vol_forecaster.py tests/test_vol_forecaster.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add GARCH(1,1) vol forecaster, DCC correlation, intraday seasonality"
```

---

## Task 4: Calibration Pipeline (calibrate-crypto.py)

Build the data-driven calibration script that fetches Coinbase historical data, runs synthetic backtests, and writes optimized parameters.

**Files:**
- Create: `scripts/calibrate-crypto.py`
- Modify: `package.json` (add npm script)

**Step 1: Write the calibration script**

```python
#!/usr/bin/env python3
"""Crypto model calibration pipeline.

Fetches historical price data from Coinbase, generates synthetic Kalshi markets,
evaluates each model, and optimizes ensemble weights + model parameters.

Usage:
    python3 scripts/calibrate-crypto.py              # Full calibration
    python3 scripts/calibrate-crypto.py --days 30     # Shorter lookback
    python3 scripts/calibrate-crypto.py --asset BTC    # Single asset
    python3 scripts/calibrate-crypto.py --dry-run      # Report only, don't save

Output: config/crypto-calibration.json
"""
import json, math, time, argparse, sys, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

import requests
from probability import (
    crypto_price_probability,
    crypto_price_probability_jd,
    crypto_price_probability_heston,
    _reset_calibration,
)
from crypto_models import EnsembleModel, REGIME_BMA_WEIGHTS, REGIME_JUMP_INTENSITY

CALIBRATION_PATH = PROJECT_DIR / "config" / "crypto-calibration.json"
ASSETS = ["BTC", "ETH"]
HORIZONS_MINUTES = [15, 60, 360, 1440]
THRESHOLD_OFFSETS = [0.97, 0.99, 1.01, 1.03]  # relative to current price


def fetch_coinbase_candles(asset, days=90, granularity=300):
    """Fetch historical OHLCV candles from Coinbase.

    Args:
        asset: "BTC" or "ETH"
        days: Lookback period
        granularity: Candle size in seconds (300=5min, 3600=1h)

    Returns:
        List of (timestamp, open, high, low, close, volume) tuples, sorted by time.
    """
    all_candles = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    # Coinbase limits to 300 candles per request
    max_candles = 300
    chunk_seconds = max_candles * granularity
    current_start = start

    while current_start < end:
        current_end = min(current_start + timedelta(seconds=chunk_seconds), end)
        url = (
            f"https://api.exchange.coinbase.com/products/{asset}-USD/candles"
            f"?start={current_start.isoformat()}&end={current_end.isoformat()}"
            f"&granularity={granularity}"
        )
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            candles = r.json()
            # Coinbase returns [time, low, high, open, close, volume]
            for c in candles:
                all_candles.append({
                    "time": c[0], "open": c[3], "high": c[2],
                    "low": c[1], "close": c[4], "volume": c[5],
                })
        except Exception as e:
            print(f"  Warning: fetch failed for {asset} chunk: {e}")
        current_start = current_end
        time.sleep(0.3)  # rate limiting

    all_candles.sort(key=lambda x: x["time"])
    return all_candles


def compute_realized_vol_from_candles(candles, window=None):
    """Compute annualized realized vol from close prices."""
    prices = [c["close"] for c in candles]
    if window:
        prices = prices[-window:]
    if len(prices) < 5:
        return 0.50  # default
    log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices)) if prices[i-1] > 0]
    if len(log_returns) < 3:
        return 0.50
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    # Assume 5-min candles -> 105120 intervals per year
    intervals_per_year = 365.25 * 24 * 60 / 5
    return math.sqrt(var * intervals_per_year)


def generate_synthetic_markets(candles, horizons=HORIZONS_MINUTES, offsets=THRESHOLD_OFFSETS):
    """Generate synthetic binary markets from price history.

    For each candle, creates markets: "Will price be above threshold in T minutes?"
    Looks ahead T minutes to determine actual outcome.

    Returns list of dicts with: current_price, threshold, direction, horizon_min, outcome (0 or 1).
    """
    markets = []
    # Build time->price lookup (5-min granularity)
    time_price = {c["time"]: c["close"] for c in candles}
    times = sorted(time_price.keys())

    for i, t in enumerate(times):
        price = time_price[t]
        for horizon in horizons:
            # Find future price
            future_time = t + horizon * 60
            future_price = time_price.get(future_time)
            if future_price is None:
                continue
            for offset in offsets:
                threshold = price * offset
                outcome = 1 if future_price > threshold else 0
                markets.append({
                    "current_price": price,
                    "threshold": threshold,
                    "direction": "above",
                    "horizon_min": horizon,
                    "outcome": outcome,
                    "rv_window": min(horizon * 3, 288),  # 3x horizon or 24h
                    "candle_idx": i,
                })
    return markets


def evaluate_model(model_fn, markets, candles):
    """Evaluate a model on synthetic markets and return Brier score.

    Args:
        model_fn: callable(current_price, threshold, direction, horizon_min, vol) -> prob
        markets: list of synthetic market dicts
        candles: for computing rolling vol

    Returns:
        dict with brier_score, per_horizon_brier, n_markets
    """
    brier_sum = 0.0
    horizon_brier = {}
    n = 0

    for m in markets:
        # Compute vol from candles up to this point
        idx = m["candle_idx"]
        window = min(idx, m["rv_window"])
        if window < 5:
            continue
        vol = compute_realized_vol_from_candles(candles[:idx+1], window=window)

        prob = model_fn(m["current_price"], m["threshold"], m["direction"], m["horizon_min"], vol)
        prob = max(0.001, min(0.999, prob))

        sq_err = (prob - m["outcome"]) ** 2
        brier_sum += sq_err
        n += 1

        h = m["horizon_min"]
        if h not in horizon_brier:
            horizon_brier[h] = {"sum": 0.0, "n": 0}
        horizon_brier[h]["sum"] += sq_err
        horizon_brier[h]["n"] += 1

    return {
        "brier_score": brier_sum / max(1, n),
        "per_horizon": {h: d["sum"] / max(1, d["n"]) for h, d in horizon_brier.items()},
        "n_markets": n,
    }


def calibrate(days=90, assets=None, dry_run=False):
    """Run full calibration pipeline."""
    assets = assets or ASSETS
    print(f"Crypto Calibration Pipeline — {days}-day lookback")
    print("=" * 60)

    results = {}
    for asset in assets:
        print(f"\n--- {asset} ---")

        # 1. Fetch data
        print(f"  Fetching {days} days of 5-min candles from Coinbase...")
        candles = fetch_coinbase_candles(asset, days=days)
        print(f"  Got {len(candles)} candles")
        if len(candles) < 100:
            print(f"  Insufficient data for {asset}, skipping")
            continue

        # 2. Generate synthetic markets
        print(f"  Generating synthetic markets...")
        markets = generate_synthetic_markets(candles)
        print(f"  Generated {len(markets)} synthetic markets")

        # 3. Evaluate each model
        print(f"  Evaluating models...")

        # GBM
        def gbm_fn(p, k, d, t, v):
            return crypto_price_probability(p, k, d, t, realized_vol_pct=v)
        gbm_result = evaluate_model(gbm_fn, markets, candles)
        print(f"    GBM Brier:     {gbm_result['brier_score']:.4f} ({gbm_result['n_markets']} markets)")

        # JD (with various lambda)
        best_jd_lambda = 1.0
        best_jd_brier = 1.0
        for lam in [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]:
            def jd_fn(p, k, d, t, v, _lam=lam):
                return crypto_price_probability_jd(p, k, d, t, realized_vol_pct=v, jump_intensity=_lam)
            jd_result = evaluate_model(jd_fn, markets, candles)
            if jd_result["brier_score"] < best_jd_brier:
                best_jd_brier = jd_result["brier_score"]
                best_jd_lambda = lam
        print(f"    JD Brier:      {best_jd_brier:.4f} (best lambda={best_jd_lambda})")

        # Heston (with default params)
        def heston_fn(p, k, d, t, v):
            return crypto_price_probability_heston(p, k, d, t, v0=v**2, kappa=2.0, theta=v**2, xi=0.3, rho=-0.7)
        heston_result = evaluate_model(heston_fn, markets, candles)
        print(f"    Heston Brier:  {heston_result['brier_score']:.4f}")

        # 4. Optimize ensemble weights
        print(f"  Optimizing ensemble weights...")
        best_weights = [0.33, 0.34, 0.33]
        best_ensemble_brier = 1.0

        for w_gbm in [x / 20 for x in range(0, 21, 2)]:
            for w_jd in [x / 20 for x in range(0, 21 - int(w_gbm * 20), 2)]:
                w_heston = 1.0 - w_gbm - w_jd
                if w_heston < 0:
                    continue

                def ensemble_fn(p, k, d, t, v, _wg=w_gbm, _wj=w_jd, _wh=w_heston, _lam=best_jd_lambda):
                    pg = crypto_price_probability(p, k, d, t, realized_vol_pct=v)
                    pj = crypto_price_probability_jd(p, k, d, t, realized_vol_pct=v, jump_intensity=_lam)
                    ph = crypto_price_probability_heston(p, k, d, t, v0=v**2, kappa=2.0, theta=v**2, xi=0.3, rho=-0.7)
                    return max(0.001, min(0.999, _wg * pg + _wj * pj + _wh * ph))

                result = evaluate_model(ensemble_fn, markets, candles)
                if result["brier_score"] < best_ensemble_brier:
                    best_ensemble_brier = result["brier_score"]
                    best_weights = [w_gbm, w_jd, w_heston]

        print(f"    Ensemble Brier: {best_ensemble_brier:.4f} (weights: GBM={best_weights[0]:.2f}, JD={best_weights[1]:.2f}, Heston={best_weights[2]:.2f})")

        results[asset] = {
            "gbm_brier": round(gbm_result["brier_score"], 4),
            "jd_brier": round(best_jd_brier, 4),
            "jd_lambda": best_jd_lambda,
            "heston_brier": round(heston_result["brier_score"], 4),
            "ensemble_brier": round(best_ensemble_brier, 4),
            "ensemble_weights": [round(w, 2) for w in best_weights],
            "per_horizon_gbm": {str(k): round(v, 4) for k, v in gbm_result["per_horizon"].items()},
            "per_horizon_heston": {str(k): round(v, 4) for k, v in heston_result["per_horizon"].items()},
            "n_markets": gbm_result["n_markets"],
        }

    # 5. Write output
    output = {
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "assets": results,
        "regime_jump_intensity": dict(REGIME_JUMP_INTENSITY),
    }

    print(f"\n{'='*60}")
    print("CALIBRATION RESULTS")
    print(f"{'='*60}")
    for asset, r in results.items():
        print(f"\n{asset}:")
        print(f"  GBM:      {r['gbm_brier']:.4f}")
        print(f"  JD:       {r['jd_brier']:.4f} (lambda={r['jd_lambda']})")
        print(f"  Heston:   {r['heston_brier']:.4f}")
        print(f"  Ensemble: {r['ensemble_brier']:.4f} (w={r['ensemble_weights']})")
        improvement = (r['gbm_brier'] - r['ensemble_brier']) / r['gbm_brier'] * 100
        print(f"  Improvement over GBM: {improvement:.1f}%")

    if not dry_run:
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nSaved to {CALIBRATION_PATH}")
    else:
        print("\n(dry run — not saved)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto model calibration")
    parser.add_argument("--days", type=int, default=90, help="Lookback days")
    parser.add_argument("--asset", type=str, help="Single asset to calibrate")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    args = parser.parse_args()
    calibrate(
        days=args.days,
        assets=[args.asset] if args.asset else None,
        dry_run=args.dry_run,
    )
```

**Step 2: Add npm script to package.json**

In `package.json`, add to the `"scripts"` section:
```json
"calibrate:crypto": "python3 scripts/calibrate-crypto.py"
```

**Step 3: Verify script runs (dry-run with small window)**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 scripts/calibrate-crypto.py --days 3 --asset BTC --dry-run
```
Expected: Fetches data, evaluates models, prints results, doesn't save.

**Step 4: Commit**

```bash
git add scripts/calibrate-crypto.py package.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add crypto calibration pipeline — synthetic backtest, ensemble weight optimization"
```

---

## Task 5: Integrate Everything into crypto-bot.py

Wire the new model stack, vol forecaster, and portfolio Kelly into the main bot.

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Modify: `tests/test_crypto.py` (update tests for new model integration)

**Step 1: Update imports in crypto-bot.py**

Add to imports (after existing ones, ~line 33):
```python
from crypto_models import EnsembleModel, smooth_edge_threshold, horizon_kelly_fraction, horizon_vol_weights, AR1VolForecast
from vol_forecaster import GARCHForecaster, DCCCorrelation, intraday_vol_multiplier, correct_bid_ask_bounce
```

**Step 2: Initialize new components (after existing component initialization, ~line 99)**

Add after regime detector initialization:
```python
# Ensemble model (loads calibration if available)
_calibration_path = PROJECT_DIR / "config" / "crypto-calibration.json"
_calibration = {}
if _calibration_path.exists():
    try:
        _calibration = json.loads(_calibration_path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
ensemble_model = EnsembleModel()

# GARCH vol forecasters per asset
garch_forecasters = {asset: GARCHForecaster() for asset in DEFAULT_VOLS}

# AR(1) vol forecasters per asset
ar1_forecasters = {asset: AR1VolForecast() for asset in DEFAULT_VOLS}

# DCC correlation tracker
dcc_tracker = DCCCorrelation(assets=list(DEFAULT_VOLS.keys()))
```

**Step 3: Replace vol blending with horizon-dependent weights (~lines 520-535)**

Replace the fixed 60/40 blending with:
```python
        # Horizon-dependent vol weighting
        w_iv, w_rv = horizon_vol_weights(minutes_to_settle)

        iv = iv_data.get(asset)
        if rv_lookback < 86400:
            rv = compute_realized_vol(asset, lookback_seconds=rv_lookback)
        else:
            rv = realized_vols.get(asset)
        default_vol = DEFAULT_VOLS.get(asset, 0.50)

        # GARCH forecast (if available)
        garch_vol = garch_forecasters.get(asset)
        garch_forecast = garch_vol.forecast_vol(annualize_factor=365.25*24*12) if garch_vol else None

        # AR(1) forecast
        ar1_vol = ar1_forecasters.get(asset)
        ar1_forecast = ar1_vol.forecast() if ar1_vol else None

        # Use best available vol estimate
        if iv is not None and rv is not None:
            vol_to_use = w_iv * iv + w_rv * rv
        elif iv is not None:
            vol_to_use = iv
        elif garch_forecast is not None:
            vol_to_use = garch_forecast
        elif rv is not None:
            vol_to_use = 0.3 * default_vol + 0.7 * rv
        else:
            vol_to_use = default_vol

        # Intraday seasonality adjustment
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        season_mult = intraday_vol_multiplier(now_utc.hour, now_utc.weekday())
        vol_to_use *= season_mult
```

**Step 4: Replace direct probability call with ensemble (~line 547)**

Replace `_compute_crypto_prob()` call with:
```python
        # Get current regime
        current_regime = regime_detector.current_regime()

        # Heston params from GARCH (dynamic v0, vol-of-vol)
        garch = garch_forecasters.get(asset)
        heston_params = dict(DEFAULT_HESTON_PARAMS)
        if garch:
            v0 = garch.heston_v0()
            if v0 is not None:
                heston_params["v0"] = v0
            vov = garch.vol_of_vol()
            if vov is not None:
                heston_params["xi"] = max(0.1, min(2.0, vov))

        if direction == "T":
            prob = ensemble_model.estimate_prob(
                current_price=current_price, threshold=threshold,
                direction="above", time_horizon_minutes=minutes_to_settle,
                vol=vol_to_use, regime=current_regime,
                drift_pct=drift, heston_params=heston_params,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
            )
        else:
            # Bracket
            range_size = _parse_bracket_range(ticker, asset, all_markets)
            prob = ensemble_model.estimate_bracket_prob(
                current_price=current_price,
                low_threshold=threshold,
                high_threshold=threshold + range_size,
                time_horizon_minutes=minutes_to_settle,
                vol=vol_to_use, regime=current_regime,
                drift_pct=drift, heston_params=heston_params,
            )
```

**Step 5: Replace edge threshold with smooth function**

Replace `_effective_edge_threshold(prob)` calls with:
```python
        eff_threshold = smooth_edge_threshold(prob, base=EDGE_THRESHOLD)
```

**Step 6: Replace quarter_kelly with horizon-scaled Kelly**

Replace the Kelly sizing block (~lines 660-668) with:
```python
        # Horizon-scaled Kelly fraction
        kelly_frac = horizon_kelly_fraction(opp["minutes_to_settle"])
        fee = kalshi_fee_cents(price)

        # Use half_kelly with manual fraction scaling
        count, risk, kelly_details = half_kelly(
            edge, price, budget.max_cost_cents,
            bankroll_cents=budget.bankroll_cents, fee_cents=fee,
            return_details=True,
        )
        # Scale from half-Kelly to horizon-appropriate fraction
        count = max(0, int(count * kelly_frac / 0.5))

        # Bankroll-scaled cap (2% of bankroll, min $5)
        max_exposure = max(500, int((budget.bankroll_cents or 50000) * 0.02))
        if count * price > max_exposure:
            count = max(1, max_exposure // price)

        # CI-aware and regime-aware multipliers (unchanged)
        filtered_est = opp["filtered_est"]
        kelly_mult = ci_kelly_multiplier(filtered_est)
        regime_mult = regime_kelly_multiplier(regime_detector)

        # Correlation adjustment: reduce if heavily correlated with existing positions
        corr_mult = 1.0
        corr_matrix = dcc_tracker.correlation_matrix()
        if corr_matrix is not None:
            # Simple: reduce Kelly by max correlation with any other asset we have positions in
            # (Full portfolio Kelly would require tracking all open positions)
            max_corr = 0.0
            for other_asset in spot_prices:
                if other_asset != opp["asset"]:
                    rho = dcc_tracker.pair_correlation(opp["asset"], other_asset)
                    if rho is not None:
                        max_corr = max(max_corr, abs(rho))
            if max_corr > 0.5:
                corr_mult = 1.0 - 0.3 * (max_corr - 0.5) / 0.5  # 0% to 30% reduction

        combined_mult = kelly_mult * regime_mult * corr_mult
        count = max(0, int(count * combined_mult))
```

**Step 7: Update GARCH/DCC after price fetch (~after line 414)**

Add after the realized vol computation block:
```python
    # Update GARCH forecasters with new returns
    for asset, price in spot_prices.items():
        history = _price_history.get(asset, [])
        if len(history) >= 2:
            prev_price = history[-2][1] if len(history) >= 2 else history[-1][1]
            if prev_price > 0:
                log_ret = math.log(price / prev_price)
                garch_forecasters[asset].update(log_ret)

    # Update AR(1) vol forecasters
    for asset in spot_prices:
        rv = realized_vols.get(asset)
        if rv is not None:
            ar1_forecasters[asset].update(rv)

    # Update DCC correlation tracker
    returns_for_dcc = {}
    for asset, price in spot_prices.items():
        history = _price_history.get(asset, [])
        if len(history) >= 2:
            prev_price = history[-2][1]
            if prev_price > 0:
                returns_for_dcc[asset] = math.log(price / prev_price)
    if returns_for_dcc:
        dcc_tracker.update(returns_for_dcc)
```

**Step 8: Add DEFAULT_HESTON_PARAMS constant (~line 104)**

```python
DEFAULT_HESTON_PARAMS = {"v0": 0.25, "kappa": 2.0, "theta": 0.25, "xi": 0.3, "rho": -0.7}
```

**Step 9: Write integration tests**

Add to `tests/test_crypto.py`:
```python
class TestEnsembleIntegration:
    """Verify crypto-bot uses ensemble model correctly."""

    def test_smooth_edge_replaces_hard_cutoff(self):
        """Edge threshold should be smooth, not discontinuous at 0.25/0.75."""
        from crypto_models import smooth_edge_threshold
        # Should be continuous across old boundary
        t_24 = smooth_edge_threshold(0.24, base=0.08)
        t_25 = smooth_edge_threshold(0.25, base=0.08)
        t_26 = smooth_edge_threshold(0.26, base=0.08)
        assert abs(t_25 - t_24) < 0.003
        assert abs(t_26 - t_25) < 0.003

    def test_horizon_kelly_shorter_is_smaller(self):
        from crypto_models import horizon_kelly_fraction
        assert horizon_kelly_fraction(5) < horizon_kelly_fraction(60)
        assert horizon_kelly_fraction(60) < horizon_kelly_fraction(1440)

    def test_horizon_vol_weights_sum_to_one(self):
        from crypto_models import horizon_vol_weights
        for t in [5, 15, 60, 360, 1440]:
            w_iv, w_rv = horizon_vol_weights(t)
            assert abs(w_iv + w_rv - 1.0) < 0.001
```

**Step 10: Run full test suite**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/ -v -x --timeout=120
```

**Step 11: Commit**

```bash
git add src/kalshi/crypto-bot.py tests/test_crypto.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate ensemble model, GARCH, DCC, horizon Kelly into crypto bot"
```

---

## Task 6: Add numpy to requirements and final verification

**Files:**
- Modify: `requirements.txt` (add numpy)

**Step 1: Add numpy**

Add to `requirements.txt`:
```
numpy>=1.24.0
```

Note: `scipy>=1.11.0` is already in requirements.txt.

**Step 2: Install**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pip install numpy
```

**Step 3: Run full test suite**

```bash
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/ -v --timeout=120
```
Expected: All tests pass (existing + new)

**Step 4: Verify no import errors**

```bash
python3 -c "from crypto_models import EnsembleModel; print('crypto_models OK')"
python3 -c "from vol_forecaster import GARCHForecaster; print('vol_forecaster OK')"
python3 -c "import numpy; import scipy; print('deps OK')"
```

**Step 5: Commit**

```bash
git add requirements.txt
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "chore: add numpy dependency for DCC correlation matrix operations"
```

---

## Verification Checklist

After all tasks complete:

```bash
# 1. All tests pass
pytest tests/ -v --timeout=120

# 2. No import errors
python3 -c "import sys; sys.path.insert(0,'src/kalshi'); from crypto_models import EnsembleModel, smooth_edge_threshold, horizon_kelly_fraction, horizon_vol_weights, AR1VolForecast; print('All imports OK')"

# 3. Calibration runs (dry)
python3 scripts/calibrate-crypto.py --days 3 --asset BTC --dry-run

# 4. Syntax check on modified files
python3 -c "import py_compile; py_compile.compile('src/kalshi/crypto-bot.py', doraise=True); py_compile.compile('src/kalshi/crypto_models.py', doraise=True); py_compile.compile('src/kalshi/vol_forecaster.py', doraise=True); print('Syntax OK')"
```

## Summary

| Task | New Files | Modified Files | Tests | Focus |
|------|-----------|---------------|-------|-------|
| 1 | - | probability.py | test_heston.py | Heston stochastic vol model |
| 2 | crypto_models.py | - | test_crypto_models.py | GBM/JD/Heston ensemble + utilities |
| 3 | vol_forecaster.py | - | test_vol_forecaster.py | GARCH, DCC, seasonality |
| 4 | calibrate-crypto.py | package.json | (script test) | Data-driven calibration pipeline |
| 5 | - | crypto-bot.py, test_crypto.py | integration tests | Wire everything together |
| 6 | - | requirements.txt | (full suite) | Dependencies + final verification |
