"""Direct tests for the extracted domain.weather.models module."""

from domain.weather.models import (
    compute_adaptive_ensemble_weights,
    nws_probability,
    weather_probability,
    weather_sigma_hourly,
)


def test_weather_probability_respects_city_specific_df_direct_module():
    calibration = {
        "weather": {
            "df": 30,
            "per_city": {"MIA": {"df": 4}},
        }
    }

    mia_prob = weather_probability(
        86.0,
        84.0,
        "T",
        city="MIA",
        load_calibration_func=lambda: calibration,
    )
    ny_prob = weather_probability(
        86.0,
        84.0,
        "T",
        city="NY",
        load_calibration_func=lambda: calibration,
    )

    assert mia_prob != ny_prob


def test_weather_sigma_hourly_decays_for_day_zero_direct_module():
    assert weather_sigma_hourly(days_out=0, hour_of_day=17) < weather_sigma_hourly(days_out=0, hour_of_day=6)


def test_compute_adaptive_ensemble_weights_prefers_lower_brier_direct_module():
    weights = compute_adaptive_ensemble_weights({
        "gfs": {"brier_predictions": [(0.8, 1)] * 25 + [(0.2, 0)] * 25},
        "ecmwf": {"brier_predictions": [(0.6, 1)] * 25 + [(0.4, 0)] * 25},
        "icon": {"brier_predictions": [(0.5, 1)] * 25 + [(0.5, 0)] * 25},
    })

    assert weights["gfs"] > weights["ecmwf"] > weights["icon"]


def test_nws_probability_falls_later_in_day_when_threshold_not_reached_direct_module():
    morning = nws_probability(86.0, 87.0, "T", 10)
    afternoon = nws_probability(86.0, 87.0, "T", 17)

    assert afternoon < morning


def test_nws_probability_is_deterministic_once_threshold_crossed_direct_module():
    assert nws_probability(86.1, 86.0, "T", 8) == 1.0


def test_nws_bracket_probability_is_zero_once_upper_bound_crossed_direct_module():
    assert nws_probability(87.0, 86.0, "B", 8) == 0.0
