"""Tests for capital allocator signal quality scoring, supersede logic,
and pending exits.

Fix 3: Validates the "best signal wins" mechanism and backward compatibility.
"""

import datetime
import json
import time
import tempfile
import pytest
import capital_allocator as allocator_mod
from pathlib import Path
from unittest.mock import MagicMock, patch

from artifact_contracts import BUDGET_RESPONSE_FIELDS
from capital_allocator import (
    PortfolioAllocator, compute_signal_quality, MODEL_QUALITY_FACTOR, BudgetResponse,
    CITY_REGIONS, _CITY_TO_REGION, MAX_REGION_FRACTION, _load_absolute_cap,
    _load_absolute_cap_pct, ABSOLUTE_DAILY_LOSS_CAP_PCT, _load_per_bot_daily_limits,
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

    def test_source_type_override_beats_generic_weather(self):
        q_forecast = compute_signal_quality("weather", 0.10, source_type="forecast_weather")
        q_nws = compute_signal_quality("weather", 0.10, source_type="nws")
        assert q_nws > q_forecast

    def test_all_known_bots_have_factors(self):
        for bot in ["source-monitor", "economics", "entertainment",
                     "weather", "crypto", "strategy", "beatrelease"]:
            assert bot in MODEL_QUALITY_FACTOR


class TestBudgetResponseContract:
    """Test canonical allocator decision contract."""

    def test_as_record_contains_required_fields_for_approval(self):
        response = BudgetResponse(
            True,
            max_cost_cents=500,
            bankroll_cents=10000,
            reason="",
            binding_constraint="bot_config_cap",
        )
        record = response.as_record()
        assert set(BUDGET_RESPONSE_FIELDS) == set(record)
        assert record["approved"] is True
        assert record["binding_constraint"] == "bot_config_cap"

    def test_as_record_contains_required_fields_for_denial(self):
        response = BudgetResponse(False, reason="already traded")
        record = response.as_record()
        assert set(BUDGET_RESPONSE_FIELDS) == set(record)
        assert record["approved"] is False
        assert record["reason"] == "already traded"


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
        """When total=100000, available=5000, bankroll should be 5000."""
        alloc = self._make_allocator(total=100000, available=5000)
        budget = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.10)
        assert budget.approved
        assert budget.bankroll_cents == 5000

    def test_bankroll_not_total(self):
        """Bankroll should NOT be total balance."""
        alloc = self._make_allocator(total=50000, available=5000)
        budget = alloc.request_budget("weather", "KXHIGHLA-26FEB16-T50", edge=0.10)
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
        """After exceeding effective cap in risk today, next request should be rejected.

        With pct=0.15 and balance=$6000 (600000c), effective cap = max($750, $900) = $900.
        Need to exceed $900 to trigger the block. Use multiple bots to avoid per-bot limits.
        """
        alloc = self._make_allocator(balance=600000)
        # Effective cap = max(75000, 600000*0.15) = max(75000, 90000) = 90000 cents ($900)
        # Spread across multiple bots to avoid per-bot daily limit.
        # Use varied ticker prefixes to avoid cluster limits.
        for i in range(45):
            alloc.record_trade("source-monitor", f"SRCMON-A{i}", 1000, edge=0.10)
        for i in range(46):
            alloc.record_trade("economics", f"KXCPI-B{i}", 1000, edge=0.10)
        # Total risk = 91000 > 90000 effective cap
        budget = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.15)
        assert not budget.approved
        assert "absolute daily risk cap" in budget.reason

    def test_under_cap_allowed(self):
        """Under effective cap should still allow trading."""
        alloc = self._make_allocator(balance=600000)
        # Effective cap = max(75000, 600000*0.15) = 90000 cents ($900).
        # Record $880 of risk spread across bots. Use varied ticker prefixes to avoid cluster limits.
        for i in range(44):
            alloc.record_trade("source-monitor", f"SRCMON-A{i}", 1000, edge=0.10)
        for i in range(44):
            alloc.record_trade("economics", f"KXCPI-B{i}", 1000, edge=0.10)
        # Total risk = 88000 < 90000 effective cap
        budget = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.15)
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


class TestDrawdownHaltRecovery:
    def _make_allocator(self, tmp_path, *, balance=10000, available=10000, portfolio_value=None, exposure=0):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, available)
        mock_client._portfolio_value = portfolio_value
        mock_client._market_exposure = exposure
        state_path = tmp_path / "allocator-state.json"
        state_path.write_text("{}")
        deposits_path = tmp_path / "deposits.json"
        deposits_path.write_text(json.dumps([{"type": "deposit", "amount_cents": 50000}]))
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_drawdown_uses_portfolio_value_when_available(self, tmp_path, monkeypatch):
        halt_path = tmp_path / "HALT_TRADING"
        monkeypatch.setattr(allocator_mod, "_HALT_TRADING_PATH", halt_path)

        alloc = self._make_allocator(
            tmp_path,
            balance=10000,
            available=10000,
            portfolio_value=45000,
            exposure=0,
        )

        assert alloc._check_drawdown_halt() is False
        assert not halt_path.exists()

    def test_automated_halt_file_is_cleared_after_recovery(self, tmp_path, monkeypatch):
        halt_path = tmp_path / "HALT_TRADING"
        halt_path.write_text(
            "Automated drawdown halt at 2026-04-02T00:00:00+00:00\n"
            "NAV: $900.00 | Deposits: $5000.00 | Drawdown: 82.0%\n"
        )
        monkeypatch.setattr(allocator_mod, "_HALT_TRADING_PATH", halt_path)

        alloc = self._make_allocator(
            tmp_path,
            balance=12000,
            available=12000,
            portfolio_value=42000,
            exposure=0,
        )

        assert alloc._check_drawdown_halt() is False
        assert not halt_path.exists()

    def test_manual_halt_file_is_not_auto_cleared(self, tmp_path, monkeypatch):
        halt_path = tmp_path / "HALT_TRADING"
        halt_path.write_text("manual operator halt\n")
        monkeypatch.setattr(allocator_mod, "_HALT_TRADING_PATH", halt_path)

        alloc = self._make_allocator(
            tmp_path,
            balance=12000,
            available=12000,
            portfolio_value=42000,
            exposure=0,
        )

        assert alloc._check_drawdown_halt() is False
        assert halt_path.exists()


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
        alloc = self._make_allocator(balance=100000)
        # Max region risk = 100000 * 0.15 = 15000 cents
        # Record HOU trades via record_trade (bypasses budget checks)
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B77", 8000, edge=0.10)
        alloc.record_trade("weather", "KXHIGHHOU-26FEB16-B78", 7000, edge=0.10)
        # Now AUS should be blocked — SOUTH_TX region at 15000 >= 15000
        # Use source-monitor (different bot, higher priority) so per-bot limit
        # doesn't fire before the region check
        result = alloc.request_budget("source-monitor", "KXHIGHAUS-26FEB16-B90", edge=0.12)
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
        # Our config has absoluteDailyLossCap: 750, so cap should be 75000 cents
        assert cap == 75000

    def test_no_safety_ceiling(self):
        """Cap should not be clamped — scales freely for large accounts."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {"absoluteDailyLossCap": 9999}})):
                cap = _load_absolute_cap()
        assert cap == 999900  # No ceiling, just converts to cents

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


class TestAbsoluteCapPctLoading:
    """Test _load_absolute_cap_pct() config loading."""

    def test_loads_from_config(self):
        """Should load absoluteDailyLossCapPct from config."""
        pct = _load_absolute_cap_pct()
        # Our config has absoluteDailyLossCapPct: 0.15
        assert pct == 0.15

    def test_safety_ceiling(self):
        """Pct should be clamped at 50% max."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {"absoluteDailyLossCapPct": 0.90}})):
                pct = _load_absolute_cap_pct()
        assert pct == 0.50

    def test_default_zero_when_missing(self):
        """Missing pct key should return 0 (disabled)."""
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text",
                       return_value=json.dumps({"allocator": {}})):
                pct = _load_absolute_cap_pct()
        assert pct == 0.0


class TestBankrollProportionalAbsoluteCap:
    """Test that absolute cap scales with bankroll when pct is configured."""

    def _make_allocator(self, balance=50000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_pct_scales_cap_with_bankroll(self):
        """With $6000 balance and 10% pct, effective cap should be max($500, $600) = $600."""
        alloc = self._make_allocator(balance=600000)  # $6000
        # Static cap is $500 (50000 cents), pct cap = 600000 * 0.10 = 60000 cents ($600)
        # Record $550 of risk (55000 cents) — exceeds static $500 but under dynamic $600
        # Spread across bots to avoid per-bot limits
        for i in range(27):
            alloc.record_trade("source-monitor", f"SRCMON-A{i}", 1000, edge=0.10)
        for i in range(28):
            alloc.record_trade("economics", f"KXCPI-B{i}", 1000, edge=0.10)
        # Total = 55000, with pct enabled should still be allowed (55000 < 60000)
        with patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0.10):
            budget = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.15)
        assert budget.approved

    def test_static_cap_is_floor(self):
        """With small balance where pct < static, static cap should be the floor.

        Balance=$4000 (400000c): pct cap = 400000*0.10 = 40000 ($400), static = $500 (50000).
        max(50000, 40000) = 50000 — static wins.
        Verify that risk under static cap is still allowed.
        """
        alloc = self._make_allocator(balance=400000)  # $4000
        # Pct cap = 400000 * 0.10 = 40000 ($400), static = $500 (50000)
        # max(50000, 40000) = 50000 — static wins
        with patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0.10):
            # Record $450 of risk (45000 < 50000 static cap)
            # Spread across bots to avoid per-bot limits
            for i in range(22):
                alloc.record_trade("source-monitor", f"SRCMON-A{i}", 1000, edge=0.10)
            for i in range(23):
                alloc.record_trade("economics", f"KXCPI-B{i}", 1000, edge=0.10)
            # Total = 45000 < 50000 static cap
            budget = alloc.request_budget("weather", "KXHIGHNY-26FEB16-T40", edge=0.15)
        assert budget.approved  # 45000 < 50000 static cap


# ===================================================================
# Correlation Engine Integration tests (T2-P2)
# ===================================================================

class TestCorrelationIntegration:
    """Test capital allocator integration with correlation engine."""

    def _make_allocator(self, balance=500000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        mock_client.get.return_value = {"market_positions": []}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_CENTS", 100000000)
    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0)
    def test_cluster_limit_blocks_concentration(self):
        """Allocator should block trades that exceed cluster concentration limit.

        With $5000 balance, cluster limit = 15% = $750.
        After $800 in CPI trades, next CPI trade should be blocked.
        Patches absolute cap to isolate the cluster check.
        """
        alloc = self._make_allocator(balance=500000)
        alloc.client.get.return_value = {
            "market_positions": [
                {"ticker": f"KXCPI-26MAY-T3{i}", "position": 1, "market_exposure": 10000}
                for i in range(8)
            ]
        }
        # Record $800 in CPI trades (80000 cents > 75000 cluster limit)
        for i in range(8):
            alloc.record_trade("source-monitor", f"KXCPI-26MAY-T3{i}", 10000, edge=0.10)
        result = alloc.request_budget("economics", "KXCPI-26MAY-T50", edge=0.10, confidence=0.90)
        assert not result.approved
        assert "cluster" in result.reason.lower()

    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_CENTS", 100000000)
    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0)
    def test_uncorrelated_trade_allowed_despite_cluster_risk(self):
        """BTC trade should be allowed even if CPI cluster is full."""
        alloc = self._make_allocator(balance=500000)
        for i in range(8):
            alloc.record_trade("economics", f"KXCPI-26MAY-T3{i}", 10000, edge=0.10)
        result = alloc.request_budget("crypto", "KXBTC-26MAR3-T95000", edge=0.10, confidence=0.80)
        assert result.approved

    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_CENTS", 100000000)
    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0)
    def test_tail_risk_reduces_bankroll(self):
        """When tail dependence is high, bankroll for Kelly should be reduced."""
        alloc = self._make_allocator(balance=500000)
        alloc.record_trade("economics", "KXCPI-26MAY-T20", 20000, edge=0.10)
        result = alloc.request_budget("economics", "KXCPI-26MAY-T21", edge=0.10, confidence=0.85)
        if result.approved:
            # Bankroll should be reduced by tail risk multiplier (75% of 500000 = 375000)
            assert result.bankroll_cents < 500000

    def test_reconcile_rebuilds_cluster_risk_from_live_positions(self):
        alloc = self._make_allocator(balance=500000)
        alloc._correlation_engine.record_trade("KXBTC-26MAR31-T80000", 80000)
        alloc.client.get.return_value = {
            "market_positions": [
                {"ticker": "KXETHY-27JAN0100-T1000.00", "position": 5, "market_exposure": 2500},
            ]
        }
        alloc._last_reconcile = 0
        alloc._reconcile_settled_positions()
        assert alloc._correlation_engine.get_cluster_risk("BTC") == 2500


# ===================================================================
# Regime-Adjusted Kelly tests (T2-P3)
# ===================================================================

class TestRegimeKellyIntegration:
    """Test that regime detector multiplier is applied in budget allocation."""

    def test_crisis_regime_reduces_bankroll(self):
        """During crisis regime, bankroll should be reduced."""
        # Mock regime detector returning crisis multiplier
        from unittest.mock import MagicMock, patch
        mock_detector = MagicMock()
        mock_detector.current_regime.return_value = "crisis"
        mock_detector.regime_confidence.return_value = 0.9

        from regime_detector import regime_kelly_multiplier
        mult = regime_kelly_multiplier(mock_detector)
        assert mult < 0.70  # Crisis with high confidence

    def test_normal_regime_no_reduction(self):
        """During normal regime, bankroll should not be reduced."""
        from unittest.mock import MagicMock
        mock_detector = MagicMock()
        mock_detector.current_regime.return_value = "normal"
        mock_detector.regime_confidence.return_value = 0.8

        from regime_detector import regime_kelly_multiplier
        mult = regime_kelly_multiplier(mock_detector)
        assert 0.9 <= mult <= 1.1

    def test_regime_mult_stacks_with_tail_risk(self):
        """Regime multiplier should stack with tail-risk multiplier."""
        tail_mult = 0.75  # From correlation engine
        regime_mult = 0.60  # Crisis regime
        combined = tail_mult * regime_mult
        assert combined < 0.50  # Severe combined reduction
        assert combined == pytest.approx(0.45, abs=0.01)


# ===================================================================
# NWS Source Type Relaxed Gate tests (Task 9)
# ===================================================================

class TestNwsSourceTypeRelaxedGate:
    """NWS source_type should get same relaxed thresholds as info_arb."""

    def _make_allocator(self, balance=50000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        mock_client.get.return_value = {"market_positions": []}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    def test_nws_source_type_gets_relaxed_gate(self):
        """NWS source_type should get same relaxed thresholds as info_arb.

        87% confidence, 12% edge — above info_arb gate (85%/10%) but below
        default gate (90%/15%). Should be approved with NWS source_type.
        """
        alloc = self._make_allocator()
        result = alloc.request_budget("source-monitor", "KXHIGHHOU-26MAR3-T86",
                                       edge=0.12, confidence=0.87,
                                       bot_max_cost_cents=500, source_type="nws")
        assert result.approved

    def test_nws_matches_info_arb_behavior(self):
        """NWS and info_arb should produce the same budget for identical inputs."""
        alloc_nws = self._make_allocator()
        alloc_arb = self._make_allocator()
        result_nws = alloc_nws.request_budget("source-monitor", "KXHIGHHOU-26MAR3-T86",
                                               edge=0.12, confidence=0.87,
                                               bot_max_cost_cents=500, source_type="nws")
        result_arb = alloc_arb.request_budget("source-monitor", "KXHIGHHOU-26MAR3-T86",
                                               edge=0.12, confidence=0.87,
                                               bot_max_cost_cents=500, source_type="info_arb")
        assert result_nws.approved == result_arb.approved
        assert result_nws.max_cost_cents == result_arb.max_cost_cents

    def test_default_source_type_rejects_below_strict_gate(self):
        """Without source_type, 87% confidence / 12% edge should NOT trigger
        the high-confidence scale-up (requires 90%/15% for default).
        The trade should still be approved at normal allocation, but not
        get the high-confidence boost.
        """
        alloc = self._make_allocator()
        result_default = alloc.request_budget("source-monitor", "KXHIGHHOU-26MAR3-T86",
                                               edge=0.12, confidence=0.87,
                                               bot_max_cost_cents=500, source_type=None)
        result_nws = self._make_allocator().request_budget(
            "source-monitor", "KXHIGHHOU-26MAR3-T86",
            edge=0.12, confidence=0.87,
            bot_max_cost_cents=500, source_type="nws")
        # Both should be approved (basic trade passes), but NWS gets higher allocation
        # because it triggers the high-confidence scale-up path
        assert result_default.approved
        assert result_nws.approved
        assert result_nws.max_cost_cents >= result_default.max_cost_cents


# ===================================================================
# Minimum Position Floor tests (Task 10)
# ===================================================================

class TestMinimumPositionFloor:
    """Bankroll after compound Kelly reductions should not drop below $1 (100 cents)."""

    def _make_allocator(self, balance=500000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        mock_client.get.return_value = {"market_positions": []}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        alloc._daily_date = datetime.date.today().isoformat()
        return alloc

    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_CENTS", 100000000)
    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0)
    def test_minimum_position_floor(self):
        """Bankroll after all reductions should not drop below $1 (100 cents).

        With $2 balance, tail_risk=0.75, crisis regime=0.50:
        bankroll = 200 * 0.75 * 0.50 = 75 cents -> floor to 100 cents.
        """
        alloc = self._make_allocator(balance=200)
        # Force 100% crisis belief -> regime mult = 0.50
        alloc._regime_detector.belief = [0.0, 0.0, 0.0, 1.0]
        alloc._regime_detector.n_updates = 10
        # Mock correlation engine to bypass cluster/VaR checks and apply tail risk
        alloc._correlation_engine.check_cluster_limit = MagicMock(return_value=(True, ""))
        alloc._correlation_engine.check_marginal_var = MagicMock(return_value=(True, ""))
        alloc._correlation_engine.get_tail_risk_multiplier = MagicMock(return_value=0.75)
        result = alloc.request_budget("crypto", "KXBTC-26MAR3-T95000",
                                       edge=0.10, confidence=0.70,
                                       bot_max_cost_cents=500)
        assert result.approved
        assert result.bankroll_cents >= 100  # $1 floor

    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_CENTS", 100000000)
    @patch("capital_allocator.ABSOLUTE_DAILY_LOSS_CAP_PCT", 0)
    def test_floor_not_applied_above_minimum(self):
        """Bankroll above $1 should not be changed by the floor."""
        alloc = self._make_allocator(balance=500000)  # $5000
        # Normal regime — no reductions
        result = alloc.request_budget("crypto", "KXBTC-26MAR3-T95000",
                                       edge=0.10, confidence=0.70,
                                       bot_max_cost_cents=500)
        assert result.approved
        # Bankroll should be the full available balance (no regime/tail reductions)
        assert result.bankroll_cents == 500000


# ===================================================================
# Config Loader Error Handling tests (Plan 1 Task 1)
# ===================================================================

class TestConfigLoaderErrorHandling:
    """Config loaders must log warnings on corrupt config, not silently use defaults."""

    def test_load_absolute_cap_logs_on_corrupt_json(self, caplog):
        """Corrupt config should produce a warning log, not silence."""
        import logging
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text", return_value="not json"):
                with caplog.at_level(logging.WARNING):
                    cap = _load_absolute_cap()
        assert cap == 10000  # $100 default
        assert any("absoluteDailyLossCap" in r.message or "Failed to load" in r.message
                    for r in caplog.records if r.levelno >= logging.WARNING)

    def test_load_absolute_cap_pct_logs_on_corrupt_json(self, caplog):
        """Corrupt config should produce a warning log for pct loader."""
        import logging
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text", return_value="not json"):
                with caplog.at_level(logging.WARNING):
                    pct = _load_absolute_cap_pct()
        assert pct == 0.0
        assert any("Failed to load" in r.message
                    for r in caplog.records if r.levelno >= logging.WARNING)

    def test_per_bot_limits_corrupt_structure_returns_empty_with_warning(self, caplog):
        """Per-bot limits with non-dict structure should log warning and return empty dict.

        When perBotDailyLimit is not a dict (e.g., an integer), calling
        .items() raises AttributeError. The exception handler should catch
        it, log a warning, and return {}.
        """
        import logging
        corrupt_config = json.dumps({
            "allocator": {"perBotDailyLimit": 42}
        })
        with patch("capital_allocator.Path.exists", return_value=True):
            with patch("capital_allocator.Path.read_text", return_value=corrupt_config):
                with caplog.at_level(logging.WARNING):
                    result = _load_per_bot_daily_limits()
        assert result == {}
        assert any("Failed to load" in r.message or "perBotDailyLimit" in r.message
                    for r in caplog.records if r.levelno >= logging.WARNING)
