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


def _norm_pdf(x):
    """Standard normal PDF. phi(x) = exp(-x^2/2) / sqrt(2*pi)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _owens_t(h, a):
    """Owen's T function via 10-point Gauss-Legendre quadrature.

    T(h, a) = (1/2pi) * integral_0^a exp(-0.5*h^2*(1+t^2)) / (1+t^2) dt

    The integral is well-behaved and converges quickly with Gaussian quadrature.
    Used by skew-normal CDF.
    """
    if abs(a) < 1e-15:
        return 0.0

    # 10-point Gauss-Legendre nodes and weights on [-1, 1]
    gl_nodes = [
        -0.9739065285171717, -0.8650633666889845, -0.6794095682990244,
        -0.4333953941292472, -0.1488743389816312,
        0.1488743389816312, 0.4333953941292472, 0.6794095682990244,
        0.8650633666889845, 0.9739065285171717,
    ]
    gl_weights = [
        0.0666713443086881, 0.1494513491505806, 0.2190863625159820,
        0.2692667193099963, 0.2955242247147529,
        0.2955242247147529, 0.2692667193099963, 0.2190863625159820,
        0.1494513491505806, 0.0666713443086881,
    ]

    # Transform from [-1, 1] to [0, a]
    half_a = a / 2.0
    mid_a = a / 2.0

    result = 0.0
    h_sq = h * h
    for i in range(10):
        t = mid_a + half_a * gl_nodes[i]
        t_sq = t * t
        integrand = math.exp(-0.5 * h_sq * (1 + t_sq)) / (1 + t_sq)
        result += gl_weights[i] * integrand

    return result * half_a / (2.0 * math.pi)


def _skew_normal_cdf(x, alpha=0.0):
    """Skew-normal CDF. alpha=0 reduces to standard normal.

    Positive alpha = right skew (warm bias), negative = left skew (cold bias).
    Uses the closed-form: Phi_SN(x) = Phi(x) - 2*T(x, alpha)
    where T(x, alpha) is Owen's T function.
    """
    result = _norm_cdf(x) - 2.0 * _owens_t(x, alpha)
    # Clamp to [0, 1] to handle minor numerical precision issues from quadrature
    return max(0.0, min(1.0, result))


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
    """Reset cached calibration to defaults (for testing).

    Sets _calibration to empty dict so _load_calibration() returns {}
    without re-reading calibration.json from disk. This ensures tests
    use hardcoded default sigma values regardless of what's on disk.
    """
    global _calibration
    _calibration = {}


# ─── Probability models ───

def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None,
                        sigma_override=None, hour_of_day=None, skew=0.0):
    """CDF-based probability for KXHIGH weather markets.

    sigma scales with forecast horizon: sigma = intercept + slope * sqrt(days_out)
    Default: sigma = 1.5 + 0.5 * sqrt(days_out)
    Sublinear (sqrt) scaling matches random-walk forecast error growth.

    If config/calibration.json exists with per-city or global sigma parameters,
    those override the defaults.

    Args:
        sigma_override: when set, replaces the computed sigma entirely.
            Used by ensemble to pass spread-adjusted sigma.
        hour_of_day: hour (0-23) for intra-day sigma decay on day-0 markets.
            Only used when days_out == 0. None = use standard sigma.
        skew: skew-normal alpha parameter (default 0.0 = symmetric).
            Positive = right skew (warm bias), negative = left skew (cold bias).
            Read from calibration.json weather.skew or weather.per_city.{city}.skew
            if not explicitly provided (i.e., if 0.0).

    direction="T": P(actual > threshold) = 1 - Phi((threshold - forecast) / sigma)
    direction="B": P(threshold <= actual < threshold+1) = Phi((threshold+1 - forecast)/sigma) - Phi((threshold - forecast)/sigma)
    """
    if forecast_temp is None:
        return None

    cal = _load_calibration()
    intercept = 1.5  # NWS MAE data shows day-0 error ~1.5°F (was 2.0; Brier 0.321 showed sigma too large)
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

    # Hour-of-day sigma adjustment for day-0 markets
    if hour_of_day is not None and days_out == 0 and sigma_override is None:
        sigma = weather_sigma_hourly(days_out=0, city=city, hour_of_day=hour_of_day)

    # Allow callers (e.g. ensemble) to override sigma entirely
    if sigma_override is not None and sigma_override > 0:
        sigma = sigma_override

    # Resolve skew parameter: explicit > per-city calibration > global calibration > 0.0
    if skew == 0.0:
        if city and city in weather_cal.get("per_city", {}):
            skew = weather_cal["per_city"][city].get("skew", 0.0)
        else:
            skew = weather_cal.get("skew", 0.0)

    # Degrees of freedom for Student's t (fat tails for forecast errors)
    df = weather_cal.get("df", 6)
    if not isinstance(df, (int, float)) or df < 2:
        _log.warning("Invalid df=%s in calibration, using default df=6", df)
        df = 6

    if direction == "T":
        # P(actual > threshold)
        z = (threshold - forecast_temp) / sigma
        base_prob = 1.0 - _student_t_cdf(z, df)

        # Skew correction: use skew-normal CDF to compute the asymmetric
        # correction, then apply as an additive delta to the Student-t result.
        # This preserves fat tails from Student-t while adding asymmetry.
        # skew_normal_cdf(z, alpha) < norm_cdf(z) when alpha > 0 (right skew)
        # => 1 - skew_normal_cdf > 1 - norm_cdf => P(above) increases with positive skew
        if abs(skew) > 1e-10:
            normal_prob = 1.0 - _norm_cdf(z)
            skew_prob = 1.0 - _skew_normal_cdf(z, alpha=skew)
            # Additive correction: difference between skew-normal and normal
            skew_correction = skew_prob - normal_prob
            base_prob = max(0.001, min(0.999, base_prob + skew_correction))

        return base_prob
    else:
        # B = bracket: P(threshold <= actual < threshold + 1)
        z_low = (threshold - forecast_temp) / sigma
        z_high = (threshold + 1 - forecast_temp) / sigma
        base_prob = _student_t_cdf(z_high, df) - _student_t_cdf(z_low, df)

        # Skew correction for brackets (apply to both bounds)
        if abs(skew) > 1e-10:
            sn_high = _skew_normal_cdf(z_high, alpha=skew)
            sn_low = _skew_normal_cdf(z_low, alpha=skew)
            normal_diff = _norm_cdf(z_high) - _norm_cdf(z_low)
            skew_diff = sn_high - sn_low
            if abs(normal_diff) > 1e-10:
                # Scale the Student-t bracket probability by the skew ratio
                ratio = skew_diff / normal_diff
                base_prob = max(0.0, base_prob * ratio)

        return base_prob


def weather_sigma(days_out=0, city=None):
    """Return the sigma used by weather_probability for a given horizon and city.

    Useful for logging sigma_used in trade records for calibration.
    """
    cal = _load_calibration()
    intercept = 1.5
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


def weather_sigma_hourly(days_out=0, city=None, hour_of_day=None):
    """Sigma with intra-day decay for day-0 forecasts.

    For days_out == 0, applies an hour-of-day decay factor to the base sigma:
      - Before 6am (overnight): decay_factor = 1.2 (extra uncertainty)
      - At hour 6 (morning): decay_factor = 1.0 (full daily sigma)
      - Exponential decay from 6am onward: decay_factor = max(0.4, exp(-0.08 * (hour - 6)))
      - At hour 14 (afternoon): ~40% reduction from morning
      - At hour 17 (evening): ~60% reduction (forecast nearly settled)

    For days_out > 0 or hour_of_day is None, returns weather_sigma unchanged.
    """
    base = weather_sigma(days_out, city)

    if hour_of_day is None or days_out > 0:
        return base

    # Intra-day decay for day-0 markets
    if hour_of_day < 6:
        decay_factor = 1.2  # overnight extra uncertainty
    else:
        decay_factor = max(0.4, math.exp(-0.08 * (hour_of_day - 6)))

    return max(0.5, base * decay_factor)


def _load_backtest_brier():
    """Load per-model Brier scores from backtest results for BMA weighting.

    Returns dict like {"gfs": 0.15, "ecmwf": 0.12, "icon": 0.18} or None
    if data isn't available.
    """
    backtest_path = Path(__file__).resolve().parent.parent.parent / "data" / "backtest-results.json"
    try:
        if backtest_path.exists():
            data = json.loads(backtest_path.read_text())
            per_model = data.get("weather", {}).get("per_model_brier")
            if per_model and isinstance(per_model, dict):
                # Validate all values are positive numbers
                if all(isinstance(v, (int, float)) and v > 0 for v in per_model.values()):
                    return per_model
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def ensemble_weather_probability(forecasts, threshold, direction, days_out=0, city=None,
                                 sigma_multiplier=1.0):
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
        sigma_multiplier: multiplier applied to base sigma (default 1.0).
            Values > 1 widen the distribution (more conservative).

    Returns:
        Weighted average probability (0-1).
    """
    cal = _load_calibration()
    ensemble_cal = cal.get("ensemble", {})

    # Try Brier-weighted BMA: weights proportional to 1/brier_score
    brier_data = _load_backtest_brier()
    if brier_data:
        inv_brier = {}
        for model_name in forecasts:
            if model_name in brier_data:
                inv_brier[model_name] = 1.0 / brier_data[model_name]
        if len(inv_brier) >= 2:
            total_inv = sum(inv_brier.values())
            weights = {k: v / total_inv for k, v in inv_brier.items()}
            dropped = [m for m in forecasts if m not in inv_brier]
            if dropped:
                _log.warning("BMA: dropping models with no Brier data: %s (weight=0)", dropped)
            _log.debug("BMA weights from Brier: %s", weights)
        else:
            # Not enough Brier data — fall back to static weights
            brier_data = None

    if not brier_data:
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

    # Compute sigma_override if multiplier != 1.0
    sigma_kwarg = {}
    if sigma_multiplier != 1.0 and sigma_multiplier > 0:
        base_sigma = weather_sigma(days_out, city)
        sigma_kwarg["sigma_override"] = base_sigma * sigma_multiplier

    total_weight = 0.0
    weighted_prob = 0.0

    for model_name, temp in forecasts.items():
        w = weights.get(model_name, 0.0)
        if w <= 0:
            continue
        prob = weather_probability(temp, threshold, direction, days_out, city=city, **sigma_kwarg)
        if prob is None:
            continue
        weighted_prob += w * prob
        total_weight += w

    if total_weight <= 0:
        # No valid models — return None so callers can fall back to single-model
        _log.error("ensemble_weather_probability: zero total weight for models %s", list(forecasts.keys()))
        return None

    return weighted_prob / total_weight


def ensemble_spread_sigma_multiplier(spread_f):
    """Compute sigma multiplier based on ensemble model spread.

    When GFS, ECMWF, and ICON disagree, forecast uncertainty is higher.
    Wider ensemble spread → wider sigma → more conservative trading.

    Args:
        spread_f: Max - min forecast temperature across models (°F).

    Returns:
        Multiplier >= 1.0. Applied to base sigma in weather_probability().
    """
    if spread_f <= 2.0:
        return 1.0  # Models agree — no adjustment
    # Linear ramp: spread 2→10°F maps to multiplier 1.0→2.0
    raw = 1.0 + (spread_f - 2.0) * 0.125
    return min(raw, 2.5)  # Cap at 2.5x


def compute_adaptive_ensemble_weights(verification_data, default_weights=None):
    """Compute model weights from recent forecast verification data.

    Uses inverse-Brier weighting: models with lower Brier scores (more accurate)
    get higher weight. Falls back to default_weights if verification data is
    insufficient (< 20 samples per model) or None.

    Args:
        verification_data: dict of {model_name: {"brier_predictions": [(pred_prob, actual_outcome), ...]}}
            or None. Each brier_predictions entry is a list of (predicted_probability, 0_or_1) tuples.
        default_weights: fallback weights dict {model_name: weight}. Returned when
            verification data is insufficient.

    Returns:
        dict of {model_name: weight} summing to 1.0.
    """
    if default_weights is None:
        default_weights = {"gfs": 0.40, "ecmwf": 0.40, "icon": 0.20}

    if not verification_data:
        return default_weights

    # Compute Brier score per model
    model_briers = {}
    min_samples = 20

    for model_name, data in verification_data.items():
        preds = data.get("brier_predictions", [])
        if len(preds) < min_samples:
            continue
        # Brier score = mean((predicted - actual)^2)
        brier = sum((p - a) ** 2 for p, a in preds) / len(preds)
        if brier > 0:
            model_briers[model_name] = brier

    if len(model_briers) < 2:
        return default_weights

    # Inverse-Brier weighting
    inv_brier = {m: 1.0 / b for m, b in model_briers.items()}
    total = sum(inv_brier.values())
    return {m: w / total for m, w in inv_brier.items()}


def ensemble_disagreement_score(model_probs):
    """Compute disagreement between ensemble model probabilities.

    Uses coefficient of variation (std/mean) of probabilities, normalized
    to [0, 1]. When score > 0.3, signals regime uncertainty and the bot
    should require higher edge threshold or skip the trade.

    Args:
        model_probs: dict of {model_name: probability} (values in [0, 1]).

    Returns:
        float in [0, 1] where 0 = perfect agreement, 1 = maximum disagreement.
    """
    if not model_probs or len(model_probs) < 2:
        return 0.0

    probs = list(model_probs.values())
    mean_p = sum(probs) / len(probs)

    if mean_p <= 0:
        return 0.0

    # Standard deviation
    variance = sum((p - mean_p) ** 2 for p in probs) / len(probs)
    std_dev = math.sqrt(variance)

    # Coefficient of variation, capped at 1.0
    # Normalize by theoretical max CV for [0,1] bounded values
    # Max CV occurs when half are 0 and half are 1 -> CV = 1/sqrt(n) * n/sqrt(n) ~ 1
    cv = std_dev / mean_p if mean_p > 0 else 0.0
    return min(1.0, cv)


def ensemble_weather_probability_v2(forecasts, threshold, direction, days_out=0, city=None,
                                     sigma_multiplier=1.0, hour_of_day=None,
                                     verification_data=None, return_details=False):
    """Enhanced ensemble with adaptive weights, hour-aware sigma, and disagreement scoring.

    Builds on ensemble_weather_probability with three additions:
    1. Hour-of-day-aware sigma for day-0 markets
    2. Adaptive weights from verification data (inverse-Brier weighting)
    3. Disagreement score measuring ensemble model divergence

    Args:
        forecasts: dict mapping model name to forecast temperature (F).
        threshold: market threshold temperature (F).
        direction: "T" (above threshold) or "B" (bracket).
        days_out: forecast horizon in days.
        city: city code for calibration lookup.
        sigma_multiplier: multiplier applied to base sigma (default 1.0).
        hour_of_day: hour (0-23) for intra-day sigma decay on day-0 markets.
        verification_data: dict for adaptive ensemble weights (from ForecastVerifier).
        return_details: if True, return (prob, details_dict) with disagreement_score,
            weights_used, per_model_probs. If False, return just prob (backward compatible).

    Returns:
        float probability, or (float, dict) if return_details=True.
    """
    cal = _load_calibration()
    ensemble_cal = cal.get("ensemble", {})

    # Determine weights: adaptive > backtest Brier > static
    if verification_data:
        weights = compute_adaptive_ensemble_weights(verification_data)
    else:
        brier_data = _load_backtest_brier()
        if brier_data:
            inv_brier = {}
            for model_name in forecasts:
                if model_name in brier_data:
                    inv_brier[model_name] = 1.0 / brier_data[model_name]
            if len(inv_brier) >= 2:
                total_inv = sum(inv_brier.values())
                weights = {k: v / total_inv for k, v in inv_brier.items()}
            else:
                brier_data = None

        if not verification_data and not brier_data:
            if days_out <= 1:
                default_weights = {"gfs": 0.50, "ecmwf": 0.35, "icon": 0.15}
            elif days_out <= 3:
                default_weights = {"gfs": 0.35, "ecmwf": 0.45, "icon": 0.20}
            else:
                default_weights = {"gfs": 0.30, "ecmwf": 0.45, "icon": 0.25}
            weights = ensemble_cal.get("weights", default_weights)

    # Compute sigma_override if multiplier != 1.0
    sigma_kwarg = {}
    if sigma_multiplier != 1.0 and sigma_multiplier > 0:
        base_sigma = weather_sigma(days_out, city)
        sigma_kwarg["sigma_override"] = base_sigma * sigma_multiplier

    # Pass hour_of_day for day-0 intra-day sigma (only when no sigma_override)
    hour_kwarg = {}
    if hour_of_day is not None and days_out == 0 and "sigma_override" not in sigma_kwarg:
        hour_kwarg["hour_of_day"] = hour_of_day

    total_weight = 0.0
    weighted_prob = 0.0
    per_model_probs = {}

    for model_name, temp in forecasts.items():
        w = weights.get(model_name, 0.0)
        if w <= 0:
            continue
        prob = weather_probability(temp, threshold, direction, days_out, city=city,
                                   **sigma_kwarg, **hour_kwarg)
        if prob is None:
            continue
        per_model_probs[model_name] = prob
        weighted_prob += w * prob
        total_weight += w

    if total_weight <= 0:
        if return_details:
            return (None, {"disagreement_score": 0.0, "weights_used": weights, "per_model_probs": {}})
        return None

    final_prob = weighted_prob / total_weight
    disagreement = ensemble_disagreement_score(per_model_probs)

    if return_details:
        return (final_prob, {
            "disagreement_score": disagreement,
            "weights_used": {k: v for k, v in weights.items() if k in per_model_probs},
            "per_model_probs": per_model_probs,
        })
    return final_prob


def empirical_ensemble_probability(member_temps, threshold, direction, bias_offset=0.0):
    """Empirical CDF from raw ensemble member temperatures.

    Ranks ensemble members, applies optional station bias correction,
    and computes KDE-smoothed probability. No parametric sigma assumption.

    Args:
        member_temps: list of forecast temperatures (F) from ensemble members.
            Typically 82 members (31 GEFS + 51 ECMWF ENS).
        threshold: market threshold temperature (F).
        direction: "T" (P(T > threshold)) or "B" (P(threshold <= T < threshold+1)).
        bias_offset: station bias correction (F) added to all members before CDF.
            Positive = warm bias in forecasts (subtract from members).
            Default 0.0 (no correction).

    Returns:
        float probability in [0, 1], or None if member_temps is empty/invalid.
    """
    if not member_temps or len(member_temps) < 5:
        return None

    # Apply bias correction: positive bias = forecasts run warm, so subtract
    corrected = [t - bias_offset for t in member_temps]
    n = len(corrected)

    # Compute standard deviation for KDE bandwidth
    mean = sum(corrected) / n
    variance = sum((t - mean) ** 2 for t in corrected) / n
    std = math.sqrt(variance) if variance > 0 else 0.5

    # Silverman's rule of thumb bandwidth, with minimum of 0.5 deg F
    h = max(0.5, 1.06 * std * n ** (-1.0 / 5.0))

    if direction == "T":
        # P(T > threshold) using kernel CDF estimator
        prob = sum(1.0 - _norm_cdf((threshold - t) / h) for t in corrected) / n
    elif direction == "B":
        # P(threshold <= T < threshold + 1) = CDF(threshold+1) - CDF(threshold)
        cdf_upper = sum(_norm_cdf((threshold + 1 - t) / h) for t in corrected) / n
        cdf_lower = sum(_norm_cdf((threshold - t) / h) for t in corrected) / n
        prob = cdf_upper - cdf_lower
    else:
        return None

    # Clamp to [0.01, 0.99] to avoid extremes with limited ensemble size
    return max(0.01, min(0.99, prob))


def nws_sigma_for_hour(hour_of_day):
    """NWS temperature uncertainty (sigma in degrees F) for a given hour.

    Continuous exponential decay model:
      sigma = max(0.5, 4.0 * exp(-0.18 * (hour - 6)))

    Falls back to legacy step-function if calibration.json has nws.sigma_by_hour.

    Exported for use in source-monitor CI-based edge gating.
    """
    cal = _load_calibration()
    nws_section = cal.get("nws", {})
    nws_cal = nws_section.get("sigma_by_hour", {})

    if nws_cal:
        # Legacy step-function: use calibrated values
        if hour_of_day < 6:
            return max(0.5, nws_cal.get("overnight", 5.0))
        elif hour_of_day >= 17:
            return max(0.5, nws_cal.get("17+", 0.5))
        elif hour_of_day >= 15:
            return max(0.5, nws_cal.get("15-16", 1.5))
        else:
            return max(0.5, nws_cal.get("before_15", 3.0))
    else:
        # Continuous model: exponential decay from morning uncertainty
        if hour_of_day < 6:
            return max(0.5, 5.0)
        else:
            return max(0.5, 4.0 * math.exp(-0.18 * (hour_of_day - 6)))


def nws_probability(running_high, threshold, direction, hour_of_day):
    """Probability for NWS actual-temp arbitrage (source-monitor).

    Uses nws_sigma_for_hour() for residual uncertainty estimation.
    Student-t CDF (df from calibration, default 6) for fat-tail modeling.

    direction="T": P(final_high > threshold)
    direction="B": P(threshold <= final_high < threshold+1)
    """
    cal = _load_calibration()
    nws_df = cal.get("nws", {}).get("df", 6)
    if not isinstance(nws_df, (int, float)) or nws_df < 2:
        _log.warning("Invalid nws_df=%s in calibration, using default df=6", nws_df)
        nws_df = 6

    sigma = nws_sigma_for_hour(hour_of_day)

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

    Returns sigma in percentage points. Wider range than CPI (GDP is noisier).
    floor=0.05 at release, range=0.15, k=0.12.

    sigma = 0.05 + 0.15 * (1 - exp(-0.12 * d))
    d=0: 0.05, d=7: ~0.107, d=14: ~0.131, d=30: ~0.172
    """
    cal = _load_calibration()
    gdp_cal = cal.get("gdp", {}).get("sigma_by_days", {})
    if gdp_cal:
        key = str(min(30, max(0, days_to_release)))
        if key in gdp_cal:
            return gdp_cal[key]

    d = max(0, days_to_release)
    return 0.05 + 0.15 * (1 - math.exp(-0.12 * d))


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


def _edge_after_fees(raw_edge, price_cents):
    """DEPRECATED internal helper. Use raw edge + fee_cents param instead.

    This function subtracts fee as a probability delta, but the mathematically
    correct treatment is to reduce the payout (100 -> 100-fee) in the Kelly
    formula. All bots now pass fee_cents directly to half_kelly/quarter_kelly.
    """
    fee = kalshi_fee_cents(price_cents)
    return raw_edge - fee / 100


# Backward-compatible alias — underscore prefix signals deprecation to developers
edge_after_fees = _edge_after_fees


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
    ou_drift_adj = 0.0  # Additional drift from mean-reversion
    if use_ou:
        half_life = ou_half_life_minutes or 120  # default 2-hour half-life
        theta_ou = math.log(2) / max(1, half_life)  # mean-reversion speed (per minute)
        two_theta_T = 2 * theta_ou * time_horizon_minutes
        if two_theta_T > 1e-10:
            # OU variance adjustment: Var[X_T] = sigma^2 * (1-e^{-2*theta*T}) / (2*theta*T)
            ou_factor = math.sqrt((1 - math.exp(-two_theta_T)) / two_theta_T)

            # OU drift correction: mean-reversion pull toward long-run mean
            # For log-price OU: E[X_T] = X_0 * e^{-theta*T} + mu * (1 - e^{-theta*T})
            # The correction reduces effective drift for deviations from mean
            theta_T_min = theta_ou * time_horizon_minutes
            ou_drift_adj = -(1 - math.exp(-theta_T_min)) * math.log(current_price / threshold)

            # Smooth blend: full OU below 180 min, linear taper to 1.0 at 300 min
            if time_horizon_minutes > 180:
                blend = max(0.0, (300 - time_horizon_minutes) / 120)
                ou_factor = blend * ou_factor + (1 - blend) * 1.0
                ou_drift_adj *= blend
            sigma = sigma * ou_factor

    sqrt_T = math.sqrt(T)
    sigma_sqrt_T = sigma * sqrt_T

    if sigma_sqrt_T <= 0:
        return 1.0 if current_price > threshold else 0.0

    # d2 with configurable drift + OU drift correction
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
    """Merton jump-diffusion probability for crypto price markets.

    Extends GBM with Poisson jump process for fatter tails. Better pricing
    for far-OTM crypto markets where flash crashes/rallies are underpriced
    by pure GBM.

    The model: dS/S = (mu - lambda*k)dt + sigma*dW + J*dN
    where J ~ N(jump_mean, jump_std), N ~ Poisson(lambda*T).

    P(S_T > K) = sum_{n=0}^{N_max} P(N=n) * P_n(S_T > K | n jumps)

    where P_n is a GBM probability with adjusted sigma and drift.

    Args:
        current_price: Current spot price.
        threshold: Market threshold price.
        direction: "above" or "below".
        time_horizon_minutes: Minutes to settlement.
        realized_vol_pct: Annualized vol as decimal (e.g., 0.60).
        iv_pct: Implied vol (takes precedence over realized).
        drift_pct: Annualized drift rate.
        jump_intensity: Average jumps per year (lambda). Default 1.0.
        jump_mean: Mean log-jump size (default -0.05 = -5%, slight downward bias).
        jump_std: Std of log-jump size (default 0.10 = 10%).
        max_jumps: Max number of jumps to sum over (default 10).

    Returns:
        Probability (0-1).
    """
    if current_price <= 0 or threshold <= 0:
        return 0.5

    # Select volatility
    if iv_pct is not None and iv_pct > 0:
        sigma = iv_pct
    elif realized_vol_pct is not None and realized_vol_pct > 0:
        sigma = realized_vol_pct
    else:
        sigma = 0.60

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        return 1.0 if current_price > threshold else 0.0

    # Compensator: k = E[e^J - 1] = exp(jump_mean + 0.5*jump_std^2) - 1
    k = math.exp(jump_mean + 0.5 * jump_std ** 2) - 1
    lambda_T = jump_intensity * T

    log_S_K = math.log(current_price / threshold)
    prob_above = 0.0

    for n in range(max_jumps + 1):
        # Poisson probability P(N = n)
        if n == 0:
            poisson_p = math.exp(-lambda_T)
        else:
            # log(P(N=n)) = n*log(lambda_T) - lambda_T - sum(log(1..n))
            log_p = n * math.log(max(lambda_T, 1e-300)) - lambda_T
            for i in range(1, n + 1):
                log_p -= math.log(i)
            poisson_p = math.exp(log_p)

        if poisson_p < 1e-15:
            break  # Negligible contribution

        # Conditional GBM with n jumps:
        # sigma_n^2 = sigma^2 + n * jump_std^2 / T
        sigma_n_sq = sigma ** 2 + n * jump_std ** 2 / max(T, 1e-15)
        sigma_n = math.sqrt(sigma_n_sq)

        # drift_n = drift - lambda*k + n*jump_mean/T
        drift_n = drift_pct - jump_intensity * k + n * jump_mean / max(T, 1e-15)

        # d2 = (log(S/K) + (drift_n - 0.5*sigma_n^2)*T) / (sigma_n * sqrt(T))
        sqrt_T = math.sqrt(T)
        d2 = (log_S_K + (drift_n - 0.5 * sigma_n_sq) * T) / (sigma_n * sqrt_T)
        prob_above += poisson_p * _norm_cdf(d2)

    # Clamp to [0, 1]
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
    """Heston stochastic volatility model for crypto binary options.

    Uses Formulation 2 (Albrecher et al. 2007) of the characteristic function
    for numerical stability. Computes P(S_T > K) via Fourier inversion.

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

    # Feller condition: 2*kappa*theta >= xi^2 ensures variance stays positive.
    # When violated (e.g., GARCH-driven xi), the Fourier inversion becomes
    # unreliable. Fall back to GBM with vol=sqrt(v0) for safety.
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
        """Heston CF for P2 (risk-neutral prob), Formulation 2 (stable)."""
        u = -0.5
        b = kappa

        a_val = rho * xi * 1j * phi - b
        d = np.sqrt(a_val ** 2 - xi ** 2 * (2 * u * 1j * phi - phi ** 2))

        # Enforce Re(d) >= 0 to select correct branch
        if np.real(d) < 0:
            d = -d

        # Formulation 2: |g| <= 1 always when Re(d) >= 0
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
        """Integrand for P2 = 0.5 + (1/pi) * integral."""
        cf = heston_cf_p2(phi)
        return np.real(cf / (1j * phi))

    # Adaptive upper limit: higher for low vol or short horizons
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

    risk_per = 100 - int(sell_price_cents)
    if risk_per <= 1:
        if return_details:
            return (0, 0, {"kelly_fraction": 0.0, "bankroll_used": bankroll_cents or 0})
        return _zero

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
    # Use round() instead of // to avoid silently zeroing out 1-contract positions
    contracts = max(1, round(contracts / 2)) if contracts >= 1 else 0
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    # Hard-cap bracket exposure
    if contracts * price_cents > max_exposure_cents:
        contracts = max_exposure_cents // price_cents
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)


def uncertainty_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                      scenario_agreement, posterior_sigma, fee_cents=0):
    """Kelly sizing scaled by model confidence.

    Wraps quarter_kelly() with two confidence multipliers:
    1. scenario_agreement (0-1): do all scenarios agree on the direction?
    2. posterior_sigma: how tight is the Bayesian posterior?

    Combined via geometric mean to avoid double-counting.

    Args:
        edge: model_prob - implied_prob
        price_cents: limit price (1-99)
        max_cost_cents: max spend per trade
        bankroll_cents: total balance
        scenario_agreement: from ScenarioResult.agreement (0-1)
        posterior_sigma: from CPIBeliefFilter.posterior sigma
        fee_cents: per-contract fee

    Returns:
        (contracts, risk_cents, details_dict)
    """
    base_count, risk, details = quarter_kelly(
        edge, price_cents, max_cost_cents, bankroll_cents,
        fee_cents=fee_cents, return_details=True,
    )

    if base_count <= 0:
        return 0, 0, {**details, "confidence": 0, "agreement_mult": 0, "sigma_mult": 0}

    # Scenario agreement: 1.0 -> full size, 0.5 -> ~35%, 0.0 -> 10%
    agreement_mult = 0.1 + 0.9 * max(0, min(1, scenario_agreement)) ** 2

    # Sigma confidence: tight posterior -> full size, wide -> reduced
    # Calibrated: 0.10pp -> mult=1.0, 0.40pp -> mult=0.25
    sigma_mult = min(1.0, 0.10 / max(posterior_sigma, 0.01))

    # Geometric mean avoids double-counting correlated signals
    confidence = math.sqrt(agreement_mult * sigma_mult)
    confidence = max(0.0, min(1.0, confidence))

    adjusted_count = max(1, int(base_count * confidence))
    adjusted_risk = risk * confidence

    return adjusted_count, adjusted_risk, {
        **details,
        "confidence": round(confidence, 4),
        "agreement_mult": round(agreement_mult, 4),
        "sigma_mult": round(sigma_mult, 4),
    }


def quarter_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None,
                       max_exposure_cents=None, fee_cents=0, return_details=False):
    """Quarter-Kelly for sell-side trades (higher model uncertainty).

    Mirrors quarter_kelly but for selling YES (buying NO).
    Uses half_kelly_sell internally, then halves the result.

    max_exposure_cents: hard cap on total position risk.
                        Default scales with bankroll: max($5, 5% of bankroll).
    fee_cents: per-contract fee in cents, passed through to half_kelly_sell.
    If return_details=True, returns (contracts, risk_cents, details_dict).
    """
    if max_exposure_cents is None:
        max_exposure_cents = max(500, int((bankroll_cents or 10000) * 0.05))
    result = half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents,
                             fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    # Halve the half-Kelly position (= quarter-Kelly)
    # Use round() instead of // to avoid silently zeroing out 1-contract positions
    contracts = max(1, round(contracts / 2)) if contracts >= 1 else 0
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    # Hard-cap exposure (risk per contract = 100 - sell_price for sell side)
    risk_per = 100 - int(sell_price_cents)
    if risk_per > 0 and contracts * risk_per > max_exposure_cents:
        contracts = max_exposure_cents // risk_per
    risk = contracts * risk_per if risk_per > 0 else 0
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
MIN_LIQUIDITY_VOLUME = 10
MAX_SPREAD_FOR_ENTRY = 20  # cents


def is_market_liquid(market, min_volume=None, max_spread=None):
    """Check if a market has sufficient liquidity for entry.

    Requires both a bid and ask, spread <= max_spread, and volume >= min_volume.
    Prevents placing orders in dead markets with no counterparties.

    Args:
        market: Market dict with yes_bid, yes_ask, volume fields.
        min_volume: Override minimum volume (default: MIN_LIQUIDITY_VOLUME=10).
        max_spread: Override max spread in cents (default: MAX_SPREAD_FOR_ENTRY=20).
    """
    vol_threshold = min_volume if min_volume is not None else MIN_LIQUIDITY_VOLUME
    spread_threshold = max_spread if max_spread is not None else MAX_SPREAD_FOR_ENTRY

    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    volume = market.get("volume", 0) or 0

    if not yes_bid or not yes_ask:
        return False
    if yes_ask - yes_bid > spread_threshold:
        return False
    if volume < vol_threshold:
        return False
    return True


# ─── Execution helpers ───

def compute_limit_price(yes_bid, yes_ask, side, edge=None):
    """Compute a limit price within the spread, adapted to edge strength.

    YES-side tiers:
      - High edge (>=15%) or edge=None: full ask (urgency)
      - Medium edge (8%-15%): ask - 1c (balanced)
      - Low edge (<8%): midpoint + 1c (patient)

    NO-side tiers (spread-fraction placement):
      - High edge (>=15%) or edge=None: full no_ask (urgency)
      - Medium edge (8%-15%): no_bid + 2/3 spread (outer third)
      - Low edge (<8%): no_bid + 1/3 spread (inner third, patient)

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
            spread = no_ask - no_bid
            if edge is not None and edge < 0.08:
                # Patient: inner third of spread (closer to bid)
                return no_bid + max(1, spread // 3)
            elif edge is not None and edge < 0.15:
                # Balanced: outer third of spread
                return no_bid + max(1, spread * 2 // 3)
            else:
                # Urgent (high edge or no edge specified): full ask
                return no_ask
        return no_ask or 0
