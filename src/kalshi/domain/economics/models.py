"""Economics probability and uncertainty helpers extracted from probability."""

from __future__ import annotations

import logging
import math

from domain.shared.stats import _student_t_cdf


_log = logging.getLogger("probability")


def _calibration(load_calibration_func):
    if load_calibration_func is None:
        return {}
    calibration = load_calibration_func() or {}
    return calibration if isinstance(calibration, dict) else {}


def econ_nowcast_probability(nowcast_value, nowcast_sigma, threshold, direction="above", df=None,
                             load_calibration_func=None):
    """CDF-based probability for economics markets (CPI, GDP, Jobs)."""
    if nowcast_sigma <= 0:
        return 1.0 if nowcast_value > threshold else 0.0

    z = (threshold - nowcast_value) / nowcast_sigma

    if df is None:
        cal = _calibration(load_calibration_func)
        df = cal.get("economics", {}).get("df", 5)

    if not isinstance(df, (int, float)) or df < 2 or df > 500:
        df = 5

    prob_above = 1.0 - _student_t_cdf(z, df)

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


def cpi_nowcast_sigma(days_to_release, fed_ci_width=None, load_calibration_func=None, logger=None):
    """Piecewise exponential for CPI nowcast uncertainty based on time to release."""
    log = logger or _log
    cal = _calibration(load_calibration_func)
    cpi_cal = cal.get("cpi", {}).get("sigma_by_days", {})
    if cpi_cal:
        key = str(min(14, max(0, days_to_release)))
        if key in cpi_cal:
            if fed_ci_width is not None and fed_ci_width > 0:
                log.debug("Calibration sigma_by_days overrides fed_ci_width=%.4f at d=%s", fed_ci_width, key)
            return cpi_cal[key]

    if fed_ci_width is not None and fed_ci_width > 0:
        dynamic_sigma = fed_ci_width / 3.29
        return max(dynamic_sigma, 0.03)

    d = max(0, days_to_release)
    if d <= 14:
        return 0.04 + 0.11 * (1 - math.exp(-0.15 * d))

    near_val = 0.04 + 0.11 * (1 - math.exp(-0.15 * 14))
    return near_val + 0.25 * (1 - math.exp(-0.03 * (d - 14)))


def gdp_nowcast_sigma(days_to_release, load_calibration_func=None):
    """Exponential decay for GDP nowcast uncertainty based on time to release."""
    cal = _calibration(load_calibration_func)
    gdp_cal = cal.get("gdp", {}).get("sigma_by_days", {})
    if gdp_cal:
        key = str(min(30, max(0, days_to_release)))
        if key in gdp_cal:
            return gdp_cal[key]

    d = max(0, days_to_release)
    return 0.15 + 0.45 * (1 - math.exp(-0.12 * d))


__all__ = [
    "cpi_nowcast_sigma",
    "econ_nowcast_probability",
    "gdp_nowcast_sigma",
]
