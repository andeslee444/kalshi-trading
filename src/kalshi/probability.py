"""Shared probability models and position sizing for Kalshi trading bots.

CDF-based probability estimation for weather, info-arb, and NWS markets,
plus generalized half-Kelly position sizing. Uses math.erf for normal CDF
to avoid a scipy dependency.
"""

import json
import logging
import math
from pathlib import Path

_log = logging.getLogger("probability")


def _norm_cdf(x):
    """Standard normal CDF. P(Z <= x) using math.erf."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _ln_gamma(x):
    """Log-gamma via Lanczos approximation (g=7, n=9). No scipy needed."""
    if x <= 0:
        return float('inf')
    coefs = [
        0.99999999999980993,
        676.5203681218851,
        -1259.1392167224028,
        771.32342877765313,
        -176.61502916214059,
        12.507343278686905,
        -0.13857109526572012,
        9.9843695780195716e-6,
        1.5056327351493116e-7,
    ]
    if x < 0.5:
        # Reflection formula
        return math.log(math.pi / math.sin(math.pi * x)) - _ln_gamma(1 - x)
    x -= 1
    a = coefs[0]
    t = x + 7.5
    for i in range(1, 9):
        a += coefs[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _regularized_beta_cf(x, a, b, max_iter=200, tol=1e-12):
    """Regularized incomplete beta I_x(a, b) via continued fraction (Numerical Recipes)."""
    if x < 0 or x > 1:
        return 0.0
    if x == 0 or x == 1:
        return x

    # Use symmetry relation for better convergence
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _regularized_beta_cf(1 - x, b, a, max_iter, tol)

    # Prefactor: x^a * (1-x)^b / (a * B(a,b))
    ln_prefactor = a * math.log(x) + b * math.log(1 - x) - math.log(a) \
                   + _ln_gamma(a + b) - _ln_gamma(a) - _ln_gamma(b)
    prefactor = math.exp(ln_prefactor)

    # Modified Lentz continued fraction (Numerical Recipes style)
    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # Even step
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        # Odd step
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < tol:
            break

    return prefactor * h


def _student_t_cdf(x, df=6):
    """Student's t CDF using regularized incomplete beta.

    At df=6, tails are ~3x heavier than Gaussian at 3-sigma.
    Converges to _norm_cdf as df -> infinity.
    """
    if df <= 0:
        return _norm_cdf(x)
    t2 = x * x
    ix = _regularized_beta_cf(df / (df + t2), df / 2.0, 0.5)
    cdf = 0.5 * ix
    if x >= 0:
        return 1.0 - cdf
    return cdf


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
    except (json.JSONDecodeError, OSError):
        _calibration = {}
    if _calibration:
        _log.info("Calibration loaded: %d cities, %d market types",
                  len(_calibration.get("weather", {}).get("per_city", {})),
                  len([k for k in _calibration if k not in ("weather",)]))
    else:
        _log.info("Calibration: using hardcoded defaults (no calibration.json)")
    return _calibration


def _reset_calibration():
    """Reset cached calibration (for testing)."""
    global _calibration
    _calibration = None


# ─── Probability models ───

def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """CDF-based probability for KXHIGH weather markets.

    sigma scales with forecast horizon: sigma = intercept + slope * sqrt(days_out)
    Default: sigma = 2.0 + 0.5 * sqrt(days_out)
    Sublinear (sqrt) scaling matches random-walk forecast error growth.

    If config/calibration.json exists with per-city or global sigma parameters,
    those override the defaults.

    direction="T": P(actual > threshold) = 1 - Phi((threshold - forecast) / sigma)
    direction="B": P(threshold <= actual < threshold+1) = Phi((threshold+1 - forecast)/sigma) - Phi((threshold - forecast)/sigma)
    """
    cal = _load_calibration()
    intercept = 2.0  # NWS MAE data shows day-0 error ~2.0°F (was 2.5)
    slope = 0.5

    weather_cal = cal.get("weather", {})
    if city and city in weather_cal.get("per_city", {}):
        city_cal = weather_cal["per_city"][city]
        intercept = city_cal.get("sigma_intercept", intercept)
        slope = city_cal.get("sigma_slope", slope)
    elif weather_cal.get("global_sigma_intercept") is not None:
        intercept = weather_cal["global_sigma_intercept"]
        slope = weather_cal.get("global_sigma_slope", slope)

    sigma = max(0.5, intercept + slope * math.sqrt(max(0, days_out)))

    # Degrees of freedom for Student's t (fat tails for forecast errors)
    df = weather_cal.get("df", 6)
    if not isinstance(df, (int, float)) or df < 2:
        _log.warning("Invalid df=%s in calibration, using default df=6", df)
        df = 6

    if direction == "T":
        # P(actual > threshold)
        z = (threshold - forecast_temp) / sigma
        return 1.0 - _student_t_cdf(z, df)
    else:
        # B = bracket: P(threshold <= actual < threshold + 1)
        z_low = (threshold - forecast_temp) / sigma
        z_high = (threshold + 1 - forecast_temp) / sigma
        return _student_t_cdf(z_high, df) - _student_t_cdf(z_low, df)


def weather_sigma(days_out=0, city=None):
    """Return the sigma used by weather_probability for a given horizon and city.

    Useful for logging sigma_used in trade records for calibration.
    """
    cal = _load_calibration()
    intercept = 2.0
    slope = 0.5
    weather_cal = cal.get("weather", {})
    if city and city in weather_cal.get("per_city", {}):
        city_cal = weather_cal["per_city"][city]
        intercept = city_cal.get("sigma_intercept", intercept)
        slope = city_cal.get("sigma_slope", slope)
    elif weather_cal.get("global_sigma_intercept") is not None:
        intercept = weather_cal["global_sigma_intercept"]
        slope = weather_cal.get("global_sigma_slope", slope)
    return max(0.5, intercept + slope * math.sqrt(max(0, days_out)))


def ensemble_weather_probability(forecasts, threshold, direction, days_out=0, city=None):
    """Weighted ensemble averaging (linear opinion pool) for KXHIGH weather markets.

    Combines GFS, ECMWF, and ICON forecasts with calibrated weights.
    Each model produces an independent probability via weather_probability(),
    and the ensemble is a weighted average.

    Args:
        forecasts: dict mapping model name to forecast temperature (F).
                   e.g. {"gfs": 82.5, "ecmwf": 83.1, "icon": 81.8}
        threshold: market threshold temperature (F).
        direction: "T" (above threshold) or "B" (bracket).
        days_out: forecast horizon in days.
        city: city code for calibration lookup.

    Returns:
        Weighted average probability (0-1).
    """
    cal = _load_calibration()
    ensemble_cal = cal.get("ensemble", {})

    # Horizon-dependent default weights:
    # GFS outperforms at day 0-1, ECMWF outperforms at day 3-7
    if days_out <= 1:
        default_weights = {"gfs": 0.50, "ecmwf": 0.35, "icon": 0.15}
    elif days_out <= 3:
        default_weights = {"gfs": 0.35, "ecmwf": 0.45, "icon": 0.20}
    else:
        default_weights = {"gfs": 0.30, "ecmwf": 0.45, "icon": 0.25}
    # Calibration.json weights still override these defaults
    weights = ensemble_cal.get("weights", default_weights)

    total_weight = 0.0
    weighted_prob = 0.0

    for model_name, temp in forecasts.items():
        w = weights.get(model_name, 0.0)
        if w <= 0:
            continue
        prob = weather_probability(temp, threshold, direction, days_out, city=city)
        weighted_prob += w * prob
        total_weight += w

    if total_weight <= 0:
        # No valid models — return None so callers can fall back to single-model
        _log.error("ensemble_weather_probability: zero total weight for models %s", list(forecasts.keys()))
        return None

    return weighted_prob / total_weight


def nws_probability(running_high, threshold, direction, hour_of_day):
    """Probability for NWS actual-temp arbitrage (source-monitor).

    Uses a continuous exponential decay model for residual uncertainty:
      sigma = max(0.5, 4.0 * exp(-0.18 * (hour - 6)))

    This gives smooth transitions instead of discontinuous steps:
      hour  6: sigma ~4.0F (morning, full uncertainty)
      hour 12: sigma ~1.4F (midday)
      hour 15: sigma ~0.7F (afternoon, mostly locked)
      hour 17: sigma ~0.4F (evening, essentially final)
      hour 20: sigma ~0.5F (floor)

    If config/calibration.json has nws sigma_by_hour overrides, falls back
    to the legacy 3-step model for backwards compatibility.

    direction="T": P(final_high > threshold)
    direction="B": P(threshold <= final_high < threshold+1)
    """
    cal = _load_calibration()
    nws_section = cal.get("nws", {})
    nws_cal = nws_section.get("sigma_by_hour", {})
    nws_df = nws_section.get("df", 6)
    if not isinstance(nws_df, (int, float)) or nws_df < 2:
        _log.warning("Invalid nws_df=%s in calibration, using default df=6", nws_df)
        nws_df = 6

    if nws_cal:
        # Legacy step-function: use calibrated values (with overnight bucket)
        if hour_of_day < 6:
            sigma = max(0.5, nws_cal.get("overnight", 5.0))
        elif hour_of_day >= 17:
            sigma = max(0.5, nws_cal.get("17+", 0.5))
        elif hour_of_day >= 15:
            sigma = max(0.5, nws_cal.get("15-16", 1.5))
        else:
            sigma = max(0.5, nws_cal.get("before_15", 3.0))
    else:
        # Continuous model: exponential decay from morning uncertainty
        if hour_of_day < 6:
            sigma = max(0.5, nws_cal.get("overnight", 5.0) if nws_cal else 5.0)
        else:
            sigma = max(0.5, 4.0 * math.exp(-0.18 * (hour_of_day - 6)))

    if direction == "T":
        z = (threshold - running_high) / sigma
        return 1.0 - _student_t_cdf(z, nws_df)
    else:
        # Bracket
        z_low = (threshold - running_high) / sigma
        z_high = (threshold + 1 - running_high) / sigma
        return _student_t_cdf(z_high, nws_df) - _student_t_cdf(z_low, nws_df)


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


def album_data_sigma(day_of_week, hours_since_publication=0):
    """Day-dependent + time-decay uncertainty for album sales data.

    day_of_week: 0=Monday ... 6=Sunday
    hours_since_publication: hours since data was published (0=fresh).
        Increases sigma by 50% per 48 hours of staleness, capped at 3x.

    Mon/Tue (early projections): sigma = 15% of threshold
    Wed/Thu (mid-week updates):  sigma = 10%
    Fri+ (actual data):          sigma = 5%

    Overridden by calibration.json album_sales.sigma_by_day if present.
    """
    cal = _load_calibration()
    album_cal = cal.get("album_sales", {}).get("sigma_by_day", {})

    if day_of_week <= 1:  # Mon, Tue
        base_sigma = album_cal.get("mon_tue", 0.15)
    elif day_of_week <= 3:  # Wed, Thu
        base_sigma = album_cal.get("wed_thu", 0.10)
    else:  # Fri, Sat, Sun
        base_sigma = album_cal.get("fri_sun", 0.05)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma


def econ_nowcast_probability(nowcast_value, nowcast_sigma, threshold, direction="above"):
    """CDF-based probability for economics markets (CPI, GDP, Jobs).

    Uses nowcast point estimate and its uncertainty (sigma) to compute
    P(actual > threshold) or P(actual < threshold).

    Args:
        nowcast_value: nowcast point estimate (e.g. 3.2% for CPI).
        nowcast_sigma: uncertainty in the nowcast (std dev, same units).
        threshold: market threshold value.
        direction: "above" for P(actual > threshold),
                   "below" for P(actual < threshold).

    Returns:
        Probability (0-1).
    """
    if nowcast_sigma <= 0:
        return 1.0 if nowcast_value > threshold else 0.0

    z = (threshold - nowcast_value) / nowcast_sigma
    prob_above = 1.0 - _norm_cdf(z)

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


def cpi_nowcast_sigma(days_to_release):
    """Exponential decay for CPI nowcast uncertainty based on time to release.

    Returns sigma in percentage points (e.g. 0.10 = 0.10%).

    If config/calibration.json has cpi.sigma_by_days (from calibrate-cpi-sigma.py),
    uses empirically calibrated values. Otherwise falls back to heuristic:
    ~0.10 at 14d, ~0.04 at 7d, ~0.03 at 1d, 0.03 at release day.
    """
    cal = _load_calibration()
    cpi_cal = cal.get("cpi", {}).get("sigma_by_days", {})
    if cpi_cal:
        key = str(min(14, max(0, days_to_release)))
        if key in cpi_cal:
            return cpi_cal[key]

    # Fallback heuristic: step function matching documented behavior
    # ~0.10% at 14d, ~0.06% at 7d, ~0.04% at 3d, 0.03% at 1d/release
    if days_to_release <= 0:
        return 0.03
    elif days_to_release <= 1:
        return 0.03
    elif days_to_release <= 3:
        return 0.04
    elif days_to_release <= 7:
        return 0.06
    elif days_to_release <= 14:
        return 0.10
    else:
        return 0.10


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

KALSHI_FEE_RATE = 0.07  # Kalshi fee formula: 0.07 * P * (1-P)


def kalshi_fee_cents(price_cents):
    """Kalshi per-contract fee in cents. Formula: 0.07 * P * (1-P) * 100."""
    p = price_cents / 100.0
    return KALSHI_FEE_RATE * p * (1 - p) * 100


def edge_after_fees(raw_edge, price_cents):
    """DEPRECATED: Do not use for new code. Fee should reduce the payout,
    not the edge. Use raw edge + pass fee_cents to Kelly functions instead.

    This function subtracts fee as a probability delta, but the mathematically
    correct treatment is to reduce the payout (100 -> 100-fee) in the Kelly
    formula. All bots now pass fee_cents directly to half_kelly/quarter_kelly.

    Kept for backward compatibility with tests and any external callers.
    """
    fee = kalshi_fee_cents(price_cents)
    return raw_edge - fee / 100


# ─── Crypto probability model ───

def crypto_price_probability(current_price, threshold, direction="above",
                              time_horizon_minutes=1440, realized_vol_pct=None,
                              iv_pct=None, use_ou=False, ou_half_life_minutes=None,
                              drift_pct=0.0):
    """Log-normal probability for crypto price markets (BTC/ETH).

    Uses geometric Brownian motion: ln(S_T/S_0) ~ N((drift-0.5*sigma^2)*T, sigma^2*T)
    where sigma is annualized volatility and drift is annualized return rate.

    P(S_T > K) = Phi(d2) where d2 = (ln(S/K) + (drift-0.5*sigma^2)*T) / (sigma*sqrt(T))

    Args:
        current_price: current spot price (e.g. 67500 for BTC).
        threshold: market threshold price.
        direction: "above" for P(price > threshold),
                   "below" for P(price < threshold).
        time_horizon_minutes: time to settlement in minutes (default 1440 = 1 day).
        realized_vol_pct: realized annualized volatility as decimal (e.g. 0.60 = 60%).
        iv_pct: implied volatility as decimal. Takes precedence over realized.
        drift_pct: annualized drift rate as decimal (default 0.0 = risk-neutral).
                   Pass positive value for physical measure (e.g. 0.30 = 30% annual).

    Returns:
        Probability (0-1).
    """
    if current_price <= 0 or threshold <= 0:
        return 0.5

    # Select volatility: IV > realized > default
    if iv_pct is not None and iv_pct > 0:
        sigma = iv_pct
    elif realized_vol_pct is not None and realized_vol_pct > 0:
        sigma = realized_vol_pct
    else:
        sigma = 0.60  # default ~60% annualized for BTC

    # Convert time to annualized fraction (365.25 * 24 * 60 minutes per year)
    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        return 1.0 if current_price > threshold else 0.0

    # Ornstein-Uhlenbeck mean-reversion adjustment with smooth blend
    if use_ou:
        half_life = ou_half_life_minutes or 120  # default 2-hour half-life
        theta_per_min = math.log(2) / max(1, half_life)
        two_theta_T = 2 * theta_per_min * time_horizon_minutes
        if two_theta_T > 1e-10:
            ou_factor = math.sqrt((1 - math.exp(-two_theta_T)) / two_theta_T)
            # Smooth blend: full OU below 180 min, linear taper to 1.0 at 300 min
            if time_horizon_minutes > 180:
                blend = max(0.0, (300 - time_horizon_minutes) / 120)
                ou_factor = blend * ou_factor + (1 - blend) * 1.0
            sigma = sigma * ou_factor

    sqrt_T = math.sqrt(T)
    sigma_sqrt_T = sigma * sqrt_T

    if sigma_sqrt_T <= 0:
        return 1.0 if current_price > threshold else 0.0

    # d2 with configurable drift (default 0.0 = risk-neutral)
    d2 = (math.log(current_price / threshold) + (drift_pct - 0.5 * sigma**2) * T) / sigma_sqrt_T
    prob_above = _norm_cdf(d2)

    if direction == "below":
        return 1.0 - prob_above
    return prob_above


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
    if any(x in t for x in ["KXBTC", "KXETH", "KXCRYPTO", "KXSOL"]):
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


# ─── Position sizing ───

def half_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None, fee_cents=0,
               return_details=False):
    """Generalized half-Kelly position sizing for binary contracts (buy side).

    Returns (contracts, risk_cents).
    If return_details=True, returns (contracts, risk_cents, details_dict) where
    details_dict contains {"kelly_fraction": float, "bankroll_used": int}.

    edge: our_prob - market_implied_prob (positive = trade, negative = skip)
    price_cents: price we'd pay (1-99)
    max_cost_cents: max total spend per trade (from config)
    bankroll_cents: total available balance for Kelly fraction calculation.
                    IMPORTANT: should always be passed for proper sizing.
                    Without it, sizing is purely cost-cap limited (not Kelly).
    fee_cents: per-contract fee in cents (from kalshi_fee_cents()). When > 0,
               reduces the payout (100 → 100-fee) rather than the probability,
               which is the mathematically correct fee treatment for Kelly.

    When buying YES at price p:
      win = (100 - fee) - p cents with prob our_prob
      lose = p cents with prob (1 - our_prob)

    When buying NO at price (100-p):
      This is equivalent — just pass the NO price as price_cents.
    """
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return _zero

    implied_prob = price_cents / 100.0
    our_prob = implied_prob + edge

    # Clamp to valid probability range
    original_prob = our_prob
    our_prob = max(0.001, min(0.999, our_prob))
    if original_prob <= 0.001 or original_prob >= 0.999:
        _log.warning("Probability clamped: %.6f → [0.001, 0.999]", original_prob)

    # Kelly fraction: f = (b*p - q) / b
    # where b = win/loss ratio, p = win prob, q = 1 - p
    # Fee reduces the payout, not the probability
    win_amount = (100 - fee_cents) - price_cents
    if win_amount <= 0:
        return _zero
    loss_amount = price_cents
    b = win_amount / loss_amount
    kelly_f = (b * our_prob - (1 - our_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return _zero

    # Max contracts from Kelly fraction (if bankroll provided)
    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / price_cents)
    else:
        max_kelly = 999999

    # Max contracts from cost cap
    max_cost = max_cost_cents // price_cents

    contracts = min(max_kelly, max_cost)
    contracts = max(0, contracts)

    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, {"kelly_fraction": round(half_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk)


def half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None, fee_cents=0,
                    return_details=False):
    """Half-Kelly for selling YES (buying NO). Returns (contracts, risk_cents).
    If return_details=True, returns (contracts, risk_cents, details_dict).

    edge: additive overpricing (implied_prob - true_prob, positive = sell signal).
          IMPORTANT: this must be an additive probability difference, NOT a
          multiplicative ratio. Use longshot_edge() to get the correct value.
    sell_price_cents: YES price we're selling at (1-99)
    max_cost_cents: max total risk (contracts * (100 - sell_price))
    bankroll_cents: total available balance for Kelly fraction calculation.
                    IMPORTANT: should always be passed for proper sizing.
    fee_cents: per-contract fee in cents. When > 0, reduces the win amount
               (sell proceeds) to reflect fee drag on the payout.

    When selling YES at price p:
      - We receive (p - fee) cents now
      - We lose (100 - p) cents if the event occurs
      - True prob of event = implied_prob - edge
    """
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or sell_price_cents <= 0 or sell_price_cents >= 100:
        return _zero

    # Market implied prob of event
    implied_prob = sell_price_cents / 100.0
    # Our estimated true probability (lower than market thinks)
    raw_p_true = implied_prob - edge
    p_true = max(0.001, min(0.999, raw_p_true))
    if raw_p_true <= 0.001 or raw_p_true >= 0.999:
        _log.warning("Probability clamped (sell): %.6f → [0.001, 0.999]", raw_p_true)

    # Selling YES: win (sell_price - fee) cents with prob (1-p_true),
    #              lose (100-sell_price) cents with prob p_true
    win_prob = 1 - p_true
    win_amount = sell_price_cents - fee_cents
    if win_amount <= 0:
        return _zero
    loss_amount = 100 - sell_price_cents

    # Kelly: f = (p*b - q) / b where b = win/loss odds ratio, p = win prob, q = 1-p
    b = win_amount / loss_amount
    kelly_f = (b * win_prob - (1 - win_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return _zero

    risk_per = 100 - sell_price_cents

    # Max contracts from Kelly fraction (if bankroll provided)
    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / risk_per)
    else:
        max_kelly = 999999

    # Max contracts from risk cap
    max_cap = max_cost_cents // risk_per

    contracts = min(max_kelly, max_cap)
    contracts = max(0, contracts)

    risk_cents = contracts * risk_per
    if return_details:
        return (contracts, risk_cents, {"kelly_fraction": round(half_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk_cents)


def quarter_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None,
                   max_exposure_cents=None, fee_cents=0, return_details=False):
    """Quarter-Kelly for bracket markets (higher model uncertainty).

    Bracket markets (1-degree windows) have much higher forecast sensitivity
    than threshold markets. A 1°F error can move bracket prob from 16% to 3%.
    Uses quarter-Kelly (half of half-Kelly) and hard-caps exposure.

    max_exposure_cents: hard cap on total position cost.
                        Default scales with bankroll: max($5, 5% of bankroll).
    fee_cents: per-contract fee in cents, passed through to half_kelly.
    If return_details=True, returns (contracts, risk_cents, details_dict).
    """
    if max_exposure_cents is None:
        max_exposure_cents = max(500, int((bankroll_cents or 10000) * 0.05))
    result = half_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                        fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    # Halve the half-Kelly position (= quarter-Kelly)
    contracts = contracts // 2
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    # Hard-cap bracket exposure
    if contracts * price_cents > max_exposure_cents:
        contracts = max_exposure_cents // price_cents
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)


def high_conviction_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None,
                           fee_cents=0, return_details=False):
    """60% Kelly for high-conviction threshold-NO trades (>80% model prob).

    When the weather model strongly favors NO on a threshold market,
    historical data shows 76.4% ROI. Scale from half-Kelly (50%) to
    60%-Kelly to capture more value from the strongest signals.

    fee_cents: per-contract fee in cents. When > 0, reduces the payout
               (100 → 100-fee) for correct Kelly computation.
    If return_details=True, returns (contracts, risk_cents, details_dict).
    """
    _zero = (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0}) if return_details else (0, 0)
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return _zero

    implied_prob = price_cents / 100.0
    our_prob = max(0.001, min(0.999, implied_prob + edge))

    # Fee reduces the payout, not the probability
    win_amount = (100 - fee_cents) - price_cents
    if win_amount <= 0:
        return _zero
    loss_amount = price_cents
    b = win_amount / loss_amount
    kelly_f = (b * our_prob - (1 - our_prob)) / b
    # Scale Kelly fraction based on bankroll size
    # 60% Kelly has unacceptable ruin probability for small bankrolls
    if bankroll_cents and bankroll_cents < 50000:  # under $500
        scaled_f = kelly_f * 0.5  # standard half-Kelly for small bankrolls
    else:
        scaled_f = kelly_f * 0.6  # 60% Kelly for larger bankrolls

    if scaled_f <= 0:
        return _zero

    if bankroll_cents is not None and bankroll_cents > 0:
        max_kelly = int((scaled_f * bankroll_cents) / price_cents)
    else:
        max_kelly = 999999

    max_cost = max_cost_cents // price_cents
    contracts = max(0, min(max_kelly, max_cost))
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, {"kelly_fraction": round(scaled_f, 6), "bankroll_used": bankroll_cents or 0})
    return (contracts, risk)


# ─── Market filters ───

# Minimum spread to consider a market liquid enough to trade
MIN_LIQUIDITY_VOLUME = 50
MAX_SPREAD_FOR_ENTRY = 20  # cents


def is_market_liquid(market):
    """Check if a market has sufficient liquidity for entry.

    Requires both a bid and ask, spread <= 20c, and volume >= 50.
    Prevents placing orders in dead markets with no counterparties.
    """
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    volume = market.get("volume", 0) or 0

    if not yes_bid or not yes_ask:
        return False
    if yes_ask - yes_bid > MAX_SPREAD_FOR_ENTRY:
        return False
    if volume < MIN_LIQUIDITY_VOLUME:
        return False
    return True


# ─── Execution helpers ───

def compute_limit_price(yes_bid, yes_ask, side, edge=None):
    """Compute a limit price within the spread, adapted to edge strength.

    Three tiers based on edge:
      - High edge (>=15%) or edge=None: pay full ask (urgency, maximize fill rate)
      - Medium edge (8%-15%): ask - 1c (balanced)
      - Low edge (<8%): midpoint + 1c (patient, legacy behavior)

    Falls back to ask if no bid or no spread.
    Returns price in cents, or 0 if no valid price available.
    """
    if side == "yes":
        if yes_bid and yes_ask and yes_ask > yes_bid:
            if edge is not None and edge < 0.08:
                # Patient: midpoint + 1c (original behavior)
                return min((yes_bid + yes_ask) // 2 + 1, yes_ask)
            elif edge is not None and edge < 0.15:
                # Balanced: ask - 1c
                return max(yes_ask - 1, yes_bid + 1)
            else:
                # Urgent (high edge or no edge specified): full ask
                return yes_ask
        return yes_ask or 0
    else:  # no
        no_bid = 100 - yes_ask if yes_ask else 0
        no_ask = 100 - yes_bid if yes_bid else 0
        if no_bid and no_ask and no_ask > no_bid:
            if edge is not None and edge < 0.08:
                return min((no_bid + no_ask) // 2 + 1, no_ask)
            elif edge is not None and edge < 0.15:
                return max(no_ask - 1, no_bid + 1)
            else:
                return no_ask
        return no_ask or 0
