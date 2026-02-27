"""Tests for capital allocator signal quality scoring, supersede logic,
and pending exits.

Fix 3: Validates the "best signal wins" mechanism and backward compatibility.
"""

import datetime
import json
import time
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from capital_allocator import (
    PortfolioAllocator, compute_signal_quality, MODEL_QUALITY_FACTOR, BudgetResponse,
    CITY_REGIONS, _CITY_TO_REGION, MAX_REGION_FRACTION, _load_absolute_cap,
)


class TestSignalQuality:
    """Test compute_signal_quality()."""

    def test_source_monitor_highest(self):
        """Source monitor with 10% edge should score highest."""
        q_sm = compute_signal_quality("source-monitor", 0.10)
        q_weather = compute_signal_quality("weather", 0.10)
        assert q_sm > q_weather

    def test_quality_scales_with_edge(self):
        """Higher edge -> higher quality."""
        q1 = compute_signal_quality("weather", 0.10)
        q2 = compute_signal_quality("weather", 0.20)
        assert q2 > q1

    def test_quality_zero_edge(self):
        assert compute_signal_quality("weather", 0.0) == 0.0

    def test_unknown_bot_gets_default(self):
        q = compute_signal_quality("unknown-bot", 0.10)
        assert q == 0.10 * 0.2  # default factor is 0.2

    def test_all_known_bots_have_factors(self):
        for bot in ["source-monitor", "economics", "entertainment",
                     "weather", "crypto", "strategy", "beatrelease"]:
            assert bot in MODEL_QUALITY_FACTOR


class TestSupersedeLogic:
    """Test the 'best signal wins' dedup replacement."""

    def _make_allocator(self, balance=50000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_same_bot_same_ticker_denied(self):
        alloc = self._make_allocator()
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 100, edge=0.10)
        result = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.10)
        assert not result.approved
        assert "already traded" in result.reason

    def test_better_signal_supersedes(self):
        alloc = self._make_allocator()
        # Weather trades with 10% edge, quality = 0.10 * 0.5 = 0.05
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 100, edge=0.10)

        # Source monitor with 20% edge, quality = 0.20 * 1.0 = 0.20
        # 0.20 > 0.05 * 1.5 = 0.075 -> supersede
        result = alloc.request_budget("source-monitor", "KXHIGHNY-26FEB16-T40", edge=0.20)
        assert result.approved

    def test_worse_signal_rejected(self):
        alloc = self._make_allocator()
        # Source monitor trades first with 20% edge, quality = 0.20 * 1.0 = 0.20
        alloc.record_trade("source-monitor", "KXHIGHNY-26FEB16-T40", 100, edge=0.20)

        # Weather tries with 10% edge, quality = 0.10 * 0.5 = 0.05
        # 0.05 < 0.20 * 1.5 = 0.30 -> rejected
        result = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.10)
        assert not result.approved

    def test_supersede_creates_pending_exit(self):
        alloc = self._make_allocator()
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 100, edge=0.10)
        alloc.request_budget("source-monitor", "KXHIGHNY-26FEB16-T40", edge=0.20)
        exits = alloc.get_pending_exits()
        assert "KXHIGHNY-26FEB16-T40" in exits

    def test_get_pending_exits_clears(self):
        alloc = self._make_allocator()
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 100, edge=0.10)
        alloc.request_budget("source-monitor", "KXHIGHNY-26FEB16-T40", edge=0.20)
        exits1 = alloc.get_pending_exits()
        assert len(exits1) == 1
        exits2 = alloc.get_pending_exits()
        assert len(exits2) == 0


class TestRecordTradeEdge:
    """Test that record_trade stores signal quality."""

    def _make_allocator(self):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (50000, 50000)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_record_stores_signal_quality(self):
        alloc = self._make_allocator()
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 100, edge=0.10)
        entry = alloc._traded_tickers["KXHIGHNY-26FEB16-T40"]
        assert isinstance(entry, dict)
        assert entry["signal_quality"] == pytest.approx(0.05, abs=0.001)
        assert entry["edge"] == 0.10
        assert entry["bot"] == "weather"


class TestBankrollUsesAvailable:
    """Fix C: Kelly bankroll should use available_balance, not total_balance."""

    def _make_allocator(self, total=10000, available=2000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (total, available)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_bankroll_equals_available(self):
        """When total=10000, available=2000, bankroll should be 2000."""
        alloc = self._make_allocator(total=10000, available=2000)
        budget = alloc.request_budget("weather", "TICK-NEW", edge=0.10)
        assert budget.approved
        assert budget.bankroll_cents == 2000

    def test_bankroll_not_total(self):
        """Bankroll should NOT be total balance."""
        alloc = self._make_allocator(total=50000, available=5000)
        budget = alloc.request_budget("weather", "TICK-NEW2", edge=0.10)
        assert budget.approved
        assert budget.bankroll_cents == 5000
        assert budget.bankroll_cents != 50000


class TestBankrollStatusReporting:
    """Phase 2 Fix: get_status() should report available_balance as bankroll_cents."""

    def _make_allocator(self, total=10000, available=2000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (total, available)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_status_bankroll_is_available(self):
        """get_status() bankroll_cents should be available_balance, not total."""
        alloc = self._make_allocator(total=10000, available=2000)
        status = alloc.get_status()
        assert status["bankroll_cents"] == 2000

    def test_status_includes_total(self):
        """get_status() should include total_balance_cents for reference."""
        alloc = self._make_allocator(total=10000, available=2000)
        status = alloc.get_status()
        assert status["total_balance_cents"] == 10000


class TestAbsoluteDailyLossCap:
    """Fix D: Absolute $100 daily risk cap."""

    def _make_allocator(self, balance=200000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_cap_blocks_after_limit(self):
        """After exceeding configured cap in risk today, next request should be rejected."""
        alloc = self._make_allocator(balance=200000)
        # Record trades summing to the configured cap (15000 cents = $150)
        for i in range(15):
            alloc.record_trade("weather", f"TICK-{i}", 1000, edge=0.10)
        budget = alloc.request_budget("weather", "TICK-NEW", edge=0.15)
        assert not budget.approved
        assert "absolute daily risk cap" in budget.reason

    def test_under_cap_allowed(self):
        """Under configured cap should still allow trading."""
        alloc = self._make_allocator(balance=200000)
        # Record $140 of risk (14000 cents, under $150 cap)
        for i in range(14):
            alloc.record_trade("weather", f"TICK-{i}", 1000, edge=0.10)
        budget = alloc.request_budget("weather", "TICK-NEW", edge=0.15)
        assert budget.approved


class TestBackwardCompat:
    """Test backward compatibility with old state file format."""

    def test_old_tuple_format_loads(self):
        """Old format: traded_tickers = {ticker: [bot, timestamp]}"""
        old_state = {
            "traded_tickers": {
                "KXHIGHNY-26FEB16-T40": ["weather", "2026-02-16T10:00:00"],
            },
            "bot_spend": {},
            "city_risk": {},
            "total_risk_cents": 0,
            "daily_date": datetime.date.today().isoformat(),
        }
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(old_state, f)
            state_path = f.name

        alloc = PortfolioAllocator(client=MagicMock(), state_path=state_path)
        alloc._load_state()

        entry = alloc._traded_tickers["KXHIGHNY-26FEB16-T40"]
        assert isinstance(entry, dict)
        assert entry["bot"] == "weather"
        assert entry["signal_quality"] == 0.0  # old format has no quality


class TestBalanceCacheTTL:
    """Test that balance cache expires after 5 seconds (Fix 5)."""

    def _make_allocator(self):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (50000, 50000)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc, mock_client

    def test_cache_expires_after_5_seconds(self):
        """Balance should be re-fetched after 5 seconds."""
        alloc, mock_client = self._make_allocator()
        # First call fetches from API
        alloc._get_balance()
        assert mock_client.get_balance.call_count == 1
        # Simulate cache expiry by backdating the fetch time
        alloc._balance_fetched_at = time.time() - 6
        alloc._get_balance()
        assert mock_client.get_balance.call_count == 2

    def test_cache_hit_within_window(self):
        """Balance should be cached within 5 seconds."""
        alloc, mock_client = self._make_allocator()
        alloc._get_balance()
        alloc._get_balance()
        alloc._get_balance()
        # Only one actual API call
        assert mock_client.get_balance.call_count == 1


# ===================================================================
# Region Exposure tests (Phase 4.5)
# ===================================================================

class TestRegionExposure:
    """Test correlated city/region exposure limits."""

    def _make_allocator(self, balance=50000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_region_constants_defined(self):
        """Region groupings should include known correlated cities."""
        assert "HOU" in _CITY_TO_REGION
        assert "AUS" in _CITY_TO_REGION
        assert _CITY_TO_REGION["HOU"] == _CITY_TO_REGION["AUS"]

    def test_all_traded_cities_have_regions(self):
        """Every city in kalshi-config.json should have a region."""
        for city in ["MIA", "LAX", "CHI", "DEN", "NY", "PHIL", "HOU", "AUS"]:
            assert city in _CITY_TO_REGION, f"{city} missing from _CITY_TO_REGION"

    def test_region_limit_blocks_combined(self):
        """Combined HOU + AUS spending should trigger region limit."""
        alloc = self._make_allocator(balance=50000)
        # Max region risk = 50000 * 0.15 = 7500 cents
        # Fill up HOU to near the limit
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 4000, edge=0.10)
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B78", 3500, edge=0.10)
        # Now AUS should be blocked — region already at 7500 >= 7500
        result = alloc.request_budget("weather", "KXHIGHAUS-26FEB16-B90", edge=0.12)
        assert not result.approved
        assert "region" in result.reason

    def test_different_region_not_blocked(self):
        """NY trades should NOT block CHI trades (different region or no region)."""
        alloc = self._make_allocator(balance=50000)
        alloc.record_trade("weather", "KXHIGHNY-26FEB16-T40", 4000, edge=0.10)
        # CHI has no region grouping, should not be blocked by NY
        result = alloc.request_budget("weather", "KXHIGHCHI-26FEB16-T50", edge=0.12)
        assert result.approved


# ===================================================================
# Max Concurrent Positions tests (Phase 4.6)
# ===================================================================

class TestMaxConcurrentPositions:
    """Test max concurrent positions limit."""

    def _make_allocator(self, balance=50000, max_positions=5, position_count=0):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        positions = [{"total_traded": 1}] * position_count
        mock_client.get.return_value = {"market_positions": positions}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(
            client=mock_client, state_path=state_path, max_positions=max_positions
        )
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_blocks_when_at_limit(self):
        """Should reject when position count >= max_positions."""
        alloc = self._make_allocator(max_positions=5, position_count=5)
        result = alloc.request_budget("weather", "KXHIGHCHI-26FEB16-T50", edge=0.12)
        assert not result.approved
        assert "max concurrent positions" in result.reason

    def test_allows_under_limit(self):
        """Should allow when position count < max_positions."""
        alloc = self._make_allocator(max_positions=20, position_count=5)
        result = alloc.request_budget("weather", "KXHIGHCHI-26FEB16-T50", edge=0.12)
        assert result.approved


# ===================================================================
# Configurable Daily Loss Cap tests
# ===================================================================

class TestConfigurableDailyLossCap:
    """Test that absoluteDailyLossCap is configurable from bots-config.json."""

    def test_load_absolute_cap_from_config(self):
        """_load_absolute_cap should read from bots-config.json."""
        cap = _load_absolute_cap()
        # Our config has absoluteDailyLossCap: 150, so cap should be 15000 cents
        assert cap == 15000

    def test_safety_ceiling(self):
        """Cap should be clamped at $500 max."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {"absoluteDailyLossCap": 9999}})):
                cap = _load_absolute_cap()
        assert cap == 50000  # $500 max

    def test_safety_floor(self):
        """Cap should be clamped at $10 min."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {"absoluteDailyLossCap": 1}})):
                cap = _load_absolute_cap()
        assert cap == 1000  # $10 min

    def test_default_when_key_missing(self):
        """Missing absoluteDailyLossCap key should default to $100."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {}})):
                cap = _load_absolute_cap()
        assert cap == 10000  # $100 default

    def test_default_on_corrupt_config(self):
        """Corrupt config file should fall back to $100."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text", return_value="not json"):
                cap = _load_absolute_cap()
        assert cap == 10000  # $100 default
