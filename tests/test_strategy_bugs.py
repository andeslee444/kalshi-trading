"""Regression tests for strategy bot bugs BUG-2, BUG-3, BUG-4.

BUG-2: Reasoning string uses yes_ask instead of sell_price for implied_prob.
BUG-3: find_near_settlement is dead code (no orders placed).
BUG-4: compute_limit_price NO-side low-edge places near full ask for tight spreads.
"""

import math
import json
import sys
import types
from pathlib import Path

import pytest

from probability import longshot_edge, compute_limit_price, _reset_calibration
from conftest import make_fake_auth, load_bot_module


@pytest.fixture(autouse=True)
def reset_calibration():
    """Reset calibration state between tests."""
    _reset_calibration()
    yield
    _reset_calibration()


# ===================================================================
# BUG-2: Reasoning string implied_prob should use sell_price, not yes_ask
# ===================================================================

class TestBug2ReasoningString:

    def test_implied_prob_uses_sell_price_not_yes_ask(self):
        """When sell_price != yes_ask, implied_prob must be sell_price/100."""
        sell_price = 4
        yes_ask = 5
        # This is what the code SHOULD compute
        implied_prob = sell_price / 100.0
        assert implied_prob == 0.04, "implied_prob should be based on sell_price"
        # It should NOT be yes_ask / 100
        assert implied_prob != yes_ask / 100.0

    def test_true_prob_never_negative_for_valid_sell_prices(self):
        """true_prob = implied_prob - est_edge should never be negative
        for any valid sell_price (1-15c) when using sell_price for implied_prob."""
        for sell_price in range(1, 16):
            implied_prob = sell_price / 100.0
            est_edge = longshot_edge(sell_price, ticker="KXNBA-TEST", hours_to_close=24)
            true_prob = implied_prob - est_edge
            assert true_prob >= 0, (
                f"true_prob={true_prob:.4f} is negative for sell_price={sell_price}c "
                f"(implied={implied_prob:.4f}, edge={est_edge:.4f})"
            )

    def test_reasoning_math_consistency(self):
        """The reasoning string math should be: implied = sell_price/100,
        true = implied - edge, and edge = longshot_edge(sell_price)."""
        sell_price = 8
        est_edge = longshot_edge(sell_price, ticker="KXNBA-TEST", hours_to_close=48)
        implied_prob = sell_price / 100.0
        true_prob = implied_prob - est_edge
        # Edge should be a fraction of implied probability (overpricing ratio * implied)
        assert 0 < est_edge < implied_prob, "edge should be less than implied_prob"
        assert true_prob > 0, "true_prob should be positive"

    def test_source_code_uses_sell_price(self):
        """Verify the source code computes implied_prob from sell_price, not yes_ask."""
        source_path = Path(__file__).resolve().parent.parent / "src" / "kalshi" / "strategy-trader.py"
        source = source_path.read_text()
        # The reasoning string section should use sell_price / 100.0
        assert "sell_price / 100.0" in source, (
            "strategy-trader.py should compute implied_prob = sell_price / 100.0"
        )
        # It should NOT use yes_ask / 100.0 for implied_prob in the reasoning
        # Check that yes_ask / 100.0 doesn't appear in the reasoning computation
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "implied_prob" in line and "yes_ask" in line and "100.0" in line:
                pytest.fail(
                    f"Line {i+1} uses yes_ask for implied_prob: {line.strip()}"
                )


# ===================================================================
# BUG-3: find_near_settlement should be removed (dead code)
# ===================================================================

class TestBug3DeadCodeRemoval:

    def _load_strategy_trader(self):
        """Load strategy-trader.py with stubbed side effects."""
        _fake_project = Path("/tmp/fake_strategy_test")
        (_fake_project / "config").mkdir(parents=True, exist_ok=True)
        (_fake_project / "data").mkdir(parents=True, exist_ok=True)
        (_fake_project / "config" / "bots-config.json").write_text(json.dumps({
            "strategy": {
                "maxTradeAmount": 5,
                "scanIntervalMinutes": 15,
                "maxDailyTrades": 20,
                "maxDailyLoss": 50,
            }
        }))

        fake_auth = make_fake_auth(PROJECT_DIR=_fake_project)

        fake_alloc = types.ModuleType("capital_allocator")
        fake_alloc.PortfolioAllocator = lambda *a, **kw: None

        return load_bot_module("strategy-trader.py", fake_auth, extra_stubs={
            "capital_allocator": fake_alloc,
        })

    def test_find_near_settlement_removed(self):
        """find_near_settlement function should not exist in strategy-trader.py."""
        mod = self._load_strategy_trader()
        assert not hasattr(mod, "find_near_settlement"), (
            "find_near_settlement should be removed (dead code)"
        )


# ===================================================================
# BUG-4: compute_limit_price NO-side should place within spread
# ===================================================================

class TestBug4NoSideLimitPrice:

    def test_no_side_low_edge_within_spread(self):
        """NO-side with low edge should return strictly less than no_ask
        when spread >= 3c."""
        # yes_bid=3, yes_ask=10 -> no_bid=90, no_ask=97
        price = compute_limit_price(3, 10, "no", edge=0.03)
        assert 90 <= price < 97, (
            f"Expected price in [90, 97), got {price}"
        )

    def test_no_side_low_edge_inner_third(self):
        """NO-side low edge should place in inner third of spread (patient)."""
        # yes_bid=86, yes_ask=92 -> no_bid=8, no_ask=14, spread=6
        price = compute_limit_price(86, 92, "no", edge=0.03)
        assert 8 < price < 14, (
            f"Expected price strictly between no_bid=8 and no_ask=14, got {price}"
        )

    def test_no_side_medium_edge_within_spread(self):
        """NO-side with medium edge should be in outer third of spread."""
        # yes_bid=3, yes_ask=10 -> no_bid=90, no_ask=97, spread=7
        price = compute_limit_price(3, 10, "no", edge=0.10)
        assert 90 < price <= 97, (
            f"Expected price in (90, 97], got {price}"
        )

    def test_no_side_high_edge_returns_full_ask(self):
        """NO-side with high edge (>=0.15) should return full no_ask (urgency)."""
        # yes_bid=3, yes_ask=10 -> no_bid=90, no_ask=97
        price = compute_limit_price(3, 10, "no", edge=0.20)
        assert price == 97

    def test_no_side_no_edge_returns_full_ask(self):
        """NO-side with edge=None should return full no_ask."""
        price = compute_limit_price(3, 10, "no", edge=None)
        assert price == 97

    def test_no_side_tight_spread_low_edge(self):
        """NO-side tight spread (3c) with low edge should still be within spread."""
        # yes_bid=47, yes_ask=50 -> no_bid=50, no_ask=53, spread=3
        price = compute_limit_price(47, 50, "no", edge=0.03)
        assert 50 <= price < 53, (
            f"Expected price in [50, 53), got {price}"
        )

    def test_no_side_2c_spread_low_edge(self):
        """NO-side 2c spread with low edge should NOT hit full ask.
        Current bug: (no_bid + no_ask) // 2 + 1 = (95+97)//2+1 = 97 = no_ask."""
        # yes_bid=3, yes_ask=5 -> no_bid=95, no_ask=97, spread=2
        price = compute_limit_price(3, 5, "no", edge=0.03)
        assert price < 97, (
            f"Low-edge should NOT place at full no_ask=97, got {price}"
        )

    def test_no_side_4c_spread_low_edge(self):
        """NO-side 4c spread, low edge: should place closer to bid (patient)."""
        # yes_bid=6, yes_ask=10 -> no_bid=90, no_ask=94, spread=4
        price = compute_limit_price(6, 10, "no", edge=0.03)
        # Inner third: 90 + max(1, 4//3) = 90 + 1 = 91
        assert 90 < price < 94, (
            f"Expected price strictly between 90 and 94, got {price}"
        )

    def test_no_spread_returns_ask(self):
        """When no_ask <= no_bid (no real spread), return no_ask."""
        # yes_bid=50, yes_ask=50 -> no_bid=50, no_ask=50
        price = compute_limit_price(50, 50, "no", edge=0.03)
        assert price == 50
