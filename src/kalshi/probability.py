"""Shared probability models and position sizing for Kalshi trading bots.

CDF-based probability estimation for weather, info-arb, and NWS markets,
plus generalized half-Kelly position sizing. Uses math.erf for normal CDF
to avoid a scipy dependency.
"""

import datetime
import json
import logging
import math
from pathlib import Path

from domain.shared.sizing import (
    KALSHI_FEE_RATE,
    MAX_SPREAD_FOR_ENTRY,
    MIN_LIQUIDITY_VOLUME,
    apply_kelly_multipliers,
    compute_limit_price,
    half_kelly,
    half_kelly_sell,
    high_conviction_kelly,
    is_market_liquid,
    kalshi_fee_cents,
    quarter_kelly,
    quarter_kelly_sell,
    uncertainty_kelly,
)
from domain.crypto.models import (
    crypto_price_probability as _crypto_price_probability_impl,
    crypto_price_probability_heston as _crypto_price_probability_heston_impl,
    crypto_price_probability_jd as _crypto_price_probability_jd_impl,
)
from domain.entertainment.models import (
    album_data_sigma as _album_data_sigma_impl,
    boxoffice_data_sigma as _boxoffice_data_sigma_impl,
    info_arb_probability as _info_arb_probability_impl,
)
from domain.economics.models import (
    cpi_nowcast_sigma as _cpi_nowcast_sigma_impl,
    econ_nowcast_probability as _econ_nowcast_probability_impl,
    gdp_nowcast_sigma as _gdp_nowcast_sigma_impl,
)
from domain.longshot.models import (
    LONGSHOT_BIAS_PARAMS,
    classify_ticker_category as _classify_ticker_category_impl,
    longshot_edge as _longshot_edge_impl,
)
from domain.shared.stats import _norm_cdf, _skew_normal_cdf, _student_t_cdf
from domain.weather.models import (
    compute_adaptive_ensemble_weights as _compute_adaptive_ensemble_weights_impl,
    ensemble_disagreement_score as _ensemble_disagreement_score_impl,
    ensemble_spread_sigma_multiplier as _ensemble_spread_sigma_multiplier_impl,
    ensemble_weather_probability as _ensemble_weather_probability_impl,
    ensemble_weather_probability_v2 as _ensemble_weather_probability_v2_impl,
    empirical_ensemble_probability as _empirical_ensemble_probability_impl,
    nws_probability as _nws_probability_impl,
    nws_sigma_for_hour as _nws_sigma_for_hour_impl,
    weather_probability as _weather_probability_impl,
    weather_sigma as _weather_sigma_impl,
    weather_sigma_hourly as _weather_sigma_hourly_impl,
)

_log = logging.getLogger("probability")


def _norm_pdf(x):
    """Standard normal PDF. phi(x) = exp(-x^2/2) / sqrt(2*pi)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _probit(p):
    """Inverse normal CDF (probit function) via Acklam rational approximation.

    Accurate to ~1e-4 for p in (0.001, 0.999). Returns z such that
    _norm_cdf(z) ≈ p. Used for price-space sigma in market maker.

    Args:
        p: probability in (0, 1).

    Returns:
        z-score (float). Clamped for p outside (0.001, 0.999).
    """
    # Clamp to avoid numerical issues at extremes
    p = max(0.001, min(0.999, p))

    # Acklam rational approximation coefficients
    a1 = -3.969683028665376e+01
    a2 = 2.209460984245205e+02
    a3 = -2.759285104469687e+02
    a4 = 1.383577518672690e+02
    a5 = -3.066479806614716e+01
    a6 = 2.506628277459239e+00

    b1 = -5.447609879822406e+01
    b2 = 1.615858368580409e+02
    b3 = -1.556989798598866e+02
    b4 = 6.680131188771972e+01
    b5 = -1.328068155288572e+01

    c1 = -7.784894002430293e-03
    c2 = -3.223964580411365e-01
    c3 = -2.400758277161838e+00
    c4 = -2.549732539343734e+00
    c5 = 4.374664141464968e+00
    c6 = 2.938163982698783e+00

    d1 = 7.784695709041462e-03
    d2 = 3.224671290700398e-01
    d3 = 2.445134137142996e+00
    d4 = 3.754408661907416e+00

    p_low = 0.02425
    p_high = 1 - p_low

    if p < p_low:
        # Rational approximation for lower region
        q = math.sqrt(-2 * math.log(p))
        return (((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / \
               ((((d1*q + d2)*q + d3)*q + d4)*q + 1)
    elif p <= p_high:
        # Rational approximation for central region
        q = p - 0.5
        r = q * q
        return (((((a1*r + a2)*r + a3)*r + a4)*r + a5)*r + a6) * q / \
               (((((b1*r + b2)*r + b3)*r + b4)*r + b5)*r + 1)
    else:
        # Rational approximation for upper region (symmetry)
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / \
                ((((d1*q + d2)*q + d3)*q + d4)*q + 1)


# ─── Calibration loading ───

_CALIBRATION_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "calibration.json"
_calibration = None
_calibration_source = None
_CALIBRATION_MISSING = object()


def _calibration_fingerprint():
    """Return a stable cache key for the current calibration artifact contents."""
    path_key = str(_CALIBRATION_PATH.resolve()) if _CALIBRATION_PATH.exists() else str(_CALIBRATION_PATH)
    try:
        if not _CALIBRATION_PATH.exists():
            return (path_key, _CALIBRATION_MISSING)
        return (path_key, _CALIBRATION_PATH.read_text())
    except OSError:
        return (path_key, _CALIBRATION_MISSING)


def _load_calibration():
    """Lazy-load config/calibration.json and reload it when the file changes."""
    global _calibration, _calibration_source
    # Tests sometimes pin calibration directly in memory; preserve that behavior.
    if _calibration is not None and _calibration_source is None:
        return _calibration
    fingerprint = _calibration_fingerprint()
    if _calibration is not None and _calibration_source == fingerprint:
        return _calibration
    try:
        raw_payload = fingerprint[1]
        if raw_payload is _CALIBRATION_MISSING:
            _calibration = {}
        else:
            _calibration = json.loads(raw_payload)
        if not isinstance(_calibration, dict):
            _log.warning("calibration.json has wrong schema (expected dict, got %s), using defaults",
                         type(_calibration).__name__)
            _calibration = {}
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("Failed to load calibration.json, using defaults: %s", e)
        _calibration = {}
    _calibration_source = fingerprint
    if _calibration:
        _log.info("Calibration loaded: %d cities, %d market types",
                  len(_calibration.get("weather", {}).get("per_city", {})),
                  len([k for k in _calibration if k not in ("weather",)]))
    else:
        _log.info("Calibration: using hardcoded defaults (no calibration.json)")
    return _calibration


def _reset_calibration():
    """Reset cached calibration to defaults (for testing).

    Sets _calibration to empty dict so _load_calibration() returns {}
    without re-reading calibration.json from disk. This ensures tests
    use hardcoded default sigma values regardless of what's on disk.
    """
    global _calibration, _calibration_source
    _calibration = {}
    _calibration_source = None


def check_calibration_freshness(max_age_days=7):
    """Warn if calibration is older than max_age_days. Returns age in days or None."""
    cal = _load_calibration()
    generated = cal.get("generated_at")
    if not generated:
        return None
    try:
        gen_dt = datetime.datetime.fromisoformat(generated)
        now = datetime.datetime.now(datetime.timezone.utc)
        # Handle naive timestamps from older calibration files
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=datetime.timezone.utc)
        age_days = (now - gen_dt).days
        if age_days > max_age_days:
            _log.warning("Calibration is %d days old (max %d). Run: npm run calibrate", age_days, max_age_days)
        return age_days
    except (ValueError, TypeError):
        return None


# ─── Probability models ───

def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None,
                        sigma_override=None, hour_of_day=None, skew=0.0):
    return _weather_probability_impl(
        forecast_temp,
        threshold,
        direction,
        days_out=days_out,
        city=city,
        sigma_override=sigma_override,
        hour_of_day=hour_of_day,
        skew=skew,
        load_calibration_func=_load_calibration,
        logger=_log,
    )


def weather_sigma(days_out=0, city=None):
    return _weather_sigma_impl(
        days_out=days_out,
        city=city,
        load_calibration_func=_load_calibration,
    )


def weather_sigma_hourly(days_out=0, city=None, hour_of_day=None):
    return _weather_sigma_hourly_impl(
        days_out=days_out,
        city=city,
        hour_of_day=hour_of_day,
        load_calibration_func=_load_calibration,
    )


def ensemble_weather_probability(forecasts, threshold, direction, days_out=0, city=None,
                                 sigma_multiplier=1.0, static_weights=None):
    return _ensemble_weather_probability_impl(
        forecasts,
        threshold,
        direction,
        days_out=days_out,
        city=city,
        sigma_multiplier=sigma_multiplier,
        static_weights=static_weights,
        load_calibration_func=_load_calibration,
        logger=_log,
    )


def ensemble_spread_sigma_multiplier(spread_f):
    return _ensemble_spread_sigma_multiplier_impl(spread_f)


def compute_adaptive_ensemble_weights(verification_data, default_weights=None, return_details=False):
    return _compute_adaptive_ensemble_weights_impl(
        verification_data,
        default_weights=default_weights,
        return_details=return_details,
    )


def ensemble_disagreement_score(model_probs):
    return _ensemble_disagreement_score_impl(model_probs)


def ensemble_weather_probability_v2(forecasts, threshold, direction, days_out=0, city=None,
                                    sigma_multiplier=1.0, hour_of_day=None,
                                    verification_data=None, return_details=False,
                                    static_weights=None, skew=0.0):
    return _ensemble_weather_probability_v2_impl(
        forecasts,
        threshold,
        direction,
        days_out=days_out,
        city=city,
        sigma_multiplier=sigma_multiplier,
        hour_of_day=hour_of_day,
        verification_data=verification_data,
        return_details=return_details,
        static_weights=static_weights,
        skew=skew,
        load_calibration_func=_load_calibration,
        logger=_log,
    )


def empirical_ensemble_probability(member_temps, threshold, direction, bias_offset=0.0,
                                   extra_points=None, return_details=False):
    return _empirical_ensemble_probability_impl(
        member_temps,
        threshold,
        direction,
        bias_offset=bias_offset,
        extra_points=extra_points,
        return_details=return_details,
    )


def nws_sigma_for_hour(hour_of_day):
    return _nws_sigma_for_hour_impl(
        hour_of_day,
        load_calibration_func=_load_calibration,
    )


def nws_probability(running_high, threshold, direction, hour_of_day):
    return _nws_probability_impl(
        running_high,
        threshold,
        direction,
        hour_of_day,
        load_calibration_func=_load_calibration,
        logger=_log,
    )


def info_arb_probability(observed, threshold, data_sigma_pct=0.05):
    return _info_arb_probability_impl(observed, threshold, data_sigma_pct=data_sigma_pct)


def album_data_sigma(day_of_week, hours_since_publication=0, source=None):
    return _album_data_sigma_impl(
        day_of_week,
        hours_since_publication=hours_since_publication,
        source=source,
        load_calibration_func=_load_calibration,
    )


def econ_nowcast_probability(nowcast_value, nowcast_sigma, threshold, direction="above", df=None):
    return _econ_nowcast_probability_impl(
        nowcast_value,
        nowcast_sigma,
        threshold,
        direction=direction,
        df=df,
        load_calibration_func=_load_calibration,
    )


def cpi_nowcast_sigma(days_to_release, fed_ci_width=None):
    return _cpi_nowcast_sigma_impl(
        days_to_release,
        fed_ci_width=fed_ci_width,
        load_calibration_func=_load_calibration,
        logger=_log,
    )


def gdp_nowcast_sigma(days_to_release):
    return _gdp_nowcast_sigma_impl(
        days_to_release,
        load_calibration_func=_load_calibration,
    )


def boxoffice_data_sigma(day_of_week, hours_since_publication=0):
    return _boxoffice_data_sigma_impl(
        day_of_week,
        hours_since_publication=hours_since_publication,
        load_calibration_func=_load_calibration,
    )


# ─── Gas price probability model ───

def gas_price_probability(current_price, threshold, direction="above",
                          weekly_sigma_pct=0.015, days_to_settle=7):
    """CDF probability for gas price markets.

    Uses current national avg price and historical weekly volatility (~1.5%).
    Sigma scales with sqrt(time) for different settlement horizons.

    Args:
        current_price: current AAA national average (dollars).
        threshold: market threshold (dollars).
        direction: "above" or "below".
        weekly_sigma_pct: weekly price std dev as fraction (default 1.5%).
        days_to_settle: days until market settlement (default 7).

    Returns:
        Probability (0-1).
    """
    sigma = current_price * weekly_sigma_pct * math.sqrt(max(1, days_to_settle) / 7.0)
    if sigma <= 0:
        return 1.0 if current_price > threshold else 0.0
    z = (threshold - current_price) / sigma
    prob_above = 1.0 - _norm_cdf(z)
    return prob_above if direction == "above" else 1.0 - prob_above


# ─── Kalshi fee helpers ───

# Shared fee and sizing helpers now live in domain.shared.sizing.
# Keep these names imported here so existing probability imports still work.



# ─── Crypto probability model ───

def crypto_price_probability(current_price, threshold, direction="above",
                              time_horizon_minutes=1440, realized_vol_pct=None,
                              iv_pct=None, use_ou=False, ou_half_life_minutes=None,
                              drift_pct=0.0, ou_target=None):
    return _crypto_price_probability_impl(
        current_price,
        threshold,
        direction=direction,
        time_horizon_minutes=time_horizon_minutes,
        realized_vol_pct=realized_vol_pct,
        iv_pct=iv_pct,
        use_ou=use_ou,
        ou_half_life_minutes=ou_half_life_minutes,
        drift_pct=drift_pct,
        ou_target=ou_target,
    )


def crypto_price_probability_jd(current_price, threshold, direction="above",
                                  time_horizon_minutes=1440, realized_vol_pct=None,
                                  iv_pct=None, drift_pct=0.0,
                                  jump_intensity=1.0, jump_mean=-0.05,
                                  jump_std=0.10, max_jumps=10):
    return _crypto_price_probability_jd_impl(
        current_price,
        threshold,
        direction=direction,
        time_horizon_minutes=time_horizon_minutes,
        realized_vol_pct=realized_vol_pct,
        iv_pct=iv_pct,
        drift_pct=drift_pct,
        jump_intensity=jump_intensity,
        jump_mean=jump_mean,
        jump_std=jump_std,
        max_jumps=max_jumps,
    )


def crypto_price_probability_heston(
    current_price, threshold, direction="above",
    time_horizon_minutes=1440,
    v0=0.25, kappa=2.0, theta=0.25, xi=0.3, rho=-0.7,
    drift_pct=0.0,
):
    return _crypto_price_probability_heston_impl(
        current_price,
        threshold,
        direction=direction,
        time_horizon_minutes=time_horizon_minutes,
        v0=v0,
        kappa=kappa,
        theta=theta,
        xi=xi,
        rho=rho,
        drift_pct=drift_pct,
    )


def classify_ticker_category(ticker):
    return _classify_ticker_category_impl(ticker)


def longshot_edge(yes_price_cents, ticker="", hours_to_close=999):
    return _longshot_edge_impl(yes_price_cents, ticker=ticker, hours_to_close=hours_to_close)


# Position sizing, fee, liquidity, and limit-price helpers are re-exported
# from domain.shared.sizing for backward compatibility.
