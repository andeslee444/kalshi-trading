"""Direct tests for the extracted domain.entertainment.models module."""

from domain.entertainment.models import (
    album_data_sigma,
    boxoffice_data_sigma,
    info_arb_probability,
)


def test_info_arb_probability_midpoint_direct_module():
    assert 0.49 < info_arb_probability(150000, 150000, 0.05) < 0.51


def test_album_data_sigma_uses_calibrated_source_direct_module():
    calibration = {
        "album_sales": {
            "sigma_by_source": {"hdd-article": 0.22},
        }
    }

    assert album_data_sigma(2, source="hdd-article", load_calibration_func=lambda: calibration) == 0.22


def test_boxoffice_data_sigma_staleness_increases_uncertainty_direct_module():
    fresh = boxoffice_data_sigma(4, hours_since_publication=0)
    stale = boxoffice_data_sigma(4, hours_since_publication=96)

    assert stale > fresh
