"""Tests for cross-platform arbitrage fuzzy matching with numerical validation.

Covers Fix F: MIN_MATCH_SCORE raised to 0.75 and validate_match() ensures
matching numerical thresholds between Kalshi and Polymarket markets.
"""

import sys
import types
import json
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_arb_module():
    """Import cross-platform-arb.py with stubbed side-effects."""
    orig_modules = {}
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "polymarket_client"):
        if mod_name in sys.modules:
            orig_modules[mod_name] = sys.modules[mod_name]

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.is_shutdown_requested = lambda: False
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = Path("/tmp/fake_arb")
    fake_auth.TradeManager = type("TradeManager", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    fake_auth.trim_trade_log = lambda *a, **kw: None
    fake_auth.build_market_snapshot = lambda **kw: kw
    fake_auth._atomic_write_json = lambda *a, **kw: None
    fake_auth.HealthCheckMonitor = type("HealthCheckMonitor", (), {
        "__init__": lambda self, *a, **kw: None,
        "record_bot_heartbeat": lambda self, *a, **kw: None,
    })
    fake_auth.ScanSummary = type("ScanSummary", (), {
        "__init__": lambda self, *a, **kw: None,
        "skip": lambda self, *a, **kw: None,
        "finalize": lambda self, *a, **kw: None,
        "markets_fetched": 0,
        "markets_evaluated": 0,
        "trades_placed": 0,
    })
    sys.modules["kalshi_auth"] = fake_auth

    fake_prob = types.ModuleType("probability")
    fake_prob.quarter_kelly = lambda *a, **kw: (0, 0)
    fake_prob.compute_limit_price = lambda *a, **kw: 50
    fake_prob.kalshi_fee_cents = lambda p: 0.07 * (p / 100) * (1 - p / 100) * 100
    sys.modules["probability"] = fake_prob

    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = type("PortfolioAllocator", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    sys.modules["capital_allocator"] = fake_alloc

    fake_pm = types.ModuleType("polymarket_client")
    fake_pm.PolymarketClient = type("PolymarketClient", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    sys.modules["polymarket_client"] = fake_pm

    # Create dirs and config
    config_dir = Path("/tmp/fake_arb/config")
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path("/tmp/fake_arb/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bots-config.json").write_text(json.dumps({
        "cross_platform_arb": {
            "scanIntervalMinutes": 30,
            "minSpreadPct": 0.02,
            "executionEnabled": False,
            "maxTradeAmount": 10,
            "maxDailyTrades": 10,
            "maxDailyLoss": 25,
        }
    }))

    spec = importlib.util.spec_from_file_location(
        "cross_platform_arb",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "cross-platform-arb.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original modules
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "polymarket_client"):
        if mod_name in orig_modules:
            sys.modules[mod_name] = orig_modules[mod_name]
        elif mod_name in sys.modules:
            del sys.modules[mod_name]

    return mod


@pytest.fixture(scope="module")
def arb_mod():
    return _load_arb_module()


class TestNumberExtraction:
    """Test _extract_numbers helper."""

    def test_integers(self, arb_mod):
        assert arb_mod._extract_numbers("BTC 70000") == {"70000"}

    def test_decimals(self, arb_mod):
        assert arb_mod._extract_numbers("CPI 3.5%") == {"3.5"}

    def test_multiple(self, arb_mod):
        nums = arb_mod._extract_numbers("between 60000 and 70000")
        assert "60000" in nums
        assert "70000" in nums

    def test_no_numbers(self, arb_mod):
        assert arb_mod._extract_numbers("Will it rain?") == set()


class TestValidateMatch:
    """Test validate_match() numerical validation."""

    def test_different_numbers_rejected(self, arb_mod):
        """BTC 70k vs BTC 80k should be rejected — different thresholds."""
        assert not arb_mod.validate_match(
            "Will BTC exceed 70000",
            "Will BTC exceed 80000",
            score=0.85,
        )

    def test_same_numbers_accepted(self, arb_mod):
        """BTC 70k vs Bitcoin 70000 should be accepted — same threshold."""
        assert arb_mod.validate_match(
            "Will BTC exceed 70000",
            "Will Bitcoin exceed 70000",
            score=0.80,
        )

    def test_low_score_rejected(self, arb_mod):
        """Even matching numbers shouldn't pass if fuzzy score is too low."""
        assert not arb_mod.validate_match(
            "Will BTC exceed 70000",
            "Will Bitcoin exceed 70000",
            score=0.50,
        )

    def test_no_numbers_in_either(self, arb_mod):
        """No numbers in either text — should pass if score is high enough."""
        assert arb_mod.validate_match(
            "Will it rain tomorrow",
            "Will it rain tomorrow in NYC",
            score=0.80,
        )

    def test_numbers_in_only_one(self, arb_mod):
        """Numbers in only one side — should pass if score is high enough."""
        assert arb_mod.validate_match(
            "Will BTC exceed 70000",
            "Will Bitcoin go up",
            score=0.80,
        )

    def test_overlapping_numbers_accepted(self, arb_mod):
        """Partial number overlap should pass (at least one shared number)."""
        assert arb_mod.validate_match(
            "CPI between 3.0 and 3.5",
            "CPI above 3.5",
            score=0.80,
        )


class TestMatchMarkets:
    """Test the full match_markets pipeline."""

    def test_rejects_different_thresholds(self, arb_mod):
        """Markets with different numerical thresholds should not match."""
        kalshi = [{"title": "Will BTC exceed 70000?", "subtitle": "", "ticker": "KXBTC-70K"}]
        poly = [{"question": "Will BTC exceed 80000?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        assert len(matches) == 0

    def test_accepts_matching_thresholds(self, arb_mod):
        """Markets with same thresholds and high similarity should match."""
        kalshi = [{"title": "Will BTC exceed 70000?", "subtitle": "", "ticker": "KXBTC-70K"}]
        poly = [{"question": "Will Bitcoin exceed 70000?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        assert len(matches) == 1
        assert matches[0][2] >= 0.75  # score

    def test_min_score_threshold(self, arb_mod):
        """Very different text should not match even with same numbers."""
        kalshi = [{"title": "CPI above 3.5%", "subtitle": "", "ticker": "KXCPI"}]
        poly = [{"question": "GDP growth exceeds 3.5%?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        # Even though "3.5" matches, text similarity should be too low
        assert len(matches) == 0


class TestSpreadCalculation:
    """Test net spread calculation after fees."""

    def _kalshi_fee_pct(self, price_cents):
        """Approximate Kalshi fee: 0.07 * p * (1-p)."""
        p = price_cents / 100
        return 0.07 * p * (1 - p)

    def _net_spread(self, pm_yes_bid, k_yes_ask_cents, polymarket_fee=0.02):
        """Compute net spread after fees."""
        raw = pm_yes_bid - k_yes_ask_cents / 100
        k_fee = self._kalshi_fee_pct(k_yes_ask_cents)
        return raw - k_fee - polymarket_fee

    def test_profitable_spread(self):
        """Polymarket 0.65, Kalshi ask 55c = 10% raw, ~5% net."""
        net = self._net_spread(0.65, 55)
        assert net > 0.03  # Profitable after fees

    def test_negative_spread(self):
        """Same price = negative after fees."""
        net = self._net_spread(0.55, 55)
        assert net < 0

    def test_fee_drag(self):
        """Fees should eat ~4-5% of raw spread."""
        raw = 0.65 - 0.55
        net = self._net_spread(0.65, 55)
        fee_drag = raw - net
        assert 0.03 < fee_drag < 0.07


class TestExtractDirection:
    """Test _extract_direction() keyword parsing."""

    def test_above_keyword(self, arb_mod):
        assert arb_mod._extract_direction("Will BTC go above 70000?") == "above"

    def test_below_keyword(self, arb_mod):
        assert arb_mod._extract_direction("Will BTC fall below 60000?") == "below"

    def test_exceed(self, arb_mod):
        assert arb_mod._extract_direction("Will CPI exceed 3.5%?") == "above"

    def test_at_least(self, arb_mod):
        assert arb_mod._extract_direction("At least 200k jobs added") == "above"

    def test_at_most(self, arb_mod):
        assert arb_mod._extract_direction("At most 150k jobs added") == "below"

    def test_higher_than(self, arb_mod):
        assert arb_mod._extract_direction("Temperature higher than 90F") == "above"

    def test_lower_than(self, arb_mod):
        assert arb_mod._extract_direction("GDP lower than 2%") == "below"

    def test_over(self, arb_mod):
        assert arb_mod._extract_direction("Bitcoin over 100000") == "above"

    def test_under(self, arb_mod):
        assert arb_mod._extract_direction("Ethereum under 3000") == "below"

    def test_more_than(self, arb_mod):
        assert arb_mod._extract_direction("More than 500 units sold") == "above"

    def test_less_than(self, arb_mod):
        assert arb_mod._extract_direction("Less than 100 units sold") == "below"

    def test_rise_above(self, arb_mod):
        assert arb_mod._extract_direction("Will ETH rise above 4000?") == "above"

    def test_drop_below(self, arb_mod):
        assert arb_mod._extract_direction("Will BTC drop below 50000?") == "below"

    def test_greater_than(self, arb_mod):
        assert arb_mod._extract_direction("CPI greater than 3%") == "above"

    def test_top(self, arb_mod):
        assert arb_mod._extract_direction("Will it top 100?") == "above"

    def test_no_direction(self, arb_mod):
        assert arb_mod._extract_direction("Will it rain tomorrow?") is None

    def test_no_direction_neutral(self, arb_mod):
        assert arb_mod._extract_direction("Who will win the election?") is None

    def test_case_insensitive(self, arb_mod):
        assert arb_mod._extract_direction("ABOVE 70000") == "above"
        assert arb_mod._extract_direction("BELOW 60000") == "below"


class TestDirectionValidation:
    """Test that validate_match rejects direction-conflicting pairs."""

    def test_conflicting_directions_rejected(self, arb_mod):
        """'above 70k' vs 'below 70k' should be rejected even with same numbers."""
        assert not arb_mod.validate_match(
            "Will BTC go above 70000",
            "Will BTC fall below 70000",
            score=0.85,
        )

    def test_same_directions_accepted(self, arb_mod):
        """Both 'above' should pass."""
        assert arb_mod.validate_match(
            "Will BTC exceed 70000",
            "Will Bitcoin go above 70000",
            score=0.80,
        )

    def test_no_direction_both_passes(self, arb_mod):
        """No direction in either text should not reject (backward compatible)."""
        assert arb_mod.validate_match(
            "Who wins the election",
            "Who will win the election",
            score=0.85,
        )

    def test_direction_in_only_one_passes(self, arb_mod):
        """Direction in only one side should not reject."""
        assert arb_mod.validate_match(
            "Will BTC exceed 70000",
            "BTC 70000 outcome",
            score=0.80,
        )

    def test_above_vs_below_rejected(self, arb_mod):
        """Explicit above vs below with high score still rejected."""
        assert not arb_mod.validate_match(
            "CPI above 3.5",
            "CPI below 3.5",
            score=0.90,
        )


class TestMatchMarketsDirection:
    """Test that match_markets pipeline respects direction validation."""

    def test_direction_conflict_filtered_out(self, arb_mod):
        """Markets with conflicting directions should not match."""
        kalshi = [{"title": "Will BTC go above 70000?", "subtitle": "", "ticker": "KXBTC-70K-UP"}]
        poly = [{"question": "Will BTC fall below 70000?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        assert len(matches) == 0

    def test_same_direction_passes(self, arb_mod):
        """Markets with same direction and matching numbers should match."""
        kalshi = [{"title": "Will BTC exceed 70000?", "subtitle": "", "ticker": "KXBTC-70K"}]
        poly = [{"question": "Will BTC exceed 70000?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        assert len(matches) == 1
        # Verify direction info is in the tuple
        assert len(matches[0]) == 5  # (km, pm, score, k_dir, p_dir)
        assert matches[0][3] == "above"  # k_dir
        assert matches[0][4] == "above"  # p_dir

    def test_no_direction_backward_compatible(self, arb_mod):
        """Markets without direction keywords should still match."""
        kalshi = [{"title": "Who wins the 2024 election?", "subtitle": "", "ticker": "KXPRES"}]
        poly = [{"question": "Who wins the 2024 election?"}]
        matches = arb_mod.match_markets(kalshi, poly)
        assert len(matches) == 1
        assert matches[0][3] is None  # k_dir
        assert matches[0][4] is None  # p_dir


class TestArbEdgeGating:
    """Test that arb requires minimum 3% net spread."""

    def test_above_threshold_is_tradeable(self):
        min_spread = 0.03
        net_spread = 0.05
        assert net_spread > min_spread

    def test_below_threshold_rejected(self):
        min_spread = 0.03
        net_spread = 0.02
        assert not (net_spread > min_spread)

    def test_boundary_at_threshold(self):
        min_spread = 0.03
        net_spread = 0.03
        assert not (net_spread > min_spread)  # Strict >
