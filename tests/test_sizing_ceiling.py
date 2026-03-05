"""Tests for _effective_max_trade_cents and _effective_max_daily_loss_cents ceiling behavior."""
import pytest
from unittest.mock import MagicMock, patch


def _make_trade_manager(max_trade=50, max_trade_pct=0.05, max_loss=300, max_loss_pct=0.15):
    """Create a TradeManager with controllable config."""
    from kalshi_auth import TradeManager
    client = MagicMock()
    config = {
        "maxTradeAmount": max_trade,
        "maxTradeAmountPct": max_trade_pct,
        "maxDailyTrades": 10,
        "maxDailyLoss": max_loss,
        "maxDailyLossPct": max_loss_pct,
    }
    tm = TradeManager(client, "/tmp/test-trades.json", config, logger=MagicMock())
    return tm


class TestEffectiveMaxTradeCents:
    def test_static_is_ceiling_not_floor(self):
        """Static maxTradeAmount ($50) should cap dynamic percentage."""
        tm = _make_trade_manager(max_trade=50, max_trade_pct=0.05)
        # Mock balance at $4000 (400000 cents)
        tm._get_available_balance = lambda: 400000
        result = tm._effective_max_trade_cents()
        # Dynamic = 400000 * 0.05 = 20000 (=$200)
        # Static = 50 * 100 = 5000 (=$50)
        # Should be min(5000, 20000) = 5000
        assert result == 5000, f"Expected $50 ceiling, got ${result/100}"

    def test_dynamic_used_when_smaller(self):
        """When balance is small, dynamic (pct) should be used."""
        tm = _make_trade_manager(max_trade=50, max_trade_pct=0.05)
        # Mock balance at $500 (50000 cents)
        tm._get_available_balance = lambda: 50000
        result = tm._effective_max_trade_cents()
        # Dynamic = 50000 * 0.05 = 2500 (=$25)
        # Static = 5000 (=$50)
        # Should be min(5000, 2500) = 2500
        assert result == 2500, f"Expected $25 (5% of $500), got ${result/100}"

    def test_no_pct_uses_static(self):
        """Without maxTradeAmountPct, use static only."""
        tm = _make_trade_manager(max_trade=50, max_trade_pct=0)
        result = tm._effective_max_trade_cents()
        assert result == 5000

    def test_zero_balance_uses_static(self):
        """Zero balance should fall back to static."""
        tm = _make_trade_manager(max_trade=50, max_trade_pct=0.05)
        tm._get_available_balance = lambda: 0
        result = tm._effective_max_trade_cents()
        assert result == 5000


class TestEffectiveMaxDailyLossCents:
    def test_static_is_ceiling_not_floor(self):
        """Static maxDailyLoss ($300) should cap dynamic percentage."""
        tm = _make_trade_manager(max_loss=300, max_loss_pct=0.15)
        tm._get_available_balance = lambda: 400000
        result = tm._effective_max_daily_loss_cents()
        # Dynamic = 400000 * 0.15 = 60000 (=$600)
        # Static = 300 * 100 = 30000 (=$300)
        # Should be min(30000, 60000) = 30000
        assert result == 30000, f"Expected $300 ceiling, got ${result/100}"

    def test_dynamic_used_when_smaller(self):
        """When balance is small, dynamic should be used."""
        tm = _make_trade_manager(max_loss=300, max_loss_pct=0.15)
        tm._get_available_balance = lambda: 100000
        result = tm._effective_max_daily_loss_cents()
        # Dynamic = 100000 * 0.15 = 15000 (=$150)
        # Static = 30000 (=$300)
        # Should be min(30000, 15000) = 15000
        assert result == 15000, f"Expected $150 (15% of $1000), got ${result/100}"
