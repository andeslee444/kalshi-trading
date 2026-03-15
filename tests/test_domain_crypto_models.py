"""Direct tests for the extracted domain.crypto.models module."""

from domain.crypto.models import (
    crypto_price_probability,
    crypto_price_probability_heston,
    crypto_price_probability_jd,
)


def test_crypto_price_probability_ou_path_changes_prob_direct_module():
    gbm = crypto_price_probability(
        70000,
        69000,
        "above",
        time_horizon_minutes=60,
        realized_vol_pct=0.60,
    )
    ou = crypto_price_probability(
        70000,
        69000,
        "above",
        time_horizon_minutes=60,
        realized_vol_pct=0.60,
        use_ou=True,
        ou_half_life_minutes=120,
        ou_target=68000,
    )

    assert abs(ou - gbm) > 0.001


def test_crypto_price_probability_jd_stays_bounded_direct_module():
    prob = crypto_price_probability_jd(
        80000,
        90000,
        "above",
        time_horizon_minutes=1440,
        realized_vol_pct=0.60,
    )

    assert 0.0 <= prob <= 1.0


def test_crypto_price_probability_heston_complements_direct_module():
    above = crypto_price_probability_heston(
        current_price=80000,
        threshold=82000,
        direction="above",
        time_horizon_minutes=60,
        v0=0.25,
        kappa=2.0,
        theta=0.25,
        xi=0.3,
        rho=-0.7,
    )
    below = crypto_price_probability_heston(
        current_price=80000,
        threshold=82000,
        direction="below",
        time_horizon_minutes=60,
        v0=0.25,
        kappa=2.0,
        theta=0.25,
        xi=0.3,
        rho=-0.7,
    )

    assert abs(above + below - 1.0) < 0.01
