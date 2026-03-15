"""Entertainment info-arb and data-sigma helpers extracted from probability."""

from __future__ import annotations

from domain.shared.stats import _norm_cdf


def _calibration(load_calibration_func):
    if load_calibration_func is None:
        return {}
    calibration = load_calibration_func() or {}
    return calibration if isinstance(calibration, dict) else {}


def info_arb_probability(observed, threshold, data_sigma_pct=0.05):
    """CDF-based confidence for info-arb trades (album sales, box office)."""
    sigma = threshold * data_sigma_pct
    if sigma <= 0:
        return 0.5
    z = (observed - threshold) / sigma
    return _norm_cdf(z)


def album_data_sigma(day_of_week, hours_since_publication=0, source=None, load_calibration_func=None):
    """Day-dependent + time-decay uncertainty for album sales data."""
    cal = _calibration(load_calibration_func)
    album_cal = cal.get("album_sales", {})
    source_cal = album_cal.get("sigma_by_source", {})
    day_cal = album_cal.get("sigma_by_day", {})

    if source == "hdd-hits-top-50":
        base_sigma = source_cal.get("hdd-hits-top-50", 0.02)
    elif source == "hdd-midweek-20":
        if day_of_week <= 1:
            base_sigma = source_cal.get("hdd-midweek-20-mon-tue", day_cal.get("mon_tue", 0.15))
        elif day_of_week <= 3:
            base_sigma = source_cal.get("hdd-midweek-20-wed-thu", day_cal.get("wed_thu", 0.10))
        else:
            base_sigma = source_cal.get("hdd-midweek-20-fri-sun", day_cal.get("fri_sun", 0.05))
    elif source == "hdd-article":
        base_sigma = source_cal.get("hdd-article", 0.18)
    else:
        if day_of_week <= 1:
            base_sigma = day_cal.get("mon_tue", 0.15)
        elif day_of_week <= 3:
            base_sigma = day_cal.get("wed_thu", 0.10)
        else:
            base_sigma = day_cal.get("fri_sun", 0.05)

    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)

    return base_sigma


def boxoffice_data_sigma(day_of_week, hours_since_publication=0, load_calibration_func=None):
    """Day-dependent + time-decay uncertainty for box office data."""
    cal = _calibration(load_calibration_func)
    box_cal = cal.get("box_office", {}).get("sigma_by_day", {})

    if day_of_week in (4, 5):
        base_sigma = box_cal.get("fri_sat", 0.12)
    elif day_of_week == 6:
        base_sigma = box_cal.get("sun", 0.05)
    else:
        base_sigma = box_cal.get("mon_thu", 0.04)

    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)

    return base_sigma


__all__ = [
    "album_data_sigma",
    "boxoffice_data_sigma",
    "info_arb_probability",
]
