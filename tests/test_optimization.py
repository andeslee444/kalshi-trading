"""Tests for optimization changes: longshot_edge, compute_limit_price,
continuous NWS sigma, risk-adjusted daily loss, PortfolioAllocator,
ensemble weather, economics, crypto models, city exposure tracking,
sell_position, and BeatRelease scanner (ticker map, content hashing, state migration).

Covers:
  - probability.py: longshot_edge(), classify_ticker_category(), compute_limit_price()
  - probability.py: continuous NWS sigma model (no calibration overrides)
  - probability.py: ensemble_weather_probability(), econ_nowcast_probability(), crypto_price_probability()
  - kalshi_auth.py: TradeManager risk-adjusted daily loss tracking, sell_position()
  - capital_allocator.py: PortfolioAllocator, city-level exposure tracking
  - beatrelease-scanner.py: build_ticker_map(), content_hash(), state migration
"""

import math
import json
import types
import sys
import importlib.util
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from probability import (
    longshot_edge,
    classify_ticker_category,
    compute_limit_price,
    nws_probability,
    half_kelly,
    half_kelly_sell,
    quarter_kelly,
    high_conviction_kelly,
    is_market_liquid,
    ensemble_weather_probability,
    econ_nowcast_probability,
    cpi_nowcast_sigma,
    crypto_price_probability,
    _reset_calibration,
    LONGSHOT_BIAS_PARAMS,
)
from kalshi_auth import TradeManager, load_trades
from capital_allocator import (
    PortfolioAllocator, BudgetResponse, MAX_TICKER_FRACTION,
    MAX_CITY_FRACTION, _extract_city_key,
)


# ---------------------------------------------------------------------------
# Import beatrelease-scanner.py (hyphenated filename, heavy side-effects)
# ---------------------------------------------------------------------------

def _load_beatrelease_scanner():
    """Import beatrelease-scanner.py with stubbed-out side effects."""
    orig_auth = sys.modules.get("kalshi_auth")

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = Path("/tmp/fake_beatrelease")
    fake_auth.fetch_parallel = lambda *a, **kw: {}
    fake_auth.retry_request = lambda *a, **kw: None
    fake_auth.TradeManager = type("TradeManager", (), {
        "__init__": lambda self, *a, **kw: None,
        "place_order": lambda self, *a, **kw: None,
    })
    fake_auth.trim_trade_log = lambda *a, **kw: None
    sys.modules["kalshi_auth"] = fake_auth

    # Create config/data dirs and a minimal bots-config.json
    config_dir = Path("/tmp/fake_beatrelease/config")
    config_dir.mkdir(parents=True, exist_ok=True)
    keys_dir = config_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    deepseek_key = keys_dir / "deepseek.txt"
    deepseek_key.write_text("test-key")
    bots_config = config_dir / "bots-config.json"
    bots_config.write_text(json.dumps({
        "beatrelease": {
            "checkIntervalHours": 1,
            "maxTradeCents": 500,
            "maxDailyTrades": 10,
            "maxDailyLoss": 25,
            "blogUrls": ["https://www.beatrelease.com/blog"],
        }
    }))
    data_dir = Path("/tmp/fake_beatrelease/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    pids_dir = data_dir / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "beatrelease_scanner",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "beatrelease-scanner.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["beatrelease_scanner"] = mod
    spec.loader.exec_module(mod)

    if orig_auth is not None:
        sys.modules["kalshi_auth"] = orig_auth
    else:
        del sys.modules["kalshi_auth"]

    return mod


_br_mod = _load_beatrelease_scanner()


# ===================================================================
# longshot_edge tests
# ===================================================================

class TestLongshotEdge:

    def test_returns_zero_for_invalid_price(self):
        assert longshot_edge(0) == 0.0
        assert longshot_edge(-5) == 0.0
        assert longshot_edge(100) == 0.0

    def test_positive_for_low_price(self):
        """1c contract should have meaningful edge."""
        edge = longshot_edge(1)
        assert edge > 0.001
        assert edge < 0.10  # additive, not multiplicative

    def test_returns_additive_edge(self):
        """Edge should be implied_prob * overpricing_ratio, NOT raw ratio."""
        edge_1c = longshot_edge(1)
        # At 1c: implied = 0.01, overpricing ~0.57, additive = 0.01 * 0.57 ≈ 0.0057
        assert edge_1c < 0.02  # definitely NOT 0.57

    def test_overpricing_ratio_decreases_with_price(self):
        """Overpricing RATIO should decrease with price. Additive edge
        peaks mid-range because edge = implied_prob * ratio, and implied
        grows linearly while ratio decays exponentially."""
        # Additive edge peaks around 5-10c, so test ratio directly
        edge_10 = longshot_edge(10)
        edge_15 = longshot_edge(15)
        edge_20 = longshot_edge(20)
        # Past the peak, additive edge does decrease
        assert edge_10 > edge_15 > edge_20

    def test_category_affects_edge(self):
        """Sports should have higher edge than weather for same price."""
        edge_sports = longshot_edge(5, ticker="KXNBA-SOMETHING")
        edge_weather = longshot_edge(5, ticker="KXHIGHMIA-SOMETHING")
        assert edge_sports > edge_weather

    def test_time_decay(self):
        """Edge should decay as market approaches close."""
        edge_24h = longshot_edge(5, hours_to_close=24)
        edge_1h = longshot_edge(5, hours_to_close=1)
        assert edge_24h > edge_1h

    def test_time_decay_floor(self):
        """At 1 hour, time_factor = 0.5 + 0.5*(1/24) ≈ 0.52, not zero."""
        edge = longshot_edge(5, hours_to_close=1)
        assert edge > 0

    def test_20c_has_tiny_edge(self):
        """At 20c, edge should be very small (past the peak)."""
        edge = longshot_edge(20)
        assert edge < 0.01


class TestClassifyTickerCategory:

    def test_sports_tickers(self):
        assert classify_ticker_category("KXNBA-SOMETHING") == "sports"
        assert classify_ticker_category("KXNFL-GAME") == "sports"
        assert classify_ticker_category("KXMARMAD-MARCH") == "sports"

    def test_entertainment_tickers(self):
        assert classify_ticker_category("KXOSCARS-BEST") == "entertainment"
        assert classify_ticker_category("KXALBUM-SALES") == "entertainment"
        assert classify_ticker_category("KXBOXOFFICE-WK") == "entertainment"

    def test_politics_tickers(self):
        assert classify_ticker_category("KXPRES-2024") == "politics"
        assert classify_ticker_category("KXSEN-GA") == "politics"

    def test_weather_tickers(self):
        assert classify_ticker_category("KXHIGHMIA-26FEB16-T86") == "weather"

    def test_economics_tickers(self):
        assert classify_ticker_category("KXCPI-JAN") == "economics"
        assert classify_ticker_category("KXFED-RATE") == "economics"

    def test_crypto_tickers(self):
        assert classify_ticker_category("KXBTC-100K") == "crypto"

    def test_unknown_returns_default(self):
        assert classify_ticker_category("KXRANDOM-THING") == "default"


# ===================================================================
# compute_limit_price tests
# ===================================================================

class TestComputeLimitPrice:

    def test_yes_with_spread(self):
        """Should place inside the spread, not at full ask."""
        price = compute_limit_price(40, 50, "yes")
        assert 40 < price <= 50

    def test_yes_at_midpoint_plus_one(self):
        """Midpoint of 40-50 is 45, plus 1 = 46."""
        price = compute_limit_price(40, 50, "yes")
        assert price == 46

    def test_yes_no_bid(self):
        """Without bid, falls back to ask."""
        price = compute_limit_price(0, 50, "yes")
        assert price == 50

    def test_no_with_spread(self):
        """For NO side, should compute from NO bid/ask derived from YES prices."""
        price = compute_limit_price(40, 50, "no")
        # NO bid = 100 - 50 = 50, NO ask = 100 - 40 = 60
        # Midpoint = 55, + 1 = 56
        assert price == 56

    def test_no_no_bid(self):
        """Without YES bid, NO ask = 100 - 0 = 0, should return 0 or fallback."""
        price = compute_limit_price(0, 50, "no")
        # no_bid = 100-50=50, no_ask = 100-0 = can't derive well
        # Actually: no_bid = 100-50=50, no_ask = 100-0 = 100? no, yes_bid=0 means no_ask=0
        # Let's just check it doesn't crash
        assert isinstance(price, int)

    def test_tight_spread(self):
        """With 1c spread, should return the ask (can't improve)."""
        price = compute_limit_price(49, 50, "yes")
        assert price == 50  # midpoint=49, +1 = 50 = ask


# ===================================================================
# Continuous NWS sigma model tests
# ===================================================================

class TestContinuousNwsSigma:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_sigma_decreases_with_hour(self):
        """Later in the day should give more extreme probabilities (lower sigma)."""
        # Running high 5F above threshold
        prob_10am = nws_probability(91.0, 86.0, "T", 10)
        prob_3pm = nws_probability(91.0, 86.0, "T", 15)
        prob_5pm = nws_probability(91.0, 86.0, "T", 17)
        assert prob_5pm > prob_3pm > prob_10am

    def test_no_discontinuous_jump(self):
        """Adjacent hours should not have massive probability jumps."""
        probs = [nws_probability(88.0, 86.0, "T", h) for h in range(8, 20)]
        for i in range(len(probs) - 1):
            # No jump should exceed 15% between adjacent hours
            assert abs(probs[i + 1] - probs[i]) < 0.15

    def test_early_morning_high_uncertainty(self):
        """Before 6AM, sigma=5.0 means even large margins aren't certain."""
        prob = nws_probability(92.0, 86.0, "T", 4)
        assert prob < 0.95  # not as certain as late afternoon

    def test_evening_nearly_locked(self):
        """At 8PM, sigma ~0.5 (floor), running high 3F above should be near-certain."""
        prob = nws_probability(89.0, 86.0, "T", 20)
        assert prob > 0.99


# ===================================================================
# Risk-adjusted daily loss tracking tests
# ===================================================================

def _make_manager(tmp_path, config=None, **kwargs):
    """Helper to create a TradeManager with a mock client."""
    mock_client = MagicMock()
    mock_client.post.return_value = {
        "order": {"order_id": "test-123", "status": "resting"}
    }
    trades_path = tmp_path / "trades.json"
    cfg = config or {"maxTradeAmount": 5, "maxDailyTrades": 100, "maxDailyLoss": 1}
    kill_path = tmp_path / "HALT"
    mgr = TradeManager(
        mock_client, trades_path, cfg,
        kill_switch_path=kill_path,
        **kwargs
    )
    return mgr, mock_client


class TestRiskAdjustedDailyLoss:

    def test_no_side_tracks_risk_not_cost(self, tmp_path):
        """Buying NO at 95c should track 5c risk, not 95c cost."""
        # maxDailyLoss=$1 = 100c
        mgr, _ = _make_manager(tmp_path)
        # Buy NO at 95c: risk = 100-95 = 5c per contract
        mgr.place_order("T1", "no", 95, 1, "r1")
        # Daily spend should be 5c (risk), not 95c (cost)
        assert mgr._daily_spend_cents == 5

    def test_yes_side_tracks_cost_as_risk(self, tmp_path):
        """Buying YES at 30c should track 30c risk (same as cost)."""
        mgr, _ = _make_manager(tmp_path)
        mgr.place_order("T1", "yes", 30, 1, "r1")
        assert mgr._daily_spend_cents == 30

    def test_no_side_allows_more_trades(self, tmp_path):
        """With risk-adjusted tracking, high-priced NO trades use less budget."""
        # $1 daily loss limit = 100c
        mgr, _ = _make_manager(tmp_path)
        # Each NO at 95c has only 5c risk, so we can do 20 trades before hitting 100c
        for i in range(19):
            result = mgr.place_order(f"T{i}", "no", 95, 1, f"r{i}")
            assert result is not None
        # 19 * 5c = 95c < 100c limit. 20th would be 100c = limit, should still work
        result = mgr.place_order("T19", "no", 95, 1, "r19")
        assert result is not None
        # 21st should be blocked: 105c > 100c
        result = mgr.place_order("T20", "no", 95, 1, "r20")
        assert result is None


# ===================================================================
# PortfolioAllocator tests
# ===================================================================

class TestPortfolioAllocator:

    def _make_allocator(self, balance=10000):
        client = MagicMock()
        client.get_balance.return_value = (balance, balance)
        return PortfolioAllocator(client=client)

    def test_basic_approval(self):
        alloc = self._make_allocator()
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents > 0
        assert budget.bankroll_cents == 10000

    def test_global_dedup(self):
        """Same ticker should be blocked for second bot."""
        alloc = self._make_allocator()
        alloc.record_trade("weather", "TICK-1", 100)
        budget = alloc.request_budget("entertainment", "TICK-1", edge=0.10)
        assert not budget.approved
        assert "already traded" in budget.reason

    def test_portfolio_daily_limit(self):
        """Total risk across all bots should be bounded."""
        alloc = self._make_allocator(balance=1000)
        # Portfolio limit = 25% of 1000 = 250c
        # source-monitor has priority=1.0, so per-bot limit = 1000*0.40*1.0 = 400c
        alloc.record_trade("source-monitor", "T1", 200)
        # 200c used portfolio-wide, 50c remaining in portfolio (250-200)
        budget = alloc.request_budget("source-monitor", "T2", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents <= 50

    def test_portfolio_limit_exhausted(self):
        alloc = self._make_allocator(balance=1000)
        # Use source-monitor (high priority) so per-bot limit isn't the blocker
        alloc.record_trade("source-monitor", "T1", 250)  # exhausts 25% portfolio limit
        budget = alloc.request_budget("source-monitor", "T2", edge=0.10)
        assert not budget.approved

    def test_high_confidence_gets_larger_budget(self):
        """High-confidence info-arb should get more capital."""
        alloc = self._make_allocator(balance=10000)
        normal = alloc.request_budget("entertainment", "T1", edge=0.10, confidence=0.70)
        high = alloc.request_budget("entertainment", "T2", edge=0.20, confidence=0.95)
        assert high.approved
        assert normal.approved
        assert high.max_cost_cents > normal.max_cost_cents

    def test_per_bot_limit(self):
        """Each bot has a fraction of the bankroll."""
        alloc = self._make_allocator(balance=10000)
        # Strategy bot priority = 0.3, max fraction = 0.40
        # Max = 10000 * 0.40 * 0.3 = 1200c
        alloc.record_trade("strategy", "T1", 1200)
        budget = alloc.request_budget("strategy", "T2", edge=0.10)
        assert not budget.approved

    def test_daily_reset(self):
        """Allocator should reset on new day."""
        alloc = self._make_allocator()
        alloc.record_trade("weather", "T1", 100)
        assert alloc.is_ticker_traded("T1")
        # Simulate day change
        alloc._daily_date = "1999-01-01"
        assert not alloc.is_ticker_traded("T1")

    def test_no_balance_denied(self):
        alloc = self._make_allocator(balance=0)
        budget = alloc.request_budget("weather", "T1", edge=0.10)
        assert not budget.approved

    def test_status_report(self):
        alloc = self._make_allocator(balance=5000)
        alloc.record_trade("weather", "T1", 100)
        status = alloc.get_status()
        assert status["bankroll_cents"] == 5000
        assert status["available_cents"] == 5000
        assert status["total_risk_today_cents"] == 100
        assert status["tickers_traded_today"] == 1
        assert "weather" in status["bot_spend"]


# ===================================================================
# Kelly + longshot_edge integration tests
# ===================================================================

class TestKellyLongshotIntegration:

    def test_longshot_edge_produces_valid_kelly_input(self):
        """longshot_edge() should produce an edge that half_kelly_sell accepts."""
        edge = longshot_edge(5, ticker="KXNBA-GAME")
        contracts, risk = half_kelly_sell(edge, 5, 500, bankroll_cents=10000)
        # Edge should be small enough that Kelly gives a reasonable answer
        assert contracts >= 0
        assert risk >= 0

    def test_old_vs_new_becker_sizing(self):
        """The old 0.57*exp(-0.15*p) edge fed directly into Kelly gave
        enormous sizes because it was treated as additive probability.
        The new longshot_edge() returns much smaller additive edges."""
        old_edge = 0.57 * math.exp(-0.15 * 5)  # old: ~0.27
        new_edge = longshot_edge(5)               # new: much smaller

        # Old edge is the overpricing RATIO, new edge is ADDITIVE probability
        assert new_edge < old_edge / 5  # at least 5x smaller

        # Use large max_cost so Kelly (not cap) is the binding constraint
        old_c, _ = half_kelly_sell(old_edge, 5, 50000, bankroll_cents=50000)
        new_c, _ = half_kelly_sell(new_edge, 5, 50000, bankroll_cents=50000)
        # Old method was dramatically oversized
        assert old_c > new_c

    def test_kelly_respects_zero(self):
        """When Kelly says 0, bots should NOT force count=1."""
        # Tiny edge on expensive contract
        edge = 0.001
        contracts, risk = half_kelly(edge, 50, 500, bankroll_cents=1000)
        # Kelly should return 0 for such a small edge
        assert contracts == 0


# ===================================================================
# Quarter-Kelly tests (Rec 3+10: brackets)
# ===================================================================

class TestQuarterKelly:

    def test_returns_half_of_half_kelly(self):
        """Quarter-Kelly should give roughly half the contracts of half-Kelly."""
        edge = 0.15
        hk_contracts, _ = half_kelly(edge, 20, 5000, bankroll_cents=10000)
        qk_contracts, _ = quarter_kelly(edge, 20, 5000, bankroll_cents=10000)
        assert qk_contracts <= hk_contracts // 2

    def test_hard_cap_exposure(self):
        """Bracket positions should be capped at max_exposure_cents."""
        edge = 0.30  # large edge to get many contracts
        contracts, risk = quarter_kelly(edge, 10, 50000, bankroll_cents=100000, max_exposure_cents=500)
        # Total cost = contracts * 10, should not exceed 500
        assert contracts * 10 <= 500

    def test_zero_edge_returns_zero(self):
        contracts, risk = quarter_kelly(0, 20, 500, bankroll_cents=1000)
        assert contracts == 0
        assert risk == 0

    def test_small_edge_may_round_to_zero(self):
        """Very small edge that gives 1 contract in half-Kelly should give 0 in quarter."""
        edge = 0.02
        hk, _ = half_kelly(edge, 50, 5000, bankroll_cents=5000)
        qk, _ = quarter_kelly(edge, 50, 5000, bankroll_cents=5000)
        # If half-Kelly gives 1 contract, quarter rounds to 0
        if hk <= 1:
            assert qk == 0

    def test_default_cap_scales_with_bankroll(self):
        """Default max_exposure_cents should scale: max($5, 5% of bankroll)."""
        # Small bankroll: cap = max(500, 10000*0.05) = 500
        edge = 0.25
        contracts, risk = quarter_kelly(edge, 5, 50000, bankroll_cents=10000)
        assert risk <= 500
        # Large bankroll: cap = max(500, 50000*0.05) = 2500
        contracts2, risk2 = quarter_kelly(edge, 5, 50000, bankroll_cents=50000)
        assert risk2 <= 2500


# ===================================================================
# High-conviction Kelly tests (Rec 4: threshold-NO)
# ===================================================================

class TestHighConvictionKelly:

    def test_scales_above_half_kelly(self):
        """60% Kelly should give more contracts than 50% Kelly (half-Kelly)."""
        edge = 0.15
        hk_contracts, _ = half_kelly(edge, 30, 5000, bankroll_cents=10000)
        hck_contracts, _ = high_conviction_kelly(edge, 30, 5000, bankroll_cents=10000)
        assert hck_contracts >= hk_contracts

    def test_zero_edge_returns_zero(self):
        contracts, risk = high_conviction_kelly(0, 30, 500, bankroll_cents=1000)
        assert contracts == 0
        assert risk == 0

    def test_invalid_price_returns_zero(self):
        contracts, risk = high_conviction_kelly(0.15, 0, 500, bankroll_cents=1000)
        assert contracts == 0
        contracts, risk = high_conviction_kelly(0.15, 100, 500, bankroll_cents=1000)
        assert contracts == 0

    def test_respects_max_cost(self):
        """Should not exceed max_cost_cents."""
        edge = 0.20
        contracts, risk = high_conviction_kelly(edge, 20, 200, bankroll_cents=100000)
        # Max contracts from cost cap = 200 // 20 = 10, risk = 200
        assert risk <= 200

    def test_ratio_is_60_percent_large_bankroll(self):
        """60% Kelly should be 20% more than 50% Kelly for large bankrolls."""
        edge = 0.10
        # Use very large limits so Kelly fraction is the binding constraint
        # bankroll >= $500 (50000c) triggers the 60% path
        hk, _ = half_kelly(edge, 30, 999999, bankroll_cents=100000)
        hck, _ = high_conviction_kelly(edge, 30, 999999, bankroll_cents=100000)
        if hk > 0:
            ratio = hck / hk
            # 60/50 = 1.2, allow some rounding tolerance
            assert 1.1 <= ratio <= 1.3

    def test_small_bankroll_caps_at_half_kelly(self):
        """Small bankrolls (<$500) should use 50% Kelly (same as half_kelly)."""
        edge = 0.10
        hk, _ = half_kelly(edge, 30, 999999, bankroll_cents=20000)
        hck, _ = high_conviction_kelly(edge, 30, 999999, bankroll_cents=20000)
        # Both should be half-Kelly (50%), so ratio ~1.0
        if hk > 0:
            ratio = hck / hk
            assert 0.9 <= ratio <= 1.1


# ===================================================================
# Market liquidity filter tests (Rec 7)
# ===================================================================

class TestIsMarketLiquid:

    def test_liquid_market(self):
        market = {"yes_bid": 40, "yes_ask": 45, "volume": 100}
        assert is_market_liquid(market) is True

    def test_no_bid(self):
        market = {"yes_bid": 0, "yes_ask": 45, "volume": 100}
        assert is_market_liquid(market) is False

    def test_no_ask(self):
        market = {"yes_bid": 40, "yes_ask": 0, "volume": 100}
        assert is_market_liquid(market) is False

    def test_wide_spread(self):
        """Spread > 20c should be illiquid."""
        market = {"yes_bid": 20, "yes_ask": 50, "volume": 100}
        assert is_market_liquid(market) is False

    def test_low_volume(self):
        """Volume < 50 should be illiquid."""
        market = {"yes_bid": 40, "yes_ask": 45, "volume": 10}
        assert is_market_liquid(market) is False

    def test_zero_volume(self):
        market = {"yes_bid": 40, "yes_ask": 45, "volume": 0}
        assert is_market_liquid(market) is False

    def test_none_volume(self):
        market = {"yes_bid": 40, "yes_ask": 45, "volume": None}
        assert is_market_liquid(market) is False

    def test_tight_spread_high_volume(self):
        """1c spread with high volume = very liquid."""
        market = {"yes_bid": 49, "yes_ask": 50, "volume": 5000}
        assert is_market_liquid(market) is True

    def test_exactly_20c_spread(self):
        """Spread of exactly 20c should pass."""
        market = {"yes_bid": 30, "yes_ask": 50, "volume": 100}
        assert is_market_liquid(market) is True

    def test_21c_spread_fails(self):
        """Spread of 21c should fail."""
        market = {"yes_bid": 30, "yes_ask": 51, "volume": 100}
        assert is_market_liquid(market) is False


# ===================================================================
# Concentration limit tests (Rec 8: MAX_TICKER_FRACTION = 0.05)
# ===================================================================

class TestConcentrationLimit:

    def test_max_ticker_fraction_is_005(self):
        """MAX_TICKER_FRACTION should be 0.05 (reduced from 0.15)."""
        assert MAX_TICKER_FRACTION == 0.05

    def test_single_ticker_budget_capped(self):
        """Single ticker should get at most 5% of bankroll."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        # 5% of 10000 = 500c
        assert budget.max_cost_cents <= 500

    def test_prevents_houston_scenario(self):
        """At $400 bankroll, single ticker should be capped at $20 (not $60+)."""
        client = MagicMock()
        client.get_balance.return_value = (40000, 40000)
        alloc = PortfolioAllocator(client=client)
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        # 5% of 40000 = 2000c = $20
        assert budget.max_cost_cents <= 2000


# ===================================================================
# BeatRelease scanner: build_ticker_map tests
# ===================================================================

class TestBuildTickerMap:

    def test_groups_by_subtitle(self):
        """Markets with the same subtitle should be grouped together."""
        mock_client = MagicMock()
        mock_client.get_all_markets.return_value = [
            {"ticker": "KXALBUMSALES-WUT-15000", "title": "Will Charli XCX sell 15K?", "subtitle": "Charli XCX / Wuthering Heights"},
            {"ticker": "KXALBUMSALES-WUT-25000", "title": "Will Charli XCX sell 25K?", "subtitle": "Charli XCX / Wuthering Heights"},
            {"ticker": "KXALBUMSALES-CLO-60000", "title": "Will Megan sell 60K?", "subtitle": "Megan Moroney / Cloud 9"},
        ]
        result = _br_mod.build_ticker_map(mock_client)
        assert "Charli XCX / Wuthering Heights" in result
        assert len(result["Charli XCX / Wuthering Heights"]) == 2
        assert "KXALBUMSALES-WUT-15000" in result["Charli XCX / Wuthering Heights"]
        assert "KXALBUMSALES-WUT-25000" in result["Charli XCX / Wuthering Heights"]
        assert "Megan Moroney / Cloud 9" in result
        assert len(result["Megan Moroney / Cloud 9"]) == 1

    def test_falls_back_to_title_when_no_subtitle(self):
        """If subtitle is empty, use title as key."""
        mock_client = MagicMock()
        mock_client.get_all_markets.return_value = [
            {"ticker": "KXALBUMSALES-XYZ-10000", "title": "Some market title", "subtitle": ""},
        ]
        result = _br_mod.build_ticker_map(mock_client)
        assert "Some market title" in result

    def test_empty_on_api_failure(self):
        """Should return empty dict if API call raises."""
        mock_client = MagicMock()
        mock_client.get_all_markets.side_effect = Exception("API down")
        result = _br_mod.build_ticker_map(mock_client)
        assert result == {}

    def test_skips_missing_ticker(self):
        """Markets without a ticker should be skipped."""
        mock_client = MagicMock()
        mock_client.get_all_markets.return_value = [
            {"ticker": "", "title": "No ticker", "subtitle": "Artist / Album"},
            {"ticker": "KXALBUMSALES-OK-5000", "title": "Good", "subtitle": "Artist / Album"},
        ]
        result = _br_mod.build_ticker_map(mock_client)
        assert "Artist / Album" in result
        assert len(result["Artist / Album"]) == 1
        assert result["Artist / Album"][0] == "KXALBUMSALES-OK-5000"


# ===================================================================
# BeatRelease scanner: content hash tests
# ===================================================================

class TestContentHash:

    def test_same_content_same_hash(self):
        h1 = _br_mod.content_hash("Hello world")
        h2 = _br_mod.content_hash("Hello world")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _br_mod.content_hash("Hello world")
        h2 = _br_mod.content_hash("Hello world updated")
        assert h1 != h2

    def test_hash_length_is_16(self):
        h = _br_mod.content_hash("Test content")
        assert len(h) == 16

    def test_empty_string(self):
        h = _br_mod.content_hash("")
        assert len(h) == 16
        assert isinstance(h, str)


# ===================================================================
# BeatRelease scanner: state migration tests
# ===================================================================

class TestStateMigration:

    def test_migrates_seen_urls_to_seen_posts(self, tmp_path, monkeypatch):
        """Old seen_urls format should be migrated to seen_posts."""
        state_file = tmp_path / "state.json"
        old_state = {
            "seen_urls": ["https://example.com/post/1", "https://example.com/post/2"],
            "last_check": "2025-01-01T00:00:00"
        }
        state_file.write_text(json.dumps(old_state))

        monkeypatch.setattr(_br_mod, "STATE_PATH", state_file)
        result = _br_mod.load_state()

        assert "seen_posts" in result
        assert "seen_urls" not in result
        assert "https://example.com/post/1" in result["seen_posts"]
        assert "https://example.com/post/2" in result["seen_posts"]

    def test_new_format_loads_directly(self, tmp_path, monkeypatch):
        """New seen_posts format should load without migration."""
        state_file = tmp_path / "state.json"
        new_state = {
            "seen_posts": {
                "https://example.com/post/1": {"hash": "abc123", "last_processed": "2025-01-01T00:00:00"}
            },
            "last_check": "2025-01-01T00:00:00"
        }
        state_file.write_text(json.dumps(new_state))

        monkeypatch.setattr(_br_mod, "STATE_PATH", state_file)
        result = _br_mod.load_state()

        assert result["seen_posts"]["https://example.com/post/1"]["hash"] == "abc123"

    def test_fresh_state(self, tmp_path, monkeypatch):
        """No state file should return empty seen_posts."""
        state_file = tmp_path / "nonexistent.json"

        monkeypatch.setattr(_br_mod, "STATE_PATH", state_file)
        result = _br_mod.load_state()

        assert result == {"seen_posts": {}, "last_check": None}


# ===================================================================
# Morning NWS large margin test (Tier 1.2)
# ===================================================================

class TestMorningNwsEdge:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_morning_large_margin_produces_tradeable_edge(self):
        """A 9F margin at 10AM should produce >80% probability (tradeable)."""
        # Running high 9F above threshold at 10 AM
        prob = nws_probability(95.0, 86.0, "T", 10)
        assert prob > 0.80, f"Expected >80% but got {prob*100:.1f}%"

    def test_morning_small_margin_stays_uncertain(self):
        """A 1F margin at 10AM should NOT be near-certain."""
        prob = nws_probability(87.0, 86.0, "T", 10)
        assert prob < 0.80, f"Expected <80% but got {prob*100:.1f}%"


# ===================================================================
# City-level exposure tracking tests (Tier 1.3)
# ===================================================================

class TestCityExposureLimit:

    def test_extract_city_key_weather_ticker(self):
        """Should extract city+date from KXHIGH tickers."""
        key = _extract_city_key("KXHIGHHOU-26FEB16-B77")
        assert key == "HOU:26FEB16"

    def test_extract_city_key_with_threshold(self):
        key = _extract_city_key("KXHIGHMIA-15MAR25-T86")
        assert key == "MIA:15MAR25"

    def test_extract_city_key_non_weather(self):
        """Non-weather tickers should return None."""
        key = _extract_city_key("KXCPI-JAN-T3.0")
        assert key is None

    def test_extract_city_key_album(self):
        key = _extract_city_key("KXALBUMSALES-WUT-15000")
        assert key is None

    def test_max_city_fraction_is_010(self):
        assert MAX_CITY_FRACTION == 0.10

    def test_same_city_brackets_capped(self):
        """Multiple brackets on same city should be capped at city limit."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)

        # City limit = 10% of 10000 = 1000c
        # Record trades on same city, different brackets
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 500)
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B78", 400)

        # 900c used for HOU, 100c remaining
        budget = alloc.request_budget("weather", "KXHIGHHOU-26FEB16-B79", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents <= 100

    def test_city_limit_exhausted_blocks_trade(self):
        """Once city limit is reached, further same-city trades should be denied."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)

        # Exhaust city limit
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)

        budget = alloc.request_budget("weather", "KXHIGHHOU-26FEB16-B78", edge=0.10)
        assert not budget.approved
        assert "city exposure" in budget.reason

    def test_cross_city_independent(self):
        """Different cities should have independent limits."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)

        # Exhaust Houston limit
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)

        # Miami should still be available
        budget = alloc.request_budget("weather", "KXHIGHMIA-26FEB16-T86", edge=0.10)
        assert budget.approved

    def test_non_weather_passthrough(self):
        """Non-weather tickers should not be affected by city limits."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)

        # City limits shouldn't affect album/crypto/econ tickers
        budget = alloc.request_budget("entertainment", "KXALBUMSALES-WUT-15000", edge=0.10)
        assert budget.approved

    def test_city_risk_resets_daily(self):
        """City risk tracking should reset on new day."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client)

        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)
        # Simulate day change
        alloc._daily_date = "1999-01-01"

        budget = alloc.request_budget("weather", "KXHIGHHOU-26FEB16-B77", edge=0.10)
        # Should be approved after daily reset (dedup also resets)
        assert budget.approved


# ===================================================================
# sell_position tests (Tier 1.4)
# ===================================================================

class TestSellPosition:

    def test_sell_calls_api_with_sell_action(self, tmp_path):
        """sell_position should use action='sell' in the API call."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "order": {"order_id": "exit-123", "status": "resting"}
        }
        trades_path = tmp_path / "trades.json"
        kill_path = tmp_path / "HALT"
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 50, "maxDailyTrades": 100, "maxDailyLoss": 100},
            kill_switch_path=kill_path,
        )

        result = mgr.sell_position("TICK-1", "yes", 85, 5, "Take profit")
        assert result is not None

        # Verify API call
        call_args = mock_client.post.call_args
        body = call_args[1].get("body") or call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["body"]
        assert body["action"] == "sell"
        assert body["ticker"] == "TICK-1"
        assert body["side"] == "yes"
        assert body["count"] == 5

    def test_sell_respects_kill_switch(self, tmp_path):
        """sell_position should refuse if kill switch is active."""
        mock_client = MagicMock()
        trades_path = tmp_path / "trades.json"
        kill_path = tmp_path / "HALT"
        kill_path.touch()  # Activate kill switch
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 50, "maxDailyTrades": 100, "maxDailyLoss": 100},
            kill_switch_path=kill_path,
        )

        result = mgr.sell_position("TICK-1", "yes", 85, 5, "Take profit")
        assert result is None

    def test_sell_saves_trade_record(self, tmp_path):
        """sell_position should save a record with action='sell'."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "order": {"order_id": "exit-456", "status": "resting"}
        }
        trades_path = tmp_path / "trades.json"
        kill_path = tmp_path / "HALT"
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 50, "maxDailyTrades": 100, "maxDailyLoss": 100},
            kill_switch_path=kill_path,
        )

        mgr.sell_position("TICK-1", "yes", 85, 5, "Take profit")

        trades = load_trades(trades_path)
        assert len(trades) == 1
        assert trades[0]["action"] == "sell"
        assert trades[0]["ticker"] == "TICK-1"

    def test_sell_does_not_count_toward_daily_limits(self, tmp_path):
        """sell_position should not increment daily trade/loss counters."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "order": {"order_id": "exit-789", "status": "resting"}
        }
        trades_path = tmp_path / "trades.json"
        kill_path = tmp_path / "HALT"
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 5, "maxDailyTrades": 1, "maxDailyLoss": 1},
            kill_switch_path=kill_path,
        )

        # Use up the daily trade limit
        mgr.place_order("T1", "yes", 30, 1, "entry")
        assert mgr._daily_trades == 1

        # sell_position should still work (bypasses daily limits)
        result = mgr.sell_position("T1", "yes", 85, 1, "exit")
        assert result is not None


# ===================================================================
# Ensemble weather probability tests (Tier 2.1)
# ===================================================================

class TestEnsembleWeatherProbability:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_equal_forecasts_equals_single_model(self):
        """When all models agree, ensemble should equal single-model result."""
        from probability import weather_probability
        temp = 85.0
        threshold = 82.0

        single_prob = weather_probability(temp, threshold, "T", days_out=1)
        ensemble_prob = ensemble_weather_probability(
            {"gfs": temp, "ecmwf": temp, "icon": temp},
            threshold, "T", days_out=1,
        )
        assert abs(single_prob - ensemble_prob) < 0.01

    def test_divergent_forecasts_produce_weighted_average(self):
        """Divergent forecasts should produce a value between extremes."""
        prob = ensemble_weather_probability(
            {"gfs": 90.0, "ecmwf": 80.0, "icon": 85.0},
            85.0, "T", days_out=1,
        )
        # Should be somewhere between probabilities for 80F and 90F
        assert 0.2 < prob < 0.8

    def test_single_model_fallback(self):
        """With only one model, should still produce valid probability."""
        prob = ensemble_weather_probability(
            {"gfs": 85.0},
            82.0, "T", days_out=1,
        )
        assert 0.0 < prob < 1.0

    def test_empty_forecasts_returns_half(self):
        """With no valid forecasts, should return 0.5."""
        prob = ensemble_weather_probability({}, 82.0, "T", days_out=1)
        assert prob == 0.5

    def test_bracket_direction(self):
        """Ensemble should work with bracket (B) direction too."""
        prob = ensemble_weather_probability(
            {"gfs": 85.0, "ecmwf": 85.5},
            85.0, "B", days_out=1,
        )
        assert 0.0 < prob < 1.0


# ===================================================================
# Economics probability tests (Tier 2.2)
# ===================================================================

class TestEconNowcastProbability:

    def test_nowcast_above_threshold(self):
        """When nowcast >> threshold, probability should be high."""
        prob = econ_nowcast_probability(3.5, 0.05, 3.0, "above")
        assert prob > 0.95

    def test_nowcast_below_threshold(self):
        """When nowcast << threshold, P(above) should be low."""
        prob = econ_nowcast_probability(2.5, 0.05, 3.0, "above")
        assert prob < 0.05

    def test_nowcast_at_threshold(self):
        """When nowcast == threshold, probability should be ~50%."""
        prob = econ_nowcast_probability(3.0, 0.05, 3.0, "above")
        assert 0.45 < prob < 0.55

    def test_below_direction(self):
        """Direction='below' should invert the probability."""
        above = econ_nowcast_probability(3.5, 0.05, 3.0, "above")
        below = econ_nowcast_probability(3.5, 0.05, 3.0, "below")
        assert abs(above + below - 1.0) < 0.001

    def test_zero_sigma(self):
        """Zero sigma should return 0 or 1 based on comparison."""
        assert econ_nowcast_probability(3.5, 0, 3.0, "above") == 1.0
        assert econ_nowcast_probability(2.5, 0, 3.0, "above") == 0.0

    def test_larger_sigma_more_uncertain(self):
        """Larger sigma should move probability closer to 50%."""
        tight = econ_nowcast_probability(3.2, 0.02, 3.0, "above")
        loose = econ_nowcast_probability(3.2, 0.10, 3.0, "above")
        # With tight sigma, more certain it's above
        assert tight > loose


class TestCpiNowcastSigma:

    def test_release_day(self):
        assert cpi_nowcast_sigma(0) == 0.01

    def test_one_day_out(self):
        """Smooth exponential: day 1 should be around 0.03."""
        sigma = cpi_nowcast_sigma(1)
        assert 0.015 < sigma < 0.05

    def test_one_week_out(self):
        """Smooth exponential: day 7 should be around 0.06."""
        sigma = cpi_nowcast_sigma(7)
        assert 0.04 < sigma < 0.08

    def test_two_weeks_out(self):
        """Smooth exponential: day 14 should be around 0.10."""
        sigma = cpi_nowcast_sigma(14)
        assert 0.08 < sigma < 0.12

    def test_sigma_decreases_toward_release(self):
        """Sigma should decrease monotonically as release approaches."""
        assert cpi_nowcast_sigma(14) > cpi_nowcast_sigma(7) > cpi_nowcast_sigma(1) > cpi_nowcast_sigma(0)

    def test_no_discontinuous_cliff(self):
        """Adjacent days should not have massive sigma jumps."""
        for d in range(1, 14):
            ratio = cpi_nowcast_sigma(d) / cpi_nowcast_sigma(d + 1)
            # No single step should change by more than 30%
            assert 0.7 < ratio < 1.3, f"Cliff at day {d}: {cpi_nowcast_sigma(d):.4f} -> {cpi_nowcast_sigma(d+1):.4f}"


# ===================================================================
# Crypto probability tests (Tier 2.3)
# ===================================================================

class TestCryptoPriceProbability:

    def test_price_well_above_threshold(self):
        """Current price well above threshold should give high P(above)."""
        prob = crypto_price_probability(70000, 60000, "above",
                                         time_horizon_minutes=1440,
                                         realized_vol_pct=0.60)
        assert prob > 0.80

    def test_price_well_below_threshold(self):
        """Current price well below threshold should give low P(above)."""
        prob = crypto_price_probability(50000, 70000, "above",
                                         time_horizon_minutes=1440,
                                         realized_vol_pct=0.60)
        assert prob < 0.20

    def test_price_at_threshold(self):
        """At the threshold, probability should be close to 50%."""
        prob = crypto_price_probability(65000, 65000, "above",
                                         time_horizon_minutes=1440,
                                         realized_vol_pct=0.60)
        # Due to drift term (-0.5*sigma^2*T), slightly below 50%
        assert 0.35 < prob < 0.55

    def test_below_direction(self):
        """Direction='below' should complement 'above'."""
        above = crypto_price_probability(65000, 60000, "above",
                                          time_horizon_minutes=1440,
                                          realized_vol_pct=0.60)
        below = crypto_price_probability(65000, 60000, "below",
                                          time_horizon_minutes=1440,
                                          realized_vol_pct=0.60)
        assert abs(above + below - 1.0) < 0.001

    def test_shorter_horizon_more_certain(self):
        """Closer to settlement, probability should be more extreme."""
        short = crypto_price_probability(70000, 60000, "above",
                                          time_horizon_minutes=15,
                                          realized_vol_pct=0.60)
        long = crypto_price_probability(70000, 60000, "above",
                                          time_horizon_minutes=1440,
                                          realized_vol_pct=0.60)
        assert short > long  # More certain when close to settlement

    def test_higher_vol_more_uncertain(self):
        """Higher volatility should push probability toward 50%."""
        low_vol = crypto_price_probability(70000, 60000, "above",
                                            time_horizon_minutes=1440,
                                            realized_vol_pct=0.30)
        high_vol = crypto_price_probability(70000, 60000, "above",
                                             time_horizon_minutes=1440,
                                             realized_vol_pct=1.00)
        assert low_vol > high_vol  # Less certain with high vol

    def test_iv_takes_precedence(self):
        """IV should override realized vol when both provided."""
        with_rv = crypto_price_probability(70000, 60000, "above",
                                            time_horizon_minutes=1440,
                                            realized_vol_pct=0.30, iv_pct=None)
        with_iv = crypto_price_probability(70000, 60000, "above",
                                            time_horizon_minutes=1440,
                                            realized_vol_pct=0.30, iv_pct=1.00)
        # High IV should make it less certain
        assert with_rv > with_iv

    def test_zero_time_horizon(self):
        """At T=0, probability should be 0 or 1 based on price vs threshold."""
        prob = crypto_price_probability(70000, 60000, "above",
                                         time_horizon_minutes=0)
        assert prob == 1.0

        prob = crypto_price_probability(50000, 60000, "above",
                                         time_horizon_minutes=0)
        assert prob == 0.0

    def test_invalid_prices(self):
        """Invalid prices should return 0.5."""
        assert crypto_price_probability(0, 60000, "above") == 0.5
        assert crypto_price_probability(70000, 0, "above") == 0.5


# ===================================================================
# Kalshi fee helpers tests (Tier 2.3)
# ===================================================================

from probability import kalshi_fee_cents, edge_after_fees, KALSHI_FEE_RATE


class TestKalshiFeeHelpers:

    def test_fee_at_50c(self):
        """At 50c, fee = 0.07 * 0.5 * 0.5 * 100 = 1.75c."""
        fee = kalshi_fee_cents(50)
        assert abs(fee - 1.75) < 0.01

    def test_fee_at_5c(self):
        """At 5c, fee = 0.07 * 0.05 * 0.95 * 100 = 0.3325c."""
        fee = kalshi_fee_cents(5)
        assert abs(fee - 0.3325) < 0.01

    def test_fee_at_95c(self):
        """Fee is symmetric: f(5) == f(95)."""
        assert abs(kalshi_fee_cents(5) - kalshi_fee_cents(95)) < 0.001

    def test_fee_at_1c(self):
        """At 1c, fee = 0.07 * 0.01 * 0.99 * 100 = 0.0693c."""
        fee = kalshi_fee_cents(1)
        assert abs(fee - 0.0693) < 0.001

    def test_edge_after_fees_positive(self):
        """Net edge after fees should be less than raw edge."""
        raw_edge = 0.10
        net = edge_after_fees(raw_edge, 50)
        assert net < raw_edge
        assert net > 0

    def test_edge_after_fees_can_go_negative(self):
        """Very small edge can go negative after fees."""
        net = edge_after_fees(0.005, 50)
        assert net < 0

    def test_fee_rate_constant(self):
        assert KALSHI_FEE_RATE == 0.07


# ===================================================================
# Kelly bankroll uses total balance (Tier 1.1)
# ===================================================================

class TestKellyBankrollUsesTotalBalance:

    def test_bankroll_cents_equals_total_not_available(self):
        """BudgetResponse.bankroll_cents should use total balance for Kelly sizing."""
        client = MagicMock()
        # Total = 20000, Available = 8000 (positions lock 12000)
        client.get_balance.return_value = (20000, 8000)
        alloc = PortfolioAllocator(client=client)
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        # Kelly bankroll should be total equity (20000), not available (8000)
        assert budget.bankroll_cents == 20000

    def test_risk_limits_use_available(self):
        """Risk limits (portfolio daily loss etc) should use available balance."""
        client = MagicMock()
        # Total = 50000, Available = 5000
        client.get_balance.return_value = (50000, 5000)
        alloc = PortfolioAllocator(client=client)
        # Portfolio limit = 25% of available (5000) = 1250
        alloc.record_trade("source-monitor", "T1", 1250)
        budget = alloc.request_budget("source-monitor", "T2", edge=0.10)
        assert not budget.approved
        assert "portfolio daily loss" in budget.reason


# ===================================================================
# High-confidence city limit bypass fix (Tier 1.6)
# ===================================================================

class TestHighConfCityLimitFix:

    def test_high_conf_respects_city_limit(self):
        """High-confidence override should NOT bypass city exposure limit."""
        client = MagicMock()
        client.get_balance.return_value = (100000, 100000)
        alloc = PortfolioAllocator(client=client)

        # City limit = 10% of 100000 = 10000c
        alloc.record_trade("source-monitor", "KXHIGHHOU-26FEB16-B77", 10000)

        # High-confidence trade on same city should be denied
        budget = alloc.request_budget(
            "source-monitor", "KXHIGHHOU-26FEB16-B78",
            edge=0.20, confidence=0.95,
        )
        assert not budget.approved
        assert "city exposure" in budget.reason

    def test_high_conf_different_city_allowed(self):
        """High-confidence should work on a different city."""
        client = MagicMock()
        client.get_balance.return_value = (100000, 100000)
        alloc = PortfolioAllocator(client=client)

        alloc.record_trade("source-monitor", "KXHIGHHOU-26FEB16-B77", 10000)

        budget = alloc.request_budget(
            "source-monitor", "KXHIGHMIA-26FEB16-T86",
            edge=0.20, confidence=0.95,
        )
        assert budget.approved


# ===================================================================
# NWS sigma floor tests (Tier 1.4)
# ===================================================================

class TestNwsSigmaFloor:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_sigma_floor_is_05(self):
        """NWS sigma should floor at 0.5, not 0.3."""
        # At hour 22, exp(-0.18*(22-6)) = exp(-2.88) ≈ 0.056
        # 4.0 * 0.056 ≈ 0.22 < 0.5, so floor should kick in
        prob_exact = nws_probability(86.0, 86.0, "T", 22)
        # At sigma=0.5, P(X > 86 | running_high=86) = 0.5
        assert abs(prob_exact - 0.5) < 0.05

    def test_marginal_call_not_overcertain(self):
        """Running high = threshold + 0.3 at hour 21 should NOT be 99%+ certain."""
        # With old floor (0.3), this would be very high confidence
        # With new floor (0.5), it should be more conservative
        prob = nws_probability(86.3, 86.0, "T", 21)
        assert prob < 0.90  # not overcertain for a 0.3F margin


# ===================================================================
# Horizon-dependent ensemble weights tests (Tier 2.2)
# ===================================================================

class TestHorizonDependentEnsemble:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_day0_gfs_weighted_more(self):
        """At day 0, GFS-favored forecast should dominate."""
        # GFS says 90, ECMWF says 80 — at day 0, GFS weight = 0.50
        prob_d0 = ensemble_weather_probability(
            {"gfs": 90.0, "ecmwf": 80.0}, 85.0, "T", days_out=0
        )
        prob_d5 = ensemble_weather_probability(
            {"gfs": 90.0, "ecmwf": 80.0}, 85.0, "T", days_out=5
        )
        # At day 0, GFS (90F, above) has more weight -> higher prob
        # At day 5, ECMWF (80F, below) has more weight -> lower prob
        assert prob_d0 > prob_d5

    def test_day7_ecmwf_weighted_more(self):
        """At day 7, ECMWF should have highest weight."""
        # ECMWF says 90, GFS says 80 — at day 7, ECMWF weight = 0.45
        prob_d7 = ensemble_weather_probability(
            {"gfs": 80.0, "ecmwf": 90.0}, 85.0, "T", days_out=7
        )
        prob_d0 = ensemble_weather_probability(
            {"gfs": 80.0, "ecmwf": 90.0}, 85.0, "T", days_out=0
        )
        # At day 7, ECMWF (90F) weighted more -> higher prob
        assert prob_d7 > prob_d0


# ===================================================================
# Quarter-Kelly bankroll scaling tests (Tier 2.5)
# ===================================================================

class TestQuarterKellyScaling:

    def test_small_bankroll_cap_is_500(self):
        """With $50 bankroll (5000c), cap = max(500, 250) = 500c."""
        edge = 0.25
        _, risk = quarter_kelly(edge, 5, 50000, bankroll_cents=5000)
        assert risk <= 500

    def test_large_bankroll_cap_scales(self):
        """With $1000 bankroll (100000c), cap = max(500, 5000) = 5000c."""
        edge = 0.25
        _, risk = quarter_kelly(edge, 5, 50000, bankroll_cents=100000)
        assert risk <= 5000

    def test_explicit_cap_overrides(self):
        """Explicit max_exposure_cents should override default."""
        edge = 0.25
        _, risk = quarter_kelly(edge, 5, 50000, bankroll_cents=100000,
                                 max_exposure_cents=300)
        assert risk <= 300


# ===================================================================
# Fee-adjusted edge rollout tests
# ===================================================================

class TestFeeAdjustedEdgeRollout:

    def test_edge_after_fees_reduces_edge(self):
        """edge_after_fees(0.10, 50) should be less than 0.10."""
        net = edge_after_fees(0.10, 50)
        assert net < 0.10

    def test_fee_impact_on_cheap_contracts(self):
        """At 5c, fee = 0.33c, edge reduced by ~0.33%."""
        fee = kalshi_fee_cents(5)
        assert abs(fee - 0.3325) < 0.01
        raw_edge = 0.10
        net = edge_after_fees(raw_edge, 5)
        assert abs(net - (raw_edge - fee / 100)) < 0.0001

    def test_fee_impact_on_midprice(self):
        """At 50c, fee = 1.75c, edge reduced by 1.75%."""
        fee = kalshi_fee_cents(50)
        assert abs(fee - 1.75) < 0.01
        raw_edge = 0.10
        net = edge_after_fees(raw_edge, 50)
        assert abs(net - (raw_edge - 0.0175)) < 0.001

    def test_zero_and_boundary_prices(self):
        """edge_after_fees at 0c and 100c returns raw edge (fee = 0)."""
        raw_edge = 0.10
        assert edge_after_fees(raw_edge, 0) == raw_edge
        assert edge_after_fees(raw_edge, 100) == raw_edge

    def test_negative_edge_stays_negative(self):
        """Fee adjustment on already-negative edge makes it more negative."""
        net = edge_after_fees(-0.05, 50)
        assert net < -0.05
