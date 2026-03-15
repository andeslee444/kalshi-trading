"""Direct tests for the extracted domain.shared.sizing module."""

import pytest

from domain.shared.sizing import (
    KALSHI_FEE_RATE,
    compute_limit_price,
    half_kelly,
    is_market_liquid,
    kalshi_fee_cents,
)


def test_kalshi_fee_cents_uses_shared_rate():
    assert KALSHI_FEE_RATE == 0.07
    assert kalshi_fee_cents(50) == pytest.approx(1.75)


def test_half_kelly_returns_details_from_extracted_module():
    contracts, risk, details = half_kelly(
        0.12,
        45,
        5_000,
        bankroll_cents=20_000,
        return_details=True,
    )

    assert contracts > 0
    assert risk == contracts * 45
    assert details["bankroll_used"] == 20_000
    assert details["kelly_fraction"] > 0


def test_is_market_liquid_uses_shared_thresholds():
    assert is_market_liquid({"yes_bid": 40, "yes_ask": 50, "volume": 10}) is True
    assert is_market_liquid({"yes_bid": 40, "yes_ask": 61, "volume": 100}) is False


def test_compute_limit_price_places_no_side_inside_spread_for_low_edge():
    assert compute_limit_price(3, 5, "no", edge=0.03) == 96
