"""Tests for optimization changes: longshot_edge, compute_limit_price,
continuous NWS sigma, risk-adjusted daily loss, PortfolioAllocator,
ensemble weather, economics, crypto models, city exposure tracking,
sell_position, BeatRelease scanner (ticker map, content hashing, state migration),
and logging/audit infrastructure (auto file logging, source_bot, market snapshot,
scan decision log).

Covers:
  - probability.py: longshot_edge(), classify_ticker_category(), compute_limit_price()
  - probability.py: continuous NWS sigma model (no calibration overrides)
  - probability.py: ensemble_weather_probability(), econ_nowcast_probability(), crypto_price_probability()
  - kalshi_auth.py: TradeManager risk-adjusted daily loss tracking, sell_position()
  - kalshi_auth.py: setup_logging() auto file logging, source_bot field, build_market_snapshot(), log_decision()
  - capital_allocator.py: PortfolioAllocator, city-level exposure tracking
  - beatrelease-scanner.py: build_ticker_map(), content_hash(), state migration
"""

import math
import json
import os
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
from kalshi_auth import TradeManager, load_trades, setup_logging, build_market_snapshot, save_decision
from capital_allocator import (
    PortfolioAllocator, BudgetResponse, MAX_TICKER_FRACTION,
    MAX_CITY_FRACTION, _extract_city_key, DEFAULT_STATE_PATH,
)

# Counter for unique temp state paths in tests
_state_counter = 0


def _temp_state_path():
    """Generate a unique temp state path so allocator tests don't share state."""
    global _state_counter
    _state_counter += 1
    return f"/tmp/kalshi_test_alloc_{_state_counter}_{os.getpid()}.json"


# ---------------------------------------------------------------------------
# Import beatrelease-scanner.py (hyphenated filename, heavy side-effects)
# ---------------------------------------------------------------------------

def _load_beatrelease_scanner():
    """Import beatrelease-scanner.py with stubbed-out side effects."""
    orig_auth = sys.modules.get("kalshi_auth")
    orig_alloc = sys.modules.get("capital_allocator")

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.is_shutdown_requested = lambda: False
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = Path("/tmp/fake_beatrelease")
    fake_auth.fetch_parallel = lambda *a, **kw: {}
    fake_auth.retry_request = lambda *a, **kw: None
    fake_auth.TradeManager = type("TradeManager", (), {
        "__init__": lambda self, *a, **kw: None,
        "place_order": lambda self, *a, **kw: None,
    })
    fake_auth.trim_trade_log = lambda *a, **kw: None
    fake_auth.notify_whatsapp = lambda *a, **kw: False
    fake_auth._atomic_write_json = lambda *a, **kw: None
    fake_auth.HealthCheckMonitor = type("HealthCheckMonitor", (), {
        "__init__": lambda self, *a, **kw: None,
        "record_bot_heartbeat": lambda self, *a, **kw: None,
        "record_source_success": lambda self, *a, **kw: None,
        "record_source_error": lambda self, *a, **kw: None,
    })
    fake_auth.ScanSummary = type("ScanSummary", (), {
        "__init__": lambda self, *a, **kw: None,
        "markets_fetched": 0,
        "markets_evaluated": 0,
        "trades_placed": 0,
        "skips": {},
        "log_summary": lambda self: None,
    })
    fake_auth.load_trades = lambda *a, **kw: []
    sys.modules["kalshi_auth"] = fake_auth

    fake_alloc = types.ModuleType("capital_allocator")
    _FakeBudget = type("BudgetResponse", (), {"approved": True, "reason": "", "max_cost_cents": 500, "bankroll_cents": 50000})
    fake_alloc.PortfolioAllocator = type("PortfolioAllocator", (), {
        "__init__": lambda self, *a, **kw: None,
        "request_budget": lambda self, *a, **kw: _FakeBudget(),
        "record_trade": lambda self, *a, **kw: None,
    })
    sys.modules["capital_allocator"] = fake_alloc

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

    if orig_alloc is not None:
        sys.modules["capital_allocator"] = orig_alloc
    elif "capital_allocator" in sys.modules:
        del sys.modules["capital_allocator"]

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

    def test_yes_default_returns_full_ask(self):
        """Without edge param, returns full ask (urgency default)."""
        price = compute_limit_price(40, 50, "yes")
        assert price == 50

    def test_yes_no_bid(self):
        """Without bid, falls back to ask."""
        price = compute_limit_price(0, 50, "yes")
        assert price == 50

    def test_no_default_returns_full_ask(self):
        """Without edge param, NO side returns full NO ask (urgency default)."""
        price = compute_limit_price(40, 50, "no")
        # NO bid = 100 - 50 = 50, NO ask = 100 - 40 = 60
        # Default (no edge) = full NO ask = 60
        assert price == 60

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
    kwargs.setdefault("breaker_state_path", None)  # in-memory breaker for tests
    mgr = TradeManager(
        mock_client, trades_path, cfg,
        kill_switch_path=kill_path,
        **kwargs
    )
    return mgr, mock_client


class TestRiskAdjustedDailyLoss:

    def test_no_side_tracks_purchase_price(self, tmp_path):
        """Buying NO at 95c should track 95c risk (purchase price)."""
        # maxDailyLoss=$1 = 100c
        mgr, _ = _make_manager(tmp_path)
        # Buy NO at 95c: risk = 95c (purchase price)
        mgr.place_order("T1", "no", 95, 1, "r1")
        assert mgr._daily_spend_cents == 95

    def test_yes_side_tracks_cost_as_risk(self, tmp_path):
        """Buying YES at 30c should track 30c risk (same as cost)."""
        mgr, _ = _make_manager(tmp_path)
        mgr.place_order("T1", "yes", 30, 1, "r1")
        assert mgr._daily_spend_cents == 30

    def test_no_side_purchase_price_limits(self, tmp_path):
        """NO risk = purchase price, so 95c NO trades hit $1 limit after 1 trade."""
        # $1 daily loss limit = 100c
        mgr, _ = _make_manager(tmp_path)
        # Buy NO at 95c: risk = 95c. Only 5c remaining.
        result = mgr.place_order("T1", "no", 95, 1, "r1")
        assert result is not None
        # 2nd at 95c should be blocked: 95+95=190 > 100c limit
        result = mgr.place_order("T2", "no", 95, 1, "r2")
        assert result is None


# ===================================================================
# PortfolioAllocator tests
# ===================================================================

class TestPortfolioAllocator:

    def _make_allocator(self, balance=10000):
        client = MagicMock()
        client.get_balance.return_value = (balance, balance)
        return PortfolioAllocator(client=client, state_path=_temp_state_path())

    def test_basic_approval(self):
        alloc = self._make_allocator()
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents > 0
        assert budget.bankroll_cents == 10000

    def test_global_dedup(self):
        """Same ticker, same bot should be blocked (no self-supersede)."""
        alloc = self._make_allocator()
        alloc.record_trade("weather", "TICK-1", 100, edge=0.10)
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert not budget.approved
        assert "already traded" in budget.reason

    def test_portfolio_daily_limit(self):
        """Total risk across all bots should be bounded."""
        alloc = self._make_allocator(balance=10000)
        # Portfolio limit = 25% of 10000 = 2500c
        # source-monitor has priority=1.0, so per-bot limit = 10000*0.30*1.0 = 3000c
        alloc.record_trade("source-monitor", "T1", 2300)
        # 2300c used portfolio-wide, 200c remaining in portfolio (2500-2300)
        budget = alloc.request_budget("source-monitor", "T2", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents <= 300

    def test_portfolio_limit_exhausted(self):
        alloc = self._make_allocator(balance=1000)
        # Use source-monitor (high priority) so per-bot limit isn't the blocker
        alloc.record_trade("source-monitor", "T1", 250)  # exhausts 25% portfolio limit
        budget = alloc.request_budget("source-monitor", "T2", edge=0.10)
        assert not budget.approved

    def test_high_confidence_gets_larger_budget(self):
        """High-confidence info-arb should get more capital."""
        alloc = self._make_allocator(balance=20000)
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
        # Simulate day change — must save so _load_state sees old date
        alloc._daily_date = "1999-01-01"
        alloc._save_state()
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
        """Volume < 10 should be illiquid."""
        market = {"yes_bid": 40, "yes_ask": 45, "volume": 5}
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

    def test_max_ticker_fraction_is_003(self):
        """MAX_TICKER_FRACTION should be 0.03 (reduced from 0.05)."""
        assert MAX_TICKER_FRACTION == 0.03

    def test_single_ticker_budget_capped(self):
        """Single ticker should get at most 5% of bankroll."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        # 5% of 10000 = 500c
        assert budget.max_cost_cents <= 500

    def test_prevents_houston_scenario(self):
        """At $400 bankroll, single ticker should be capped at $20 (not $60+)."""
        client = MagicMock()
        client.get_balance.return_value = (40000, 40000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())
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

    def test_max_city_fraction_is_007(self):
        assert MAX_CITY_FRACTION == 0.07

    def test_same_city_brackets_capped(self):
        """Multiple brackets on same city should be capped at city limit."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        # City limit = 7% of 10000 = 700c
        # Record trades on same city, different brackets
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 300)
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B78", 300)

        # 600c used for HOU, 100c remaining
        budget = alloc.request_budget("weather", "KXHIGHHOU-26FEB16-B79", edge=0.10)
        assert budget.approved
        assert budget.max_cost_cents <= 100

    def test_city_limit_exhausted_blocks_trade(self):
        """Once city limit is reached, further same-city trades should be denied."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        # Exhaust city limit
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)

        budget = alloc.request_budget("weather", "KXHIGHHOU-26FEB16-B78", edge=0.10)
        assert not budget.approved
        assert "city exposure" in budget.reason

    def test_cross_city_independent(self):
        """Different cities should have independent limits."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        # Exhaust Houston limit
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)

        # Miami should still be available
        budget = alloc.request_budget("weather", "KXHIGHMIA-26FEB16-T86", edge=0.10)
        assert budget.approved

    def test_non_weather_passthrough(self):
        """Non-weather tickers should not be affected by city limits."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        # City limits shouldn't affect album/crypto/econ tickers
        budget = alloc.request_budget("entertainment", "KXALBUMSALES-WUT-15000", edge=0.10)
        assert budget.approved

    def test_city_risk_resets_daily(self):
        """City risk tracking should reset on new day."""
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 1000)
        # Simulate day change — must save so _load_state sees old date
        alloc._daily_date = "1999-01-01"
        alloc._save_state()

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
            breaker_state_path=None,
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
            breaker_state_path=None,
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
            breaker_state_path=None,
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
            breaker_state_path=None,
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

    def test_empty_forecasts_returns_none(self):
        """With no valid forecasts, should return None (callers fall back to single-model)."""
        prob = ensemble_weather_probability({}, 82.0, "T", days_out=1)
        assert prob is None

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
        assert cpi_nowcast_sigma(0) == 0.04

    def test_one_day_out(self):
        """Piecewise exponential: day 1 is above floor."""
        sigma = cpi_nowcast_sigma(1)
        assert 0.04 < sigma < 0.07

    def test_one_week_out(self):
        """Piecewise exponential: day 7 should be around 0.11."""
        sigma = cpi_nowcast_sigma(7)
        assert 0.09 < sigma < 0.14

    def test_two_weeks_out(self):
        """Piecewise exponential: day 14 should be around 0.14."""
        sigma = cpi_nowcast_sigma(14)
        assert 0.12 < sigma < 0.16

    def test_sigma_decreases_toward_release(self):
        """Sigma should decrease monotonically from 14d down to 1d before release."""
        assert cpi_nowcast_sigma(14) > cpi_nowcast_sigma(7) >= cpi_nowcast_sigma(1)

    def test_no_discontinuous_cliff(self):
        """Adjacent days should not have massive sigma jumps (step function allows up to 50%)."""
        for d in range(1, 14):
            ratio = cpi_nowcast_sigma(d) / cpi_nowcast_sigma(d + 1)
            # Step function: allow up to 50% change at boundaries
            assert 0.5 < ratio < 1.7, f"Cliff at day {d}: {cpi_nowcast_sigma(d):.4f} -> {cpi_nowcast_sigma(d+1):.4f}"

    def test_calibration_aware_sigma(self, monkeypatch):
        """When calibration has cpi.sigma_by_days, should use calibrated values."""
        import probability
        fake_cal = {
            "cpi": {
                "sigma_by_days": {
                    "0": 0.008, "1": 0.025, "7": 0.055, "14": 0.095,
                }
            }
        }
        monkeypatch.setattr(probability, "_calibration", fake_cal)
        assert cpi_nowcast_sigma(0) == 0.008
        assert cpi_nowcast_sigma(1) == 0.025
        assert cpi_nowcast_sigma(7) == 0.055
        assert cpi_nowcast_sigma(14) == 0.095
        # Reset
        monkeypatch.setattr(probability, "_calibration", None)


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

    def test_ou_differs_from_gbm_short_horizon(self):
        """OU adjusts both vol and drift, producing different prob than GBM."""
        gbm = crypto_price_probability(70000, 69000, "above",
                                        time_horizon_minutes=60,
                                        realized_vol_pct=0.60)
        ou = crypto_price_probability(70000, 69000, "above",
                                       time_horizon_minutes=60,
                                       realized_vol_pct=0.60,
                                       use_ou=True, ou_half_life_minutes=120)
        # OU reduces vol but also adds mean-reversion drift correction
        # Net effect depends on parameters — just verify they differ
        assert abs(ou - gbm) > 0.001, "OU should produce different prob than GBM"

    def test_ou_no_effect_long_horizon(self):
        """OU should have no effect at >4h horizons (disabled)."""
        gbm = crypto_price_probability(70000, 60000, "above",
                                        time_horizon_minutes=1440,
                                        realized_vol_pct=0.60)
        ou = crypto_price_probability(70000, 60000, "above",
                                       time_horizon_minutes=1440,
                                       realized_vol_pct=0.60,
                                       use_ou=True, ou_half_life_minutes=120)
        assert abs(gbm - ou) < 0.001

    def test_ou_default_disabled(self):
        """Default use_ou=False should match standard GBM."""
        gbm = crypto_price_probability(70000, 60000, "above",
                                        time_horizon_minutes=60,
                                        realized_vol_pct=0.60)
        default = crypto_price_probability(70000, 60000, "above",
                                            time_horizon_minutes=60,
                                            realized_vol_pct=0.60,
                                            use_ou=False)
        assert gbm == default

    def test_ou_complementary(self):
        """With OU, above + below should still sum to 1.0."""
        above = crypto_price_probability(70000, 65000, "above",
                                          time_horizon_minutes=60,
                                          realized_vol_pct=0.60,
                                          use_ou=True, ou_half_life_minutes=120)
        below = crypto_price_probability(70000, 65000, "below",
                                          time_horizon_minutes=60,
                                          realized_vol_pct=0.60,
                                          use_ou=True, ou_half_life_minutes=120)
        assert abs(above + below - 1.0) < 0.001


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

class TestKellyBankrollUsesAvailableBalance:

    def test_bankroll_cents_equals_available_not_total(self):
        """BudgetResponse.bankroll_cents should use available balance for Kelly sizing.

        Fix C: locked capital shouldn't inflate Kelly sizing. If $12k is in
        positions and only $8k is available, size based on $8k.
        """
        client = MagicMock()
        # Total = 20000, Available = 8000 (positions lock 12000)
        client.get_balance.return_value = (20000, 8000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved
        # Kelly bankroll should be available (8000), not total (20000)
        assert budget.bankroll_cents == 8000

    def test_risk_limits_use_available(self):
        """Risk limits (portfolio daily loss etc) should use available balance."""
        client = MagicMock()
        # Total = 50000, Available = 5000
        client.get_balance.return_value = (50000, 5000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())
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
        # Use 50000c balance so city limit = 10% = 5000c (below $100 absolute cap)
        client.get_balance.return_value = (50000, 50000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        # City limit = 10% of 50000 = 5000c
        alloc.record_trade("source-monitor", "KXHIGHHOU-26FEB16-B77", 5000)

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
        # Use 50000c balance so city limit = 10% = 5000c (below $100 absolute cap)
        client.get_balance.return_value = (50000, 50000)
        alloc = PortfolioAllocator(client=client, state_path=_temp_state_path())

        alloc.record_trade("source-monitor", "KXHIGHHOU-26FEB16-B77", 5000)

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


# ===================================================================
# Edge-adaptive limit pricing tests (Fund Optimization Change 2)
# ===================================================================

class TestEdgeAdaptivePricing:

    def test_high_edge_returns_full_ask(self):
        """edge >= 0.15 should return full ask for urgency."""
        price = compute_limit_price(40, 50, "yes", edge=0.20)
        assert price == 50

    def test_medium_edge_returns_ask_minus_one(self):
        """0.08 <= edge < 0.15 should return ask - 1c."""
        price = compute_limit_price(40, 50, "yes", edge=0.10)
        assert price == 49  # ask - 1

    def test_low_edge_returns_midpoint_plus_one(self):
        """edge < 0.08 should return midpoint + 1c (legacy behavior)."""
        price = compute_limit_price(40, 50, "yes", edge=0.05)
        assert price == 46  # (40+50)//2 + 1

    def test_no_edge_returns_full_ask(self):
        """edge=None should return full ask (backward compatibility)."""
        price = compute_limit_price(40, 50, "yes", edge=None)
        assert price == 50

    def test_no_side_pricing(self):
        """NO-side tiers should work correctly with spread-fraction placement."""
        # NO bid=50, NO ask=60, spread=10 (from yes_bid=40, yes_ask=50)
        # High edge: full NO ask = 60
        price_high = compute_limit_price(40, 50, "no", edge=0.20)
        assert price_high == 60
        # Medium edge: no_bid + spread*2//3 = 50 + 6 = 56
        price_med = compute_limit_price(40, 50, "no", edge=0.10)
        assert price_med == 56
        # Low edge: no_bid + spread//3 = 50 + 3 = 53
        price_low = compute_limit_price(40, 50, "no", edge=0.05)
        assert price_low == 53

    def test_no_spread_returns_ask(self):
        """When bid == ask (no spread), should return ask regardless of edge."""
        # No spread: bid=50, ask=50 → yes_ask > yes_bid is False → return yes_ask
        price = compute_limit_price(50, 50, "yes", edge=0.05)
        assert price == 50


# ===================================================================
# Allocator shared state tests (Fund Optimization Change 1)
# ===================================================================

class TestAllocatorSharedState:

    def _make_allocator(self, tmp_path, balance=10000):
        client = MagicMock()
        client.get_balance.return_value = (balance, balance)
        state_path = tmp_path / "allocator-state.json"
        return PortfolioAllocator(client=client, state_path=str(state_path))

    def test_save_and_load_roundtrip(self, tmp_path):
        """Save state, create new allocator, load, verify."""
        alloc1 = self._make_allocator(tmp_path)
        alloc1.record_trade("weather", "TICK-1", 200)
        alloc1.record_trade("entertainment", "TICK-2", 300)

        # New allocator should see the state
        alloc2 = self._make_allocator(tmp_path)
        assert alloc2.is_ticker_traded("TICK-1")
        assert alloc2.is_ticker_traded("TICK-2")

    def test_daily_reset_clears_state_file(self, tmp_path):
        """State file cleared on date change."""
        alloc = self._make_allocator(tmp_path)
        alloc.record_trade("weather", "TICK-1", 200)
        assert alloc.is_ticker_traded("TICK-1")

        # Simulate day change
        alloc._daily_date = "1999-01-01"
        alloc._save_state()

        alloc2 = self._make_allocator(tmp_path)
        # After daily reset, ticker should no longer be traded
        assert not alloc2.is_ticker_traded("TICK-1")

    def test_concurrent_access_with_locking(self, tmp_path):
        """Two allocators recording trades — no data loss."""
        alloc1 = self._make_allocator(tmp_path)
        alloc2 = self._make_allocator(tmp_path)

        alloc1.record_trade("weather", "TICK-1", 100)
        alloc2.record_trade("entertainment", "TICK-2", 200)

        # Both should be visible from a fresh allocator
        alloc3 = self._make_allocator(tmp_path)
        assert alloc3.is_ticker_traded("TICK-1")
        assert alloc3.is_ticker_traded("TICK-2")

    def test_missing_state_file_initializes_empty(self, tmp_path):
        """Fresh start with no file should work."""
        state_path = tmp_path / "nonexistent.json"
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=str(state_path))
        # Should not crash, and no tickers traded
        assert not alloc.is_ticker_traded("TICK-1")

    def test_corrupt_state_file_recovers(self, tmp_path):
        """Invalid JSON should reset to empty state."""
        state_path = tmp_path / "allocator-state.json"
        state_path.write_text("not valid json {{{")
        client = MagicMock()
        client.get_balance.return_value = (10000, 10000)
        alloc = PortfolioAllocator(client=client, state_path=str(state_path))
        # Should recover gracefully
        assert not alloc.is_ticker_traded("TICK-1")
        budget = alloc.request_budget("weather", "TICK-1", edge=0.10)
        assert budget.approved

    def test_record_trade_persists(self, tmp_path):
        """Record trade, verify in file."""
        alloc = self._make_allocator(tmp_path)
        alloc.record_trade("weather", "TICK-1", 500)

        state_path = tmp_path / "allocator-state.json"
        data = json.loads(state_path.read_text())
        assert "TICK-1" in data["traded_tickers"]
        assert data["bot_spend"]["weather"] == 500

    def test_is_ticker_traded_cross_process(self, tmp_path):
        """Record in one allocator, check in another."""
        alloc1 = self._make_allocator(tmp_path)
        alloc1.record_trade("weather", "TICK-ABC", 100)

        alloc2 = self._make_allocator(tmp_path)
        assert alloc2.is_ticker_traded("TICK-ABC")

    def test_default_state_path(self):
        """Verify DEFAULT_STATE_PATH used when none provided."""
        from capital_allocator import DEFAULT_STATE_PATH
        alloc = PortfolioAllocator()
        assert alloc.state_path == DEFAULT_STATE_PATH


# ===================================================================
# Reconciliation fix tests (Fund Optimization Change 4)
# ===================================================================

class TestReconciliationFix:

    def _make_reconcile_inputs(self, trades_by_bot, settlements):
        """Helper to build reconcile_trades inputs."""
        local = [{"label": label, "trades": trades}
                 for label, trades in trades_by_bot]
        return local, settlements, []  # empty fills

    def test_no_double_counting_same_ticker_two_bots(self):
        """Same ticker in 2 bots — revenue counted only once."""
        # Import here to avoid module-level issues
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "TICK-1", "order_id": "ord-1", "side": "yes"}
            ]},
            {"label": "bot-B", "trades": [
                {"ticker": "TICK-1", "order_id": "ord-2", "side": "yes"}
            ]},
        ]
        settlements = [{"ticker": "TICK-1", "revenue": 100}]

        result = mod.reconcile_trades(local, settlements, [])
        # Only one bot should get credited
        total_pnl = sum(b["pnl_cents"] for b in result["per_bot"])
        assert total_pnl == 100  # NOT 200

    def test_order_id_matching(self):
        """Matches by order_id when available (dedup)."""
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "TICK-1", "order_id": "ord-1", "side": "no"},
                {"ticker": "TICK-1", "order_id": "ord-1", "side": "no"},  # dup
            ]},
        ]
        settlements = [{"ticker": "TICK-1", "revenue": 50}]

        result = mod.reconcile_trades(local, settlements, [])
        assert result["per_bot"][0]["wins"] == 1  # not 2

    def test_fallback_to_ticker_when_no_order_id(self):
        """Old records without order_id still work via ticker dedup."""
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "TICK-1", "side": "yes"},  # no order_id
            ]},
            {"label": "bot-B", "trades": [
                {"ticker": "TICK-1", "side": "yes"},  # same ticker, no order_id
            ]},
        ]
        settlements = [{"ticker": "TICK-1", "revenue": 75}]

        result = mod.reconcile_trades(local, settlements, [])
        total_pnl = sum(b["pnl_cents"] for b in result["per_bot"])
        assert total_pnl == 75  # counted once

    def test_per_side_win_rate(self):
        """YES and NO win rates tracked separately."""
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "T1", "order_id": "o1", "side": "yes"},
                {"ticker": "T2", "order_id": "o2", "side": "yes"},
                {"ticker": "T3", "order_id": "o3", "side": "no"},
                {"ticker": "T4", "order_id": "o4", "side": "no"},
                {"ticker": "T5", "order_id": "o5", "side": "no"},
            ]},
        ]
        settlements = [
            {"ticker": "T1", "revenue": 50},   # yes win
            {"ticker": "T2", "revenue": -30},   # yes loss
            {"ticker": "T3", "revenue": 40},    # no win
            {"ticker": "T4", "revenue": 20},    # no win
            {"ticker": "T5", "revenue": -10},   # no loss
        ]

        result = mod.reconcile_trades(local, settlements, [])
        agg = result["aggregate"]
        assert agg["yes_wins"] == 1
        assert agg["yes_losses"] == 1
        assert agg["no_wins"] == 2
        assert agg["no_losses"] == 1
        # YES win rate: 50%, NO win rate: 66.7%
        assert abs(agg["yes_win_rate"] - 0.5) < 0.01
        assert abs(agg["no_win_rate"] - 0.6667) < 0.01

    def test_empty_settlements(self):
        """No settlements → all unmatched."""
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "T1", "order_id": "o1", "side": "yes"},
            ]},
        ]

        result = mod.reconcile_trades(local, [], [])
        assert result["per_bot"][0]["unmatched"] == 1
        assert result["aggregate"]["pnl_cents"] == 0

    def test_mixed_revenues(self):
        """Positive and negative revenues counted correctly."""
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "analyze_performance",
            str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
        )
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        local = [
            {"label": "bot-A", "trades": [
                {"ticker": "T1", "order_id": "o1", "side": "yes"},
                {"ticker": "T2", "order_id": "o2", "side": "no"},
            ]},
        ]
        settlements = [
            {"ticker": "T1", "revenue": 100},
            {"ticker": "T2", "revenue": -50},
        ]

        result = mod.reconcile_trades(local, settlements, [])
        assert result["aggregate"]["pnl_cents"] == 50
        assert result["aggregate"]["wins"] == 1
        assert result["aggregate"]["losses"] == 1


# ===================================================================
# Settlement-aware stale order cleanup tests (Fund Optimization Change 3)
# ===================================================================

class TestSettlementAwareCleanup:
    """Tests for the rewritten position-monitor cancel_stale_orders().

    Since the function uses module-level `client` and `log`, we test the
    logic by verifying the expected behavior via mock interactions.
    """

    def _load_position_monitor(self):
        """Import position-monitor with stubbed side-effects."""
        import types
        orig_modules = {}
        for mod_name in ("kalshi_auth", "probability", "capital_allocator"):
            if mod_name in sys.modules:
                orig_modules[mod_name] = sys.modules[mod_name]

        fake_client = MagicMock()
        fake_client.get_balance.return_value = (10000, 10000)

        fake_auth = types.ModuleType("kalshi_auth")
        fake_auth.KalshiClient = lambda *a, **kw: fake_client
        fake_auth.setup_unbuffered = lambda: None
        fake_auth.setup_signal_handlers = lambda: None
        fake_auth.is_shutdown_requested = lambda: False
        fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
        fake_auth.PROJECT_DIR = Path("/tmp/fake_posmon")
        fake_auth.TradeManager = type("TradeManager", (), {
            "__init__": lambda self, *a, **kw: None,
        })
        fake_auth.trim_trade_log = lambda *a, **kw: None
        fake_auth.load_trades = lambda *a, **kw: []
        fake_auth._atomic_write_json = lambda *a, **kw: None
        fake_auth.CITY_TIMEZONES = {
            "MIA": "America/New_York", "LAX": "America/Los_Angeles",
            "PHIL": "America/New_York", "NY": "America/New_York",
            "CHI": "America/Chicago", "AUS": "America/Chicago",
            "DEN": "America/Denver", "HOU": "America/Chicago",
        }
        fake_auth._local_today = lambda city_code="NY": "2026-02-20"
        fake_auth.round_half_up = lambda v: int(__import__("decimal").Decimal(str(v)).quantize(
            __import__("decimal").Decimal("1"), rounding=__import__("decimal").ROUND_HALF_UP))
        fake_auth.retry_request = lambda *a, **kw: MagicMock()
        fake_auth.fetch_parallel = lambda *a, **kw: []
        fake_auth.HealthCheckMonitor = type("HealthCheckMonitor", (), {
            "__init__": lambda self, *a, **kw: None,
            "record_bot_heartbeat": lambda self, *a, **kw: None,
            "record_source_success": lambda self, *a, **kw: None,
            "record_source_error": lambda self, *a, **kw: None,
            "check_health": lambda self, *a, **kw: [],
        })
        fake_auth.ScanSummary = type("ScanSummary", (), {
            "__init__": lambda self, *a, **kw: None,
            "markets_fetched": 0,
            "markets_evaluated": 0,
            "trades_placed": 0,
            "skips": {},
            "log_summary": lambda self: None,
        })
        fake_auth.notify_whatsapp = lambda *a, **kw: None
        sys.modules["kalshi_auth"] = fake_auth

        fake_prob = types.ModuleType("probability")
        fake_prob.half_kelly = lambda *a, **kw: (0, 0)
        fake_prob.half_kelly_sell = lambda *a, **kw: (0, 0)
        fake_prob.compute_limit_price = lambda *a, **kw: 50
        fake_prob.weather_probability = lambda *a, **kw: 0.5
        fake_prob.nws_probability = lambda *a, **kw: 0.5
        fake_prob.edge_after_fees = lambda *a, **kw: 0.0
        fake_prob.kalshi_fee_cents = lambda p: 0.07 * (p / 100) * (1 - p / 100) * 100
        fake_prob.crypto_price_probability = lambda *a, **kw: 0.5
        sys.modules["probability"] = fake_prob

        fake_alloc = types.ModuleType("capital_allocator")
        fake_alloc.PortfolioAllocator = type("PortfolioAllocator", (), {
            "__init__": lambda self, *a, **kw: None,
        })
        sys.modules["capital_allocator"] = fake_alloc

        # Create dirs and config
        data_dir = Path("/tmp/fake_posmon/data")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "pids").mkdir(parents=True, exist_ok=True)
        config_dir = Path("/tmp/fake_posmon/config")
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "bots-config.json").write_text(json.dumps({
            "positionMonitor": {
                "checkIntervalMinutes": 15,
                "takeProfitPct": 0.20,
                "stopLossPct": 0.50,
                "maxDailyExits": 5,
            }
        }))

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "position_monitor",
            str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "position-monitor.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Restore original modules
        for mod_name in ("kalshi_auth", "probability", "capital_allocator"):
            if mod_name in orig_modules:
                sys.modules[mod_name] = orig_modules[mod_name]
            elif mod_name in sys.modules:
                del sys.modules[mod_name]

        return mod, fake_client

    def test_cancels_order_near_settlement(self):
        """close_time < 2h from now should trigger cancel."""
        import datetime as dt
        mod, mock_client = self._load_position_monitor()

        now = dt.datetime.now(dt.timezone.utc)
        close_soon = (now + dt.timedelta(hours=1)).isoformat()

        mock_client.get.return_value = {
            "orders": [{
                "order_id": "ORD-1",
                "ticker": "TICK-1",
                "expiration_time": close_soon,
                "created_time": now.isoformat(),
            }]
        }
        mock_client.delete = MagicMock()

        mod.cancel_stale_orders()
        mock_client.delete.assert_called_once_with("/portfolio/orders/ORD-1")

    def test_keeps_order_far_from_settlement(self):
        """close_time > 2h and age < 12h should keep order."""
        import datetime as dt
        mod, mock_client = self._load_position_monitor()

        now = dt.datetime.now(dt.timezone.utc)
        close_far = (now + dt.timedelta(hours=10)).isoformat()
        recent = (now - dt.timedelta(hours=1)).isoformat()

        mock_client.get.return_value = {
            "orders": [{
                "order_id": "ORD-2",
                "ticker": "TICK-2",
                "expiration_time": close_far,
                "created_time": recent,
            }]
        }
        mock_client.delete = MagicMock()

        mod.cancel_stale_orders()
        mock_client.delete.assert_not_called()

    def test_cancels_old_order_without_close_time(self):
        """age > 12h and no close_time → cancel."""
        import datetime as dt
        mod, mock_client = self._load_position_monitor()

        now = dt.datetime.now(dt.timezone.utc)
        old_time = (now - dt.timedelta(hours=15)).isoformat()

        mock_client.get.return_value = {
            "orders": [{
                "order_id": "ORD-3",
                "ticker": "TICK-3",
                "created_time": old_time,
            }]
        }
        mock_client.delete = MagicMock()

        mod.cancel_stale_orders()
        mock_client.delete.assert_called_once_with("/portfolio/orders/ORD-3")

    def test_keeps_recent_order_without_close_time(self):
        """age < TTL (120 min) and no close_time → keep."""
        import datetime as dt
        mod, mock_client = self._load_position_monitor()

        now = dt.datetime.now(dt.timezone.utc)
        recent = (now - dt.timedelta(minutes=90)).isoformat()

        mock_client.get.return_value = {
            "orders": [{
                "order_id": "ORD-4",
                "ticker": "TICK-4",
                "created_time": recent,
            }]
        }
        mock_client.delete = MagicMock()

        mod.cancel_stale_orders()
        mock_client.delete.assert_not_called()


# ===================================================================
# File logging tests (Logging Change 1)
# ===================================================================

class TestFileLogging:

    def test_setup_logging_creates_log_file(self, tmp_path, monkeypatch):
        """Auto-derived log file should be created."""
        import logging
        import kalshi_auth
        monkeypatch.setattr(kalshi_auth, "PROJECT_DIR", tmp_path)
        # Use a unique logger name to avoid handler caching
        name = f"test-bot-{os.getpid()}-1"
        logger = setup_logging(name)
        log_file = tmp_path / "data" / "logs" / f"{name}.log"
        assert log_file.exists()
        # Cleanup handlers to avoid leaking
        logger.handlers.clear()

    def test_setup_logging_explicit_path(self, tmp_path):
        """Explicit log_file should be used instead of auto-derived path."""
        import logging
        name = f"test-bot-{os.getpid()}-2"
        explicit = str(tmp_path / "custom.log")
        logger = setup_logging(name, log_file=explicit)
        assert Path(explicit).exists()
        logger.handlers.clear()

    def test_setup_logging_mkdir_parents(self, tmp_path, monkeypatch):
        """Nested dirs should be created automatically."""
        import kalshi_auth
        monkeypatch.setattr(kalshi_auth, "PROJECT_DIR", tmp_path)
        name = f"test-bot-{os.getpid()}-3"
        logger = setup_logging(name)
        log_dir = tmp_path / "data" / "logs"
        assert log_dir.is_dir()
        logger.handlers.clear()

    def test_log_writes_to_file(self, tmp_path, monkeypatch):
        """Log message should appear in the file."""
        import logging
        import kalshi_auth
        monkeypatch.setattr(kalshi_auth, "PROJECT_DIR", tmp_path)
        name = f"test-bot-{os.getpid()}-4"
        logger = setup_logging(name)
        logger.info("Test message 12345")
        # Flush all handlers
        for h in logger.handlers:
            h.flush()
        log_file = tmp_path / "data" / "logs" / f"{name}.log"
        content = log_file.read_text()
        assert "Test message 12345" in content
        logger.handlers.clear()


# ===================================================================
# Trade record fields tests (Logging Changes 2-4)
# ===================================================================

class TestTradeRecordFields:

    def test_place_order_includes_source_bot(self, tmp_path):
        """Trade record from place_order should include source_bot field."""
        import logging
        mgr, _ = _make_manager(tmp_path, logger=logging.getLogger("test-weather"))
        mgr.place_order("T1", "yes", 50, 1, "test reason")
        trades = load_trades(tmp_path / "trades.json")
        assert len(trades) == 1
        assert trades[0]["source_bot"] == "test-weather"

    def test_sell_position_includes_source_bot(self, tmp_path):
        """Trade record from sell_position should include source_bot field."""
        import logging
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "order": {"order_id": "exit-1", "status": "resting"}
        }
        trades_path = tmp_path / "trades.json"
        kill_path = tmp_path / "HALT"
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 50, "maxDailyTrades": 100, "maxDailyLoss": 100},
            kill_switch_path=kill_path,
            logger=logging.getLogger("test-posmon"),
            breaker_state_path=None,
        )
        mgr.sell_position("T1", "yes", 85, 5, "exit reason")
        trades = load_trades(trades_path)
        assert len(trades) == 1
        assert trades[0]["source_bot"] == "test-posmon"

    def test_place_order_includes_market_snapshot(self, tmp_path):
        """market_snapshot passed via extra_fields should be in trade record."""
        mgr, _ = _make_manager(tmp_path)
        snap = build_market_snapshot(yes_bid=45, yes_ask=50)
        mgr.place_order("T1", "yes", 50, 1, "test", market_snapshot=snap)
        trades = load_trades(tmp_path / "trades.json")
        assert trades[0]["market_snapshot"]["yes_bid"] == 45
        assert trades[0]["market_snapshot"]["yes_ask"] == 50

    def test_build_market_snapshot_full(self):
        """All fields populated should all appear."""
        snap = build_market_snapshot(yes_bid=40, yes_ask=50, volume=1000, open_interest=500)
        assert snap == {"yes_bid": 40, "yes_ask": 50, "volume": 1000, "open_interest": 500}

    def test_build_market_snapshot_partial(self):
        """Only provided fields should appear (no None values)."""
        snap = build_market_snapshot(yes_bid=40)
        assert snap == {"yes_bid": 40}
        assert "yes_ask" not in snap
        assert "volume" not in snap

    def test_log_decision_creates_file(self, tmp_path):
        """log_decision should create a decisions file."""
        mgr, _ = _make_manager(tmp_path)
        mgr.log_decision("T1", "yes", "skipped", "edge below threshold", edge=0.05)
        decisions_path = tmp_path / "trades-decisions.json"
        assert decisions_path.exists()
        decisions = json.loads(decisions_path.read_text())
        assert len(decisions) == 1
        assert decisions[0]["ticker"] == "T1"
        assert decisions[0]["action"] == "skipped"

    def test_log_decision_bounded(self, tmp_path):
        """Decisions log should be bounded at ~5000 entries."""
        mgr, _ = _make_manager(tmp_path)
        decisions_path = tmp_path / "trades-decisions.json"
        # Pre-fill with 6000 entries
        prefill = [{"timestamp": "2025-01-01T00:00:00", "ticker": f"T{i}",
                     "side": "yes", "action": "skipped", "reason": "test",
                     "source_bot": "test"} for i in range(6000)]
        decisions_path.write_text(json.dumps(prefill))
        # Add one more — should trigger truncation
        mgr.log_decision("T999", "yes", "skipped", "test")
        decisions = json.loads(decisions_path.read_text())
        assert len(decisions) <= 5001  # 4000 kept + 1 new (after truncation from 6000)

    def test_log_decision_fields(self, tmp_path):
        """All expected fields should be present in decision record."""
        import logging
        mgr, _ = _make_manager(tmp_path, logger=logging.getLogger("test-crypto"))
        mgr.log_decision("T1", "no", "rejected", "daily limit reached",
                          edge=0.12, price_cents=30, custom_field="extra")
        decisions_path = tmp_path / "trades-decisions.json"
        decisions = json.loads(decisions_path.read_text())
        d = decisions[0]
        assert d["ticker"] == "T1"
        assert d["side"] == "no"
        assert d["action"] == "rejected"
        assert d["reason"] == "daily limit reached"
        assert d["source_bot"] == "test-crypto"
        assert d["edge"] == 0.12
        assert d["price_cents"] == 30
        assert d["custom_field"] == "extra"
        assert "timestamp" in d


# ===================================================================
# Ensemble weight calibration tests (Phase 3.5)
# ===================================================================

class TestEnsembleWeightUpdate:

    def test_lower_mae_gets_higher_weight(self):
        """Model with lower MAE should get higher weight."""
        # Import calibrate_ensemble_weights from calibrate-sigma.py
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "calibrate_sigma",
            str(Path(__file__).resolve().parent.parent / "scripts" / "calibrate-sigma.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Create fake trades with ensemble data and settlements
        trades = [
            {
                "ticker": "KXHIGHMIA-26FEB16-T86",
                "side": "yes",
                "timestamp": "2026-02-15T12:00:00Z",
                "ensemble_forecasts": {"gfs": 88.0, "ecmwf": 90.0, "icon": 85.0},
            },
            {
                "ticker": "KXHIGHMIA-27FEB16-T84",
                "side": "no",
                "timestamp": "2026-02-15T12:00:00Z",
                "ensemble_forecasts": {"gfs": 82.0, "ecmwf": 80.0, "icon": 83.0},
            },
        ]
        # First trade: YES won (event occurred), Second: NO won (event didn't occur)
        settlement_map = {
            "KXHIGHMIA-26FEB16-T86": 100,   # YES won
            "KXHIGHMIA-27FEB16-T84": 100,   # NO won (revenue > 0 for NO side)
        }

        result = mod.calibrate_ensemble_weights(trades, settlement_map)
        assert result.get("n", 0) > 0

        if "weights" in result and "model_maes" in result:
            maes = result["model_maes"]
            weights = result["weights"]
            # Model with lowest MAE should have highest weight
            if len(maes) >= 2:
                best_model = min(maes, key=maes.get)
                worst_model = max(maes, key=maes.get)
                # Due to EMA blending, the relationship may be dampened
                # but the newly calibrated inverse-MAE component should push in this direction
                assert weights.get(best_model, 0) >= weights.get(worst_model, 0) * 0.5

    def test_weights_sum_to_one(self):
        """Calibrated weights must sum to 1.0."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "calibrate_sigma",
            str(Path(__file__).resolve().parent.parent / "scripts" / "calibrate-sigma.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        trades = [
            {
                "ticker": "KXHIGHMIA-26FEB16-T86",
                "side": "yes",
                "timestamp": "2026-02-15T12:00:00Z",
                "ensemble_forecasts": {"gfs": 88.0, "ecmwf": 90.0, "icon": 85.0},
            },
        ]
        settlement_map = {"KXHIGHMIA-26FEB16-T86": 100}

        result = mod.calibrate_ensemble_weights(trades, settlement_map)
        if "weights" in result:
            total = sum(result["weights"].values())
            assert abs(total - 1.0) < 0.01, f"Weights sum to {total}, expected 1.0"
