"""Crypto probability models extracted from probability."""

from __future__ import annotations

import logging
import math

from domain.shared.stats import _norm_cdf


_log = logging.getLogger("probability")


def crypto_price_probability(current_price, threshold, direction="above",
                             time_horizon_minutes=1440, realized_vol_pct=None,
                             iv_pct=None, use_ou=False, ou_half_life_minutes=None,
                             drift_pct=0.0, ou_target=None):
    """Log-normal probability for crypto price markets (BTC/ETH)."""
    if current_price <= 0 or threshold <= 0:
        return 0.5

    if iv_pct is not None and iv_pct > 0:
        sigma = iv_pct
    elif realized_vol_pct is not None and realized_vol_pct > 0:
        sigma = realized_vol_pct
    else:
        sigma = 0.60

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        return 1.0 if current_price > threshold else 0.0

    ou_drift_adj = 0.0
    if use_ou and ou_target is not None and ou_target > 0:
        half_life = ou_half_life_minutes or 120
        theta_ou = math.log(2) / max(1, half_life)
        two_theta_T = 2 * theta_ou * time_horizon_minutes
        if two_theta_T > 1e-10:
            ou_factor = math.sqrt((1 - math.exp(-two_theta_T)) / two_theta_T)
            theta_T_min = theta_ou * time_horizon_minutes
            ou_drift_adj = (1 - math.exp(-theta_T_min)) * math.log(ou_target / current_price)
            if time_horizon_minutes > 180:
                blend = max(0.0, (300 - time_horizon_minutes) / 120)
                ou_factor = blend * ou_factor + (1 - blend) * 1.0
                ou_drift_adj *= blend
            sigma = sigma * ou_factor

    sqrt_T = math.sqrt(T)
    sigma_sqrt_T = sigma * sqrt_T

    if sigma_sqrt_T <= 0:
        return 1.0 if current_price > threshold else 0.0

    d2 = (math.log(current_price / threshold) + (drift_pct - 0.5 * sigma**2) * T + ou_drift_adj) / sigma_sqrt_T
    prob_above = _norm_cdf(d2)

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


def crypto_price_probability_jd(current_price, threshold, direction="above",
                                time_horizon_minutes=1440, realized_vol_pct=None,
                                iv_pct=None, drift_pct=0.0,
                                jump_intensity=1.0, jump_mean=-0.05,
                                jump_std=0.10, max_jumps=10):
    """Merton jump-diffusion probability for crypto price markets."""
    if current_price <= 0 or threshold <= 0:
        return 0.5

    if iv_pct is not None and iv_pct > 0:
        sigma = iv_pct
    elif realized_vol_pct is not None and realized_vol_pct > 0:
        sigma = realized_vol_pct
    else:
        sigma = 0.60

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        return 1.0 if current_price > threshold else 0.0

    k = math.exp(jump_mean + 0.5 * jump_std ** 2) - 1
    lambda_T = jump_intensity * T

    log_S_K = math.log(current_price / threshold)
    prob_above = 0.0

    for n in range(max_jumps + 1):
        if n == 0:
            poisson_p = math.exp(-lambda_T)
        else:
            log_p = n * math.log(max(lambda_T, 1e-300)) - lambda_T
            for i in range(1, n + 1):
                log_p -= math.log(i)
            poisson_p = math.exp(log_p)

        if poisson_p < 1e-15:
            break

        sigma_n_sq = sigma ** 2 + n * jump_std ** 2 / max(T, 1e-15)
        sigma_n = math.sqrt(sigma_n_sq)
        drift_n = drift_pct - jump_intensity * k + n * jump_mean / max(T, 1e-15)

        sqrt_T = math.sqrt(T)
        d2 = (log_S_K + (drift_n - 0.5 * sigma_n_sq) * T) / (sigma_n * sqrt_T)
        prob_above += poisson_p * _norm_cdf(d2)

    prob_above = max(0.0, min(1.0, prob_above))

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


def crypto_price_probability_heston(
    current_price, threshold, direction="above",
    time_horizon_minutes=1440,
    v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
    drift_pct=0.0,
):
    """Heston stochastic volatility model for crypto binary options."""
    import numpy as np
    from scipy import integrate

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if current_price <= 0 or threshold <= 0:
        if direction == "above":
            return 0.999 if current_price > threshold else 0.001
        return 0.999 if current_price < threshold else 0.001
    if T <= 0:
        if direction == "above":
            return 0.999 if current_price > threshold else 0.001
        return 0.999 if current_price < threshold else 0.001

    if 2 * kappa * theta < xi ** 2:
        _log.warning("Heston Feller violated (2κθ=%.3f < ξ²=%.3f), falling back to GBM",
                     2 * kappa * theta, xi ** 2)
        vol = math.sqrt(max(v0, 1e-10))
        return crypto_price_probability(
            current_price, threshold, direction,
            time_horizon_minutes, realized_vol_pct=vol, drift_pct=drift_pct,
        )

    S = current_price
    K = threshold
    mu = drift_pct
    x = math.log(S / K)

    def heston_cf_p2(phi):
        u = -0.5
        b = kappa

        a_val = rho * xi * 1j * phi - b
        d = np.sqrt(a_val ** 2 - xi ** 2 * (2 * u * 1j * phi - phi ** 2))

        if np.real(d) < 0:
            d = -d

        denom = -a_val + d
        if abs(denom) < 1e-15:
            g = 0.0
        else:
            g = (-a_val - d) / denom

        exp_neg_dT = np.exp(-d * T)

        C = mu * 1j * phi * T + (kappa * theta / xi ** 2) * (
            (-a_val - d) * T - 2 * np.log((1 - g * exp_neg_dT) / (1 - g + 1e-30))
        )
        D = ((-a_val - d) / xi ** 2) * (1 - exp_neg_dT) / (1 - g * exp_neg_dT + 1e-30)

        return np.exp(C + D * v0 + 1j * phi * x)

    def integrand_p2(phi):
        cf = heston_cf_p2(phi)
        return np.real(cf / (1j * phi))

    upper = max(200, min(1000, 50 / math.sqrt(v0 * T + 1e-10)))

    try:
        int2, _ = integrate.quad(integrand_p2, 1e-8, upper, limit=150)
        P2 = 0.5 + int2 / math.pi
    except Exception:
        _log.warning("Heston integration failed (v0=%.3f, xi=%.3f, rho=%.3f), falling back to GBM",
                     v0, xi, rho)
        vol = math.sqrt(max(v0, 1e-10))
        return crypto_price_probability(
            current_price, threshold, direction,
            time_horizon_minutes, realized_vol_pct=vol, drift_pct=drift_pct,
        )

    prob_above = max(0.001, min(0.999, P2))

    if direction == "above":
        return prob_above
    return max(0.001, min(0.999, 1.0 - prob_above))


__all__ = [
    "crypto_price_probability",
    "crypto_price_probability_heston",
    "crypto_price_probability_jd",
]
