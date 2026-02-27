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
