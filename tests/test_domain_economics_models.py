"""Direct tests for the extracted domain.economics.models module."""

import pytest

from domain.economics.models import (
    cpi_nowcast_sigma,
    econ_nowcast_probability,
    gdp_nowcast_sigma,
)


def test_econ_nowcast_probability_uses_calibrated_df_direct_module():
    calibration = {"economics": {"df": 30}}

    calibrated = econ_nowcast_probability(
        3.0,
        0.10,
        3.30,
        "above",
        load_calibration_func=lambda: calibration,
    )
    explicit = econ_nowcast_probability(3.0, 0.10, 3.30, "above", df=30)

    assert calibrated == pytest.approx(explicit)


def test_cpi_nowcast_sigma_uses_calibration_override_direct_module():
    calibration = {"cpi": {"sigma_by_days": {"0": 0.008, "7": 0.055}}}

    assert cpi_nowcast_sigma(0, load_calibration_func=lambda: calibration) == 0.008
    assert cpi_nowcast_sigma(7, load_calibration_func=lambda: calibration) == 0.055


def test_gdp_nowcast_sigma_monotonic_direct_module():
    assert gdp_nowcast_sigma(7) > gdp_nowcast_sigma(0)
