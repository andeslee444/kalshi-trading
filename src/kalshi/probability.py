"""Shared probability models and position sizing for Kalshi trading bots.

CDF-based probability estimation for weather, info-arb, and NWS markets,
plus generalized half-Kelly position sizing. Uses math.erf for normal CDF
to avoid a scipy dependency.
"""

import json
import math
from pathlib import Path


def _norm_cdf(x):
    """Standard normal CDF. P(Z <= x) using math.erf."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


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
    return _calibration


def _reset_calibration():
    """Reset cached calibration (for testing)."""
    global _calibration
    _calibration = None


# ─── Probability models ───

def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """CDF-based probability for KXHIGH weather markets.

    sigma scales with forecast horizon: sigma = intercept + slope * days_out
    Default: sigma = 2.5 + 0.5 * days_out
    (day-0 sigma ~2.5F, day-3 sigma ~4.0F, day-7 sigma ~6.0F per NWS verification).

    If config/calibration.json exists with per-city or global sigma parameters,
    those override the defaults.

    direction="T": P(actual > threshold) = 1 - Phi((threshold - forecast) / sigma)
    direction="B": P(threshold <= actual < threshold+1) = Phi((threshold+1 - forecast)/sigma) - Phi((threshold - forecast)/sigma)
    """
    cal = _load_calibration()
    intercept = 2.5
    slope = 0.5

    weather_cal = cal.get("weather", {})
    if city and city in weather_cal.get("per_city", {}):
        city_cal = weather_cal["per_city"][city]
        intercept = city_cal.get("sigma_intercept", intercept)
        slope = city_cal.get("sigma_slope", slope)
    elif weather_cal.get("global_sigma_intercept") is not None:
        intercept = weather_cal["global_sigma_intercept"]
        slope = weather_cal.get("global_sigma_slope", slope)

    sigma = intercept + slope * max(0, days_out)

    if direction == "T":
        # P(actual > threshold)
        z = (threshold - forecast_temp) / sigma
        return 1.0 - _norm_cdf(z)
    else:
        # B = bracket: P(threshold <= actual < threshold + 1)
        z_low = (threshold - forecast_temp) / sigma
        z_high = (threshold + 1 - forecast_temp) / sigma
        return _norm_cdf(z_high) - _norm_cdf(z_low)


def nws_probability(running_high, threshold, direction, hour_of_day):
    """Probability for NWS actual-temp arbitrage (source-monitor).

    After peak heating (3+ PM), running_high is near-final.
    sigma_residual scales with time remaining:
      hour >= 17 (5 PM+): sigma=0.5F (essentially locked)
      hour >= 15 (3 PM+): sigma=1.5F (small upside possible)
      else: sigma=3.0F (too early, full uncertainty)

    If config/calibration.json has nws sigma_by_hour overrides, those are used.

    direction="T": P(final_high > threshold)
    direction="B": P(threshold <= final_high < threshold+1)
    """
    cal = _load_calibration()
    nws_cal = cal.get("nws", {}).get("sigma_by_hour", {})

    if hour_of_day >= 17:
        sigma = nws_cal.get("17+", 0.5)
    elif hour_of_day >= 15:
        sigma = nws_cal.get("15-16", 1.5)
    else:
        sigma = nws_cal.get("before_15", 3.0)

    if direction == "T":
        z = (threshold - running_high) / sigma
        return 1.0 - _norm_cdf(z)
    else:
        # Bracket
        z_low = (threshold - running_high) / sigma
        z_high = (threshold + 1 - running_high) / sigma
        return _norm_cdf(z_high) - _norm_cdf(z_low)


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


def album_data_sigma(day_of_week):
    """Day-dependent uncertainty for album sales data.

    day_of_week: 0=Monday ... 6=Sunday

    Mon/Tue (early projections): sigma = 15% of threshold
    Wed/Thu (mid-week updates):  sigma = 10%
    Fri+ (actual data):          sigma = 3%

    Overridden by calibration.json album_sales.sigma_by_day if present.
    """
    cal = _load_calibration()
    album_cal = cal.get("album_sales", {}).get("sigma_by_day", {})

    if day_of_week <= 1:  # Mon, Tue
        return album_cal.get("mon_tue", 0.15)
    elif day_of_week <= 3:  # Wed, Thu
        return album_cal.get("wed_thu", 0.10)
    else:  # Fri, Sat, Sun
        return album_cal.get("fri_sun", 0.03)


def boxoffice_data_sigma(day_of_week):
    """Day-dependent uncertainty for box office data.

    day_of_week: 0=Monday ... 6=Sunday

    Fri/Sat (estimates): sigma = 12%
    Sun (Sunday actuals): sigma = 5%
    Mon+ (final):         sigma = 2%

    Overridden by calibration.json box_office.sigma_by_day if present.
    """
    cal = _load_calibration()
    box_cal = cal.get("box_office", {}).get("sigma_by_day", {})

    if day_of_week in (4, 5):  # Fri, Sat
        return box_cal.get("fri_sat", 0.12)
    elif day_of_week == 6:  # Sun
        return box_cal.get("sun", 0.05)
    else:  # Mon-Thu
        return box_cal.get("mon_thu", 0.02)


# ─── Position sizing ───

def half_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None):
    """Generalized half-Kelly position sizing for binary contracts (buy side).

    Returns (contracts, risk_cents).

    edge: our_prob - market_implied_prob (positive = trade, negative = skip)
    price_cents: price we'd pay (1-99)
    max_cost_cents: max total spend per trade (from config)
    bankroll_cents: optional, for Kelly fraction calculation

    When buying YES at price p:
      win = 100 - p cents with prob our_prob
      lose = p cents with prob (1 - our_prob)

    When buying NO at price (100-p):
      This is equivalent — just pass the NO price as price_cents.
    """
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return (0, 0)

    implied_prob = price_cents / 100.0
    our_prob = implied_prob + edge

    # Kelly fraction: f = (b*p - q) / b
    # where b = win/loss ratio, p = win prob, q = 1 - p
    win_amount = 100 - price_cents
    loss_amount = price_cents
    b = win_amount / loss_amount
    kelly_f = (b * our_prob - (1 - our_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return (0, 0)

    # Max contracts from Kelly fraction (if bankroll provided)
    if bankroll_cents and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / price_cents)
    else:
        max_kelly = 999999

    # Max contracts from cost cap
    max_cost = max_cost_cents // price_cents

    contracts = min(max_kelly, max_cost)
    contracts = max(0, contracts)

    risk = contracts * price_cents
    return (contracts, risk)


def half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None):
    """Half-Kelly for selling YES (buying NO). Returns (contracts, risk_cents).

    edge: overpricing (implied_prob - true_prob, positive = sell signal)
    sell_price_cents: YES price we're selling at (1-99)
    max_cost_cents: max total risk (contracts * (100 - sell_price))
    bankroll_cents: optional Kelly fraction constraint

    When selling YES at price p:
      - We receive p cents now
      - We lose (100 - p) cents if the event occurs
      - True prob of event = implied_prob - edge
    """
    if edge <= 0 or sell_price_cents <= 0 or sell_price_cents >= 100:
        return (0, 0)

    # Market implied prob of event
    implied_prob = sell_price_cents / 100.0
    # Our estimated true probability (lower than market thinks)
    p_true = max(0.001, implied_prob - edge)

    # Selling YES: win sell_price cents with prob (1-p_true),
    #              lose (100-sell_price) cents with prob p_true
    win_prob = 1 - p_true
    win_amount = sell_price_cents
    loss_amount = 100 - sell_price_cents

    # Kelly: f = (p*b - q) / b where b = win/loss odds ratio, p = win prob, q = 1-p
    b = win_amount / loss_amount
    kelly_f = (b * win_prob - (1 - win_prob)) / b
    half_f = kelly_f / 2

    if half_f <= 0:
        return (0, 0)

    risk_per = 100 - sell_price_cents

    # Max contracts from Kelly fraction (if bankroll provided)
    if bankroll_cents and bankroll_cents > 0:
        max_kelly = int((half_f * bankroll_cents) / risk_per)
    else:
        max_kelly = 999999

    # Max contracts from risk cap
    max_cap = max_cost_cents // risk_per

    contracts = min(max_kelly, max_cap)
    contracts = max(0, contracts)

    risk_cents = contracts * risk_per
    return (contracts, risk_cents)
