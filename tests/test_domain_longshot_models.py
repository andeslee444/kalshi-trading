"""Direct tests for the extracted domain.longshot.models module."""

from domain.longshot.models import (
    LONGSHOT_BIAS_PARAMS,
    classify_ticker_category,
    longshot_edge,
)


def test_classify_ticker_category_direct_module():
    assert classify_ticker_category("KXOSCARS-BEST") == "entertainment"


def test_longshot_edge_respects_category_strength_direct_module():
    sports = longshot_edge(5, ticker="KXNBA-GAME", hours_to_close=24)
    weather = longshot_edge(5, ticker="KXHIGHMIA-DAY", hours_to_close=24)

    assert sports > weather


def test_longshot_bias_params_exposes_default_direct_module():
    assert LONGSHOT_BIAS_PARAMS["default"] == (0.57, 0.15)
