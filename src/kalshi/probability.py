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


def _load_calibration():
    """Lazy-load config/calibration.json. Returns dict (empty if missing)."""
    global _calibration
    if _calibration is not None:
        return _calibration
    try:
        _calibration = json.loads(_CALIBRATION_PATH.read_text()) if _CALIBRATION_PATH.exists() else {}
        if not isinstance(_calibration, dict):
            _log.warning("calibration.json has wrong schema (expected dict, got %s), using defaults",
                         type(_calibration).__name__)
            _calibration = {}
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("Failed to load calibration.json, using defaults: %s", e)
        _calibration = {}
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
    global _calibration
    _calibration = {}


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
    """CDF-based confidence for info-arb trades (album sales, box office).

    observed = actual data (units sold, gross $)
    threshold = market threshold
    data_sigma_pct = uncertainty as fraction of threshold

    P(final > threshold) = Phi((observed - threshold) / (threshold * data_sigma_pct))
    """
    sigma = threshold * data_sigma_pct
    if sigma <= 0:
        return 0.5
    z = (observed - threshold) / sigma
    return _norm_cdf(z)


def album_data_sigma(day_of_week, hours_since_publication=0, source=None):
    """Day-dependent + time-decay uncertainty for album sales data.

    day_of_week: 0=Monday ... 6=Sunday
    hours_since_publication: hours since data was published (0=fresh).
        Increases sigma by 50% per 48 hours of staleness, capped at 3x.
    source: data source identifier for source-aware sigma:
        "hdd-hits-top-50" → 2% (IS the settlement source, only chart correction risk)
        "hdd-midweek-20"  → day-based (10-15%, building estimates can shift)
        "hdd-article"     → 18% (text extraction, numbers may be projections/ranges)
        None              → day-based legacy behavior (backward compatible)

    Legacy day-based defaults (source=None or unknown source):
        Mon/Tue (early projections): sigma = 15% of threshold
        Wed/Thu (mid-week updates):  sigma = 10%
        Fri+ (actual data):          sigma = 5%

    Overridden by calibration.json album_sales.sigma_by_day or
    album_sales.sigma_by_source if present.
    """
    cal = _load_calibration()
    album_cal = cal.get("album_sales", {})
    source_cal = album_cal.get("sigma_by_source", {})
    day_cal = album_cal.get("sigma_by_day", {})

    # Source-aware sigma: known sources get fixed sigma values
    if source == "hdd-hits-top-50":
        base_sigma = source_cal.get("hdd-hits-top-50", 0.02)
    elif source == "hdd-midweek-20":
        # Midweek estimates use day-based sigma (same range as legacy)
        if day_of_week <= 1:
            base_sigma = source_cal.get("hdd-midweek-20-mon-tue", day_cal.get("mon_tue", 0.15))
        elif day_of_week <= 3:
            base_sigma = source_cal.get("hdd-midweek-20-wed-thu", day_cal.get("wed_thu", 0.10))
        else:
            base_sigma = source_cal.get("hdd-midweek-20-fri-sun", day_cal.get("fri_sun", 0.05))
    elif source == "hdd-article":
        base_sigma = source_cal.get("hdd-article", 0.18)
    else:
        # Legacy behavior: day-based sigma (backward compatible)
        if day_of_week <= 1:  # Mon, Tue
            base_sigma = day_cal.get("mon_tue", 0.15)
        elif day_of_week <= 3:  # Wed, Thu
            base_sigma = day_cal.get("wed_thu", 0.10)
        else:  # Fri, Sat, Sun
            base_sigma = day_cal.get("fri_sun", 0.05)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma


def econ_nowcast_probability(nowcast_value, nowcast_sigma, threshold, direction="above", df=None):
    """CDF-based probability for economics markets (CPI, GDP, Jobs).

    Uses nowcast point estimate and its uncertainty (sigma) to compute
    P(actual > threshold) or P(actual < threshold).

    Uses Student-t distribution (default df=5) for fatter tails — CPI/GDP
    surprise prints at 3+ sigma happen far more often than Gaussian predicts.

    Args:
        nowcast_value: nowcast point estimate (e.g. 3.2% for CPI).
        nowcast_sigma: uncertainty in the nowcast (std dev, same units).
        threshold: market threshold value.
        direction: "above" for P(actual > threshold),
                   "below" for P(actual < threshold).
        df: degrees of freedom for Student-t (None = load from calibration, default 5).
            Set df >= 500 for approximately Gaussian behavior.

    Returns:
        Probability (0-1).
    """
    if nowcast_sigma <= 0:
        return 1.0 if nowcast_value > threshold else 0.0

    z = (threshold - nowcast_value) / nowcast_sigma

    # Load df from calibration if not explicitly provided
    if df is None:
        cal = _load_calibration()
        df = cal.get("economics", {}).get("df", 5)

    # Validate df range
    if not isinstance(df, (int, float)) or df < 2 or df > 500:
        df = 5

    prob_above = 1.0 - _student_t_cdf(z, df)

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


def cpi_nowcast_sigma(days_to_release, fed_ci_width=None):
    """Piecewise exponential for CPI nowcast uncertainty based on time to release.

    Returns sigma in percentage points (e.g. 0.10 = 0.10%).

    Args:
        days_to_release: Days until the CPI release date.
        fed_ci_width: Optional dynamic CI width derived from cross-measure
            dispersion (e.g. std of CPI/CoreCPI/PCE/CorePCE * 1.645).
            When provided and > 0, converts to sigma via 90% CI formula
            (sigma = fed_ci_width / 3.29) and uses it instead of the
            hardcoded exponential decay, with a floor of 0.03 to prevent
            unreasonably tight estimates.

    If config/calibration.json has cpi.sigma_by_days (from calibrate-cpi-sigma.py),
    uses empirically calibrated values. Otherwise falls back to heuristic:
    ~0.04 at release, ~0.11 at 7d, ~0.23 at 30d, ~0.37 at 107d+.
    """
    cal = _load_calibration()
    cpi_cal = cal.get("cpi", {}).get("sigma_by_days", {})
    if cpi_cal:
        key = str(min(14, max(0, days_to_release)))
        if key in cpi_cal:
            if fed_ci_width is not None and fed_ci_width > 0:
                _log.debug("Calibration sigma_by_days overrides fed_ci_width=%.4f at d=%s", fed_ci_width, key)
            return cpi_cal[key]

    # Dynamic sigma from cross-measure dispersion (when available)
    if fed_ci_width is not None and fed_ci_width > 0:
        # 90% CI = 3.29 sigma for normal distribution (z=1.645 * 2)
        dynamic_sigma = fed_ci_width / 3.29
        return max(dynamic_sigma, 0.03)

    # Fallback: piecewise exponential for empirical CPI surprise distribution
    # Calibrated against: Knotek & Zaman (2024) Cleveland Fed WP 24-06,
    # Bloomberg consensus CPI surprise sigma (~15 bps at 30d),
    # SPF error statistics (Philadelphia Fed), BLS sampling error floor.
    #
    # Two-regime model: fast decay near release (gasoline/shelter data arrives),
    # slow decay at long horizons (structural uncertainty dominates).
    d = max(0, days_to_release)
    if d <= 14:
        # Near-release: sigma decays rapidly as BLS component data arrives
        # d=0: 0.04, d=3: 0.08, d=7: 0.11, d=14: 0.14
        return 0.04 + 0.11 * (1 - math.exp(-0.15 * d))
    else:
        # Long-horizon: structural uncertainty, slower decay
        # d=14: 0.14, d=30: 0.23, d=60: 0.32, d=107: 0.37
        near_val = 0.04 + 0.11 * (1 - math.exp(-0.15 * 14))  # ~0.14 at d=14
        return near_val + 0.25 * (1 - math.exp(-0.03 * (d - 14)))


def gdp_nowcast_sigma(days_to_release):
    """Exponential decay for GDP nowcast uncertainty based on time to release.

    Returns sigma in percentage points. GDP is much noisier than CPI —
    actual GDP forecast RMSE is 0.5-1.0 pp even close to release.
    floor=0.15 at release, range=0.45, k=0.12.

    sigma = 0.15 + 0.45 * (1 - exp(-0.12 * d))
    d=0: 0.15, d=7: ~0.41, d=14: ~0.52, d=30: ~0.59
    """
    cal = _load_calibration()
    gdp_cal = cal.get("gdp", {}).get("sigma_by_days", {})
    if gdp_cal:
        key = str(min(30, max(0, days_to_release)))
        if key in gdp_cal:
            return gdp_cal[key]

    d = max(0, days_to_release)
    return 0.15 + 0.45 * (1 - math.exp(-0.12 * d))


def boxoffice_data_sigma(day_of_week, hours_since_publication=0):
    """Day-dependent + time-decay uncertainty for box office data.

    day_of_week: 0=Monday ... 6=Sunday
    hours_since_publication: hours since data was published (0=fresh).
        Increases sigma by 50% per 48 hours of staleness, capped at 3x.

    Fri/Sat (estimates): sigma = 12%
    Sun (Sunday actuals): sigma = 5%
    Mon+ (final):         sigma = 4%

    Overridden by calibration.json box_office.sigma_by_day if present.
    """
    cal = _load_calibration()
    box_cal = cal.get("box_office", {}).get("sigma_by_day", {})

    if day_of_week in (4, 5):  # Fri, Sat
        base_sigma = box_cal.get("fri_sat", 0.12)
    elif day_of_week == 6:  # Sun
        base_sigma = box_cal.get("sun", 0.05)
    else:  # Mon-Thu
        base_sigma = box_cal.get("mon_thu", 0.04)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma


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


# ─── Longshot bias model ───

# Category-specific Becker (2025) parameters: (amplitude, decay_rate)
# Amplitude: fraction of overpricing at 1 cent
# Decay rate: exponential decay per cent of price
# Becker studied aggregate bias; these category adjustments reflect
# that retail-heavy categories (sports, entertainment) show stronger bias.
LONGSHOT_BIAS_PARAMS = {
    "sports": (0.65, 0.12),       # strongest bias, slowest decay
    "entertainment": (0.60, 0.14),
    "politics": (0.45, 0.18),
    "weather": (0.30, 0.20),      # weakest bias, fastest decay
    "economics": (0.35, 0.18),
    "crypto": (0.50, 0.15),
    "default": (0.57, 0.15),      # original Becker aggregate
}


def classify_ticker_category(ticker):
    """Classify a Kalshi ticker into a longshot bias category."""
    t = ticker.upper()
    if any(x in t for x in ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXMARMAD",
                             "KXSOCCER", "KXUFC", "KXNCAA", "KXSPORT"]):
        return "sports"
    if any(x in t for x in ["KXOSCARS", "KXGRAMMYS", "KXBILLBOARD", "KXALBUM",
                             "KXMOVIE", "KXBOX", "KXFILM", "KXMUSIC", "KXENTERTAIN",
                             "KXSTREAM", "KXSPOTIFY"]):
        return "entertainment"
    if any(x in t for x in ["KXPRES", "KXSEN", "KXGOV", "KXELECT", "KXHOUSE",
                             "KXCONGRESS", "KXSCOTUS"]):
        return "politics"
    if any(x in t for x in ["KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW",
                             "KXWEATHER"]):
        return "weather"
    if any(x in t for x in ["KXCPI", "KXGDP", "KXFED", "KXJOBS", "KXRATE",
                             "KXECON", "KXINFLATION"]):
        return "economics"
    if any(x in t for x in ["KXBTC", "KXETH", "KXCRYPTO", "KXSOL", "KXDOGE", "KXXRP"]):
        return "crypto"
    return "default"


def longshot_edge(yes_price_cents, ticker="", hours_to_close=999):
    """Estimate longshot overpricing edge using category-adjusted Becker model.

    Returns the additive probability edge (implied_prob - true_prob).

    yes_price_cents: current YES price (1-15c typical longshot range)
    ticker: market ticker for category classification
    hours_to_close: hours until market closes (time decay factor)
    """
    if yes_price_cents <= 0 or yes_price_cents > 99:
        return 0.0

    category = classify_ticker_category(ticker)
    amplitude, decay_rate = LONGSHOT_BIAS_PARAMS.get(category, LONGSHOT_BIAS_PARAMS["default"])

    # Time decay: full edge only if >24h to close, decays to 50% edge at 1h
    time_factor = min(1.0, 0.5 + 0.5 * min(hours_to_close, 24) / 24)

    # Overpricing ratio: fraction by which the implied prob exceeds true prob
    overpricing_ratio = amplitude * math.exp(-decay_rate * yes_price_cents) * time_factor

    # Convert to additive edge: implied_prob - true_prob
    implied_prob = yes_price_cents / 100.0
    true_prob = implied_prob * (1.0 - overpricing_ratio)
    additive_edge = implied_prob - true_prob  # = implied_prob * overpricing_ratio

    return max(0.0, additive_edge)


# Position sizing, fee, liquidity, and limit-price helpers are re-exported
# from domain.shared.sizing for backward compatibility.
