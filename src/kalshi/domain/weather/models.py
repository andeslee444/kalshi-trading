"""Weather-specific probability models extracted from probability."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from domain.shared.stats import _norm_cdf, _skew_normal_cdf, _student_t_cdf


PROJECT_DIR = Path(__file__).resolve().parents[4]
DEFAULT_BACKTEST_PATH = PROJECT_DIR / "data" / "backtest-results.json"

_log = logging.getLogger("probability")


def _calibration(load_calibration_func):
    if load_calibration_func is None:
        return {}
    calibration = load_calibration_func() or {}
    return calibration if isinstance(calibration, dict) else {}


def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None,
                        sigma_override=None, hour_of_day=None, skew=0.0,
                        load_calibration_func=None, logger=None):
    """CDF-based probability for weather markets."""
    if forecast_temp is None:
        return None

    log = logger or _log
    cal = _calibration(load_calibration_func)
    intercept = 1.5
    slope = 0.5

    weather_cal = cal.get("weather", {})
    city_cal = weather_cal.get("per_city", {}).get(city, {}) if city else {}
    if city_cal:
        intercept = city_cal.get("sigma_intercept", intercept)
        slope = city_cal.get("sigma_slope", slope)
    elif weather_cal.get("global_sigma_intercept") is not None:
        intercept = weather_cal["global_sigma_intercept"]
        slope = weather_cal.get("global_sigma_slope", slope)

    sigma = max(0.5, intercept + slope * math.sqrt(max(0, days_out)))

    if hour_of_day is not None and days_out == 0 and sigma_override is None:
        sigma = weather_sigma_hourly(
            days_out=0,
            city=city,
            hour_of_day=hour_of_day,
            load_calibration_func=load_calibration_func,
        )

    if sigma_override is not None and sigma_override > 0:
        sigma = sigma_override

    if skew == 0.0:
        if city_cal:
            skew = city_cal.get("skew", 0.0)
        else:
            skew = weather_cal.get("skew", 0.0)

    df = city_cal.get("df", weather_cal.get("df", 6))
    if not isinstance(df, (int, float)) or df < 2:
        log.warning("Invalid df=%s in calibration, using default df=6", df)
        df = 6

    if direction == "T":
        z = (threshold - forecast_temp) / sigma
        base_prob = 1.0 - _student_t_cdf(z, df)
        if abs(skew) > 1e-10:
            normal_prob = 1.0 - _norm_cdf(z)
            skew_prob = 1.0 - _skew_normal_cdf(z, alpha=skew)
            skew_correction = skew_prob - normal_prob
            base_prob = max(0.001, min(0.999, base_prob + skew_correction))
        return base_prob

    z_low = (threshold - forecast_temp) / sigma
    z_high = (threshold + 1 - forecast_temp) / sigma
    base_prob = _student_t_cdf(z_high, df) - _student_t_cdf(z_low, df)

    if abs(skew) > 1e-10:
        sn_high = _skew_normal_cdf(z_high, alpha=skew)
        sn_low = _skew_normal_cdf(z_low, alpha=skew)
        normal_diff = _norm_cdf(z_high) - _norm_cdf(z_low)
        skew_diff = sn_high - sn_low
        if abs(normal_diff) > 1e-10:
            ratio = skew_diff / normal_diff
            base_prob = max(0.0, base_prob * ratio)

    return base_prob


def weather_sigma(days_out=0, city=None, load_calibration_func=None):
    """Return the sigma used by weather_probability for a given horizon and city."""
    cal = _calibration(load_calibration_func)
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


def weather_sigma_hourly(days_out=0, city=None, hour_of_day=None, load_calibration_func=None):
    """Sigma with intra-day decay for day-0 forecasts."""
    base = weather_sigma(days_out, city, load_calibration_func=load_calibration_func)

    if hour_of_day is None or days_out > 0:
        return base

    if hour_of_day < 6:
        decay_factor = 1.2
    else:
        decay_factor = max(0.4, math.exp(-0.08 * (hour_of_day - 6)))

    return max(0.5, base * decay_factor)


def _resolve_weather_sigma(days_out=0, city=None, sigma_override=None, hour_of_day=None,
                           load_calibration_func=None):
    if sigma_override is not None and sigma_override > 0:
        return sigma_override
    if hour_of_day is not None and days_out == 0:
        return weather_sigma_hourly(
            days_out=0,
            city=city,
            hour_of_day=hour_of_day,
            load_calibration_func=load_calibration_func,
        )
    return weather_sigma(days_out, city, load_calibration_func=load_calibration_func)


def _normalize_weights(weight_map):
    positive = {
        model: weight for model, weight in weight_map.items()
        if isinstance(weight, (int, float)) and weight > 0
    }
    total = sum(positive.values())
    if total <= 0:
        return {}
    return {model: weight / total for model, weight in positive.items()}


def _default_ensemble_weights(model_names, days_out=0):
    if days_out <= 1:
        priors = {
            "hrrr": 1.30,
            "nam": 1.10,
            "nbm": 1.05,
            "gfs": 1.00,
            "ecmwf": 0.95,
            "graphcast": 1.00,
            "icon": 0.85,
            "aifs": 0.85,
            "nws": 1.00,
        }
    elif days_out <= 3:
        priors = {
            "ecmwf": 1.15,
            "graphcast": 1.10,
            "gfs": 1.00,
            "nbm": 1.00,
            "aifs": 0.95,
            "icon": 0.90,
            "nam": 0.75,
            "nws": 0.80,
        }
    else:
        priors = {
            "ecmwf": 1.20,
            "graphcast": 1.10,
            "gfs": 1.00,
            "aifs": 0.95,
            "icon": 0.90,
            "nbm": 0.85,
            "nws": 0.70,
        }

    weights = {}
    for model_name in model_names:
        weights[model_name] = priors.get(model_name, 1.0)
    return _normalize_weights(weights)


def _resolve_static_ensemble_weights(forecasts, days_out, ensemble_cal=None, static_weights=None):
    default_weights = _default_ensemble_weights(forecasts.keys(), days_out)
    if static_weights is None:
        static_weights = (ensemble_cal or {}).get("weights", {})
    if not static_weights:
        return default_weights

    merged = {}
    for model_name in forecasts:
        merged[model_name] = static_weights.get(model_name, default_weights.get(model_name, 0.0))
    normalized = _normalize_weights(merged)
    return normalized or default_weights


def _load_backtest_brier(backtest_path=DEFAULT_BACKTEST_PATH):
    """Load per-model Brier scores from backtest results for BMA weighting."""
    try:
        if backtest_path.exists():
            import json

            data = json.loads(backtest_path.read_text())
            per_model = data.get("weather", {}).get("per_model_brier")
            if per_model and isinstance(per_model, dict):
                if all(isinstance(value, (int, float)) and value > 0 for value in per_model.values()):
                    return per_model
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def ensemble_weather_probability(forecasts, threshold, direction, days_out=0, city=None,
                                 sigma_multiplier=1.0, static_weights=None,
                                 load_calibration_func=None, backtest_path=DEFAULT_BACKTEST_PATH,
                                 logger=None):
    """Weighted ensemble averaging for weather markets."""
    log = logger or _log
    cal = _calibration(load_calibration_func)
    ensemble_cal = cal.get("ensemble", {})

    brier_data = _load_backtest_brier(backtest_path=backtest_path)
    if brier_data:
        inv_brier = {}
        for model_name in forecasts:
            if model_name in brier_data:
                inv_brier[model_name] = 1.0 / brier_data[model_name]
        if len(inv_brier) >= 2:
            total_inv = sum(inv_brier.values())
            weights = {key: value / total_inv for key, value in inv_brier.items()}
            dropped = [model_name for model_name in forecasts if model_name not in inv_brier]
            if dropped:
                log.warning("BMA: dropping models with no Brier data: %s (weight=0)", dropped)
            log.debug("BMA weights from Brier: %s", weights)
        else:
            brier_data = None

    if not brier_data:
        weights = _resolve_static_ensemble_weights(
            forecasts,
            days_out,
            ensemble_cal,
            static_weights=static_weights,
        )

    sigma_kwarg = {}
    if sigma_multiplier != 1.0 and sigma_multiplier > 0:
        base_sigma = weather_sigma(days_out, city, load_calibration_func=load_calibration_func)
        sigma_kwarg["sigma_override"] = base_sigma * sigma_multiplier

    total_weight = 0.0
    weighted_prob = 0.0

    for model_name, temp in forecasts.items():
        weight = weights.get(model_name, 0.0)
        if weight <= 0:
            continue
        prob = weather_probability(
            temp,
            threshold,
            direction,
            days_out,
            city=city,
            load_calibration_func=load_calibration_func,
            logger=log,
            **sigma_kwarg,
        )
        if prob is None:
            continue
        weighted_prob += weight * prob
        total_weight += weight

    if total_weight <= 0:
        log.error("ensemble_weather_probability: zero total weight for models %s", list(forecasts.keys()))
        return None

    return weighted_prob / total_weight


def ensemble_spread_sigma_multiplier(spread_f):
    """Compute sigma multiplier based on ensemble model spread."""
    if spread_f <= 2.0:
        return 1.0
    raw = 1.0 + (spread_f - 2.0) * 0.125
    return min(raw, 2.5)


def _blend_weight_maps(default_weights, adaptive_weights, alpha):
    alpha = max(0.0, min(1.0, alpha))
    if alpha <= 0:
        return dict(default_weights)
    if alpha >= 1:
        return dict(adaptive_weights)

    merged = {}
    for model_name in set(default_weights) | set(adaptive_weights):
        merged[model_name] = (
            (1.0 - alpha) * default_weights.get(model_name, 0.0)
            + alpha * adaptive_weights.get(model_name, 0.0)
        )
    normalized = _normalize_weights(merged)
    return normalized or dict(default_weights)


def _adaptive_sample_confidence(model_payload, source):
    pending_market_n = max(0, int(model_payload.get("pending_market_n", 0) or 0))
    pending_snapshot_n = max(0, int(model_payload.get("pending_snapshot_n", 0) or 0))
    if source == "brier":
        resolved = max(0, int(model_payload.get("market_n", 0) or len(model_payload.get("brier_predictions", []))))
        effective = resolved + 0.10 * min(pending_market_n, 50)
        return min(1.0, effective / 40.0)

    resolved = max(0, int(model_payload.get("n", 0) or 0))
    effective = resolved + 0.05 * min(pending_snapshot_n, 50) + 0.10 * min(pending_market_n, 50)
    return min(1.0, effective / 60.0)


def compute_adaptive_ensemble_weights(verification_data, default_weights=None, return_details=False):
    """Compute model weights from recent forecast verification data."""
    if default_weights is None:
        default_weights = {"gfs": 0.40, "ecmwf": 0.40, "icon": 0.20}

    def _finalize(weights, method, blend_alpha, sample_confidence):
        details = {
            "method": method,
            "blend_alpha": round(blend_alpha, 4),
            "sample_confidence": round(sample_confidence, 4),
        }
        if return_details:
            return weights, details
        return weights

    if not verification_data:
        return _finalize(default_weights, "default", 0.0, 0.0)

    model_briers = {}
    min_samples = 20

    for model_name, data in verification_data.items():
        preds = data.get("brier_predictions", [])
        if len(preds) < min_samples:
            continue
        brier = sum((predicted - actual) ** 2 for predicted, actual in preds) / len(preds)
        if brier > 0:
            model_briers[model_name] = brier

    if len(model_briers) < 2:
        model_maes = {}
        min_mae_samples = 10
        for model_name, data in verification_data.items():
            mae = data.get("mae")
            n = data.get("n", 0)
            if isinstance(mae, (int, float)) and mae > 0 and n >= min_mae_samples:
                model_maes[model_name] = mae
        if len(model_maes) < 2:
            return _finalize(default_weights, "default", 0.0, 0.0)
        inv_mae = {model_name: 1.0 / mae for model_name, mae in model_maes.items()}
        total = sum(inv_mae.values())
        adaptive_weights = {model_name: weight / total for model_name, weight in inv_mae.items()}
        confidences = [
            _adaptive_sample_confidence(verification_data[model_name], "mae")
            for model_name in adaptive_weights
        ]
        blend_alpha = sum(confidences) / len(confidences) if confidences else 0.0
        weights = _blend_weight_maps(default_weights, adaptive_weights, blend_alpha)
        return _finalize(weights, "inverse_mae", blend_alpha, blend_alpha)

    inv_brier = {model_name: 1.0 / brier for model_name, brier in model_briers.items()}
    total = sum(inv_brier.values())
    adaptive_weights = {model_name: weight / total for model_name, weight in inv_brier.items()}
    confidences = [
        _adaptive_sample_confidence(verification_data[model_name], "brier")
        for model_name in adaptive_weights
    ]
    blend_alpha = sum(confidences) / len(confidences) if confidences else 0.0
    weights = _blend_weight_maps(default_weights, adaptive_weights, blend_alpha)
    return _finalize(weights, "inverse_brier", blend_alpha, blend_alpha)


def ensemble_disagreement_score(model_probs):
    """Compute disagreement between ensemble model probabilities."""
    if not model_probs or len(model_probs) < 2:
        return 0.0

    probs = [prob for prob in model_probs.values() if isinstance(prob, (int, float))]
    if len(probs) < 2:
        return 0.0
    mean_p = sum(probs) / len(probs)
    denom = math.sqrt(max(mean_p * (1.0 - mean_p), 1e-6))
    if denom <= 0:
        return 0.0

    variance = sum((prob - mean_p) ** 2 for prob in probs) / len(probs)
    std_dev = math.sqrt(variance)
    return min(1.0, std_dev / denom)


def ensemble_weather_probability_v2(forecasts, threshold, direction, days_out=0, city=None,
                                    sigma_multiplier=1.0, hour_of_day=None,
                                    verification_data=None, return_details=False,
                                    static_weights=None, skew=0.0,
                                    load_calibration_func=None,
                                    backtest_path=DEFAULT_BACKTEST_PATH,
                                    logger=None):
    """Enhanced ensemble with adaptive weights, hour-aware sigma, and disagreement scoring."""
    log = logger or _log
    cal = _calibration(load_calibration_func)
    ensemble_cal = cal.get("ensemble", {})

    if verification_data:
        defaults = _resolve_static_ensemble_weights(
            forecasts,
            days_out,
            ensemble_cal,
            static_weights=static_weights,
        )
        weights, adaptive_details = compute_adaptive_ensemble_weights(
            verification_data,
            default_weights=defaults,
            return_details=True,
        )
    else:
        adaptive_details = {"method": "static", "blend_alpha": 0.0, "sample_confidence": 0.0}
        brier_data = _load_backtest_brier(backtest_path=backtest_path)
        if brier_data:
            inv_brier = {}
            for model_name in forecasts:
                if model_name in brier_data:
                    inv_brier[model_name] = 1.0 / brier_data[model_name]
            if len(inv_brier) >= 2:
                total_inv = sum(inv_brier.values())
                weights = {key: value / total_inv for key, value in inv_brier.items()}
                adaptive_details = {"method": "backtest_brier", "blend_alpha": 1.0, "sample_confidence": 1.0}
            else:
                brier_data = None

        if not verification_data and not brier_data:
            weights = _resolve_static_ensemble_weights(
                forecasts,
                days_out,
                ensemble_cal,
                static_weights=static_weights,
            )

    sigma_kwarg = {}
    if sigma_multiplier != 1.0 and sigma_multiplier > 0:
        base_sigma = weather_sigma(days_out, city, load_calibration_func=load_calibration_func)
        sigma_kwarg["sigma_override"] = base_sigma * sigma_multiplier

    hour_kwarg = {}
    if hour_of_day is not None and days_out == 0 and "sigma_override" not in sigma_kwarg:
        hour_kwarg["hour_of_day"] = hour_of_day

    total_weight = 0.0
    weighted_prob = 0.0
    per_model_probs = {}
    weighted_center = 0.0

    for model_name, temp in forecasts.items():
        weight = weights.get(model_name, 0.0)
        if weight <= 0:
            continue
        prob = weather_probability(
            temp,
            threshold,
            direction,
            days_out,
            city=city,
            skew=skew,
            load_calibration_func=load_calibration_func,
            logger=log,
            **sigma_kwarg,
            **hour_kwarg,
        )
        if prob is None:
            continue
        per_model_probs[model_name] = prob
        weighted_prob += weight * prob
        weighted_center += weight * temp
        total_weight += weight

    if total_weight <= 0:
        if return_details:
            return (None, {
                "center_temp": None,
                "disagreement_score": 0.0,
                "method": "parametric_ensemble_v2",
                "per_model_probs": {},
                "sigma_used": None,
                "weights_used": weights,
            })
        return None

    final_prob = weighted_prob / total_weight
    center_temp = weighted_center / total_weight
    disagreement = ensemble_disagreement_score(per_model_probs)
    sigma_used = _resolve_weather_sigma(
        days_out=days_out,
        city=city,
        sigma_override=sigma_kwarg.get("sigma_override"),
        hour_of_day=hour_kwarg.get("hour_of_day"),
        load_calibration_func=load_calibration_func,
    )

    if return_details:
        return (final_prob, {
            "center_temp": center_temp,
            "disagreement_score": disagreement,
            "method": "parametric_ensemble_v2",
            "weights_used": {key: value for key, value in weights.items() if key in per_model_probs},
            "per_model_probs": per_model_probs,
            "sigma_used": sigma_used,
            "weight_method": adaptive_details.get("method", "static"),
            "verification_confidence": adaptive_details.get("sample_confidence", 0.0),
        })
    return final_prob


def empirical_ensemble_probability(member_temps, threshold, direction, bias_offset=0.0,
                                   extra_points=None, return_details=False):
    """Empirical CDF from raw ensemble member temperatures."""
    if not member_temps or len(member_temps) < 5:
        return None

    weighted_samples = [(temp - bias_offset, 1.0 / len(member_temps), "ensemble") for temp in member_temps]
    base_mass = 1.0
    extra_summary = {}
    for point in extra_points or []:
        if isinstance(point, dict):
            temp = point.get("temp")
            weight = point.get("weight")
            label = point.get("label") or point.get("model") or "extra"
        else:
            try:
                temp, weight, label = point
            except (TypeError, ValueError):
                continue
        if temp is None or not isinstance(weight, (int, float)) or weight <= 0:
            continue
        weighted_samples.append((temp - bias_offset, weight, label))
        base_mass += weight
        extra_summary[label] = round(extra_summary.get(label, 0.0) + weight, 4)

    normalized = [(temp, weight / base_mass, label) for temp, weight, label in weighted_samples]
    mean = sum(weight * temp for temp, weight, _ in normalized)
    variance = sum(weight * (temp - mean) ** 2 for temp, weight, _ in normalized)
    std = math.sqrt(max(variance, 0.0)) if variance > 0 else 0.5

    n_eff = 1.0 / max(sum(weight * weight for _, weight, _ in normalized), 1e-9)
    n_eff = max(n_eff, 2.0)
    h = max(0.5, 1.06 * std * n_eff ** (-1.0 / 5.0))

    if direction == "T":
        prob = sum(weight * (1.0 - _norm_cdf((threshold - temp) / h))
                   for temp, weight, _ in normalized)
    elif direction == "B":
        cdf_upper = sum(weight * _norm_cdf((threshold + 1 - temp) / h)
                        for temp, weight, _ in normalized)
        cdf_lower = sum(weight * _norm_cdf((threshold - temp) / h)
                        for temp, weight, _ in normalized)
        prob = cdf_upper - cdf_lower
    else:
        return None

    prob = max(0.01, min(0.99, prob))
    if return_details:
        return (prob, {
            "bandwidth": h,
            "center_temp": mean,
            "effective_sample_size": n_eff,
            "extra_weights": extra_summary,
            "method": "empirical_ensemble",
            "member_count": len(member_temps),
            "sigma_used": max(0.5, std),
        })
    return prob


def nws_sigma_for_hour(hour_of_day, load_calibration_func=None):
    """NWS temperature uncertainty for a given hour."""
    cal = _calibration(load_calibration_func)
    nws_section = cal.get("nws", {})
    nws_cal = nws_section.get("sigma_by_hour", {})

    if nws_cal:
        if hour_of_day < 6:
            return max(0.5, nws_cal.get("overnight", 5.0))
        if hour_of_day >= 17:
            return max(0.5, nws_cal.get("17+", 0.5))
        if hour_of_day >= 15:
            return max(0.5, nws_cal.get("15-16", 1.5))
        return max(0.5, nws_cal.get("before_15", 3.0))

    if hour_of_day < 6:
        return max(0.5, 5.0)
    return max(0.5, 4.0 * math.exp(-0.18 * (hour_of_day - 6)))


def nws_probability(running_high, threshold, direction, hour_of_day,
                    load_calibration_func=None, logger=None):
    """Probability for NWS actual-temp arbitrage."""
    log = logger or _log
    cal = _calibration(load_calibration_func)
    nws_df = cal.get("nws", {}).get("df", 6)
    if not isinstance(nws_df, (int, float)) or nws_df < 2:
        log.warning("Invalid nws_df=%s in calibration, using default df=6", nws_df)
        nws_df = 6

    sigma = nws_sigma_for_hour(hour_of_day, load_calibration_func=load_calibration_func)

    if direction == "T":
        z = (threshold - running_high) / sigma
        return 1.0 - _student_t_cdf(z, nws_df)

    z_low = (threshold - running_high) / sigma
    z_high = (threshold + 1 - running_high) / sigma
    return _student_t_cdf(z_high, nws_df) - _student_t_cdf(z_low, nws_df)


__all__ = [
    "compute_adaptive_ensemble_weights",
    "ensemble_disagreement_score",
    "ensemble_spread_sigma_multiplier",
    "ensemble_weather_probability",
    "ensemble_weather_probability_v2",
    "empirical_ensemble_probability",
    "nws_probability",
    "nws_sigma_for_hour",
    "weather_probability",
    "weather_sigma",
    "weather_sigma_hourly",
]
