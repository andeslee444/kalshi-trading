"""Tests for safety infrastructure: kill switch, circuit breaker, config validation,
TradeManager, atomic writes, and trade log trimming."""

import json
import time
import datetime
import types
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import directly from kalshi_auth since conftest.py adds src/kalshi to sys.path
from kalshi_auth import (
    check_kill_switch,
    CircuitBreaker,
    validate_trade_config,
    TradeManager,
    _atomic_write_json,
    trim_trade_log,
    load_trades,
    save_trade,
    RecentTradeTracker,
    KILL_SWITCH_PATH,
)


# ===================================================================
# Kill Switch tests
# ===================================================================

class TestKillSwitch:

    def test_returns_false_when_file_absent(self, tmp_path):
        assert check_kill_switch(tmp_path / "HALT_TRADING") is False

    def test_returns_true_when_file_present(self, tmp_path):
        halt_file = tmp_path / "HALT_TRADING"
        halt_file.touch()
        assert check_kill_switch(halt_file) is True

    def test_default_path_constant_set(self):
        assert KILL_SWITCH_PATH is not None
        assert KILL_SWITCH_PATH.name == "HALT_TRADING"


# ===================================================================
# Circuit Breaker tests
# ===================================================================

class TestCircuitBreaker:

    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.is_open() is False

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(max_failures=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.is_open() is False

    def test_opens_after_n_failures(self):
        cb = CircuitBreaker(max_failures=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open() is True

    def test_resets_on_success(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.is_open() is False

    def test_auto_resets_after_timeout(self):
        cb = CircuitBreaker(max_failures=2, reset_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True
        time.sleep(0.15)
        assert cb.is_open() is False

    def test_remains_open_before_timeout(self):
        cb = CircuitBreaker(max_failures=2, reset_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True


# ===================================================================
# Config Validation tests
# ===================================================================

class TestConfigValidation:

    def test_valid_config_passes(self):
        config = {"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25}
        validate_trade_config(config)  # should not raise

    def test_negative_trade_amount_fails(self):
        with pytest.raises(ValueError, match="maxTradeAmount"):
            validate_trade_config({"maxTradeAmount": -1, "maxDailyTrades": 10, "maxDailyLoss": 25})

    def test_zero_trade_amount_fails(self):
        with pytest.raises(ValueError, match="maxTradeAmount"):
            validate_trade_config({"maxTradeAmount": 0, "maxDailyTrades": 10, "maxDailyLoss": 25})

    def test_excessive_trade_amount_fails(self):
        with pytest.raises(ValueError, match="maxTradeAmount.*safety cap"):
            validate_trade_config({"maxTradeAmount": 200, "maxDailyTrades": 10, "maxDailyLoss": 25})

    def test_negative_daily_trades_fails(self):
        with pytest.raises(ValueError, match="maxDailyTrades"):
            validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": -1, "maxDailyLoss": 25})

    def test_float_daily_trades_fails(self):
        with pytest.raises(ValueError, match="maxDailyTrades"):
            validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": 5.5, "maxDailyLoss": 25})

    def test_excessive_daily_trades_fails(self):
        with pytest.raises(ValueError, match="maxDailyTrades.*safety cap"):
            validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": 200, "maxDailyLoss": 25})

    def test_negative_daily_loss_fails(self):
        with pytest.raises(ValueError, match="maxDailyLoss"):
            validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": -10})

    def test_excessive_daily_loss_fails(self):
        with pytest.raises(ValueError, match="maxDailyLoss.*safety cap"):
            validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 1000})

    def test_bot_name_in_error_message(self):
        with pytest.raises(ValueError, match="\\[mybot\\]"):
            validate_trade_config(
                {"maxTradeAmount": -1, "maxDailyTrades": 10, "maxDailyLoss": 25},
                bot_name="mybot"
            )


# ===================================================================
# Atomic Write tests
# ===================================================================

class TestAtomicWrite:

    def test_creates_valid_json(self, tmp_path):
        p = tmp_path / "test.json"
        data = [{"a": 1}, {"b": 2}]
        _atomic_write_json(p, data)
        assert json.loads(p.read_text()) == data

    def test_overwrites_corrupt_file(self, tmp_path):
        p = tmp_path / "test.json"
        p.write_text("NOT JSON {{{")
        _atomic_write_json(p, {"fixed": True})
        assert json.loads(p.read_text()) == {"fixed": True}

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "subdir" / "deep" / "test.json"
        _atomic_write_json(p, [1, 2, 3])
        assert json.loads(p.read_text()) == [1, 2, 3]


# ===================================================================
# Trim Trade Log tests
# ===================================================================

class TestTrimTradeLog:

    def test_trims_old_entries(self, tmp_path):
        p = tmp_path / "trades.json"
        old_ts = (datetime.datetime.now() - datetime.timedelta(days=100)).isoformat()
        new_ts = datetime.datetime.now().isoformat()
        trades = [
            {"timestamp": old_ts, "ticker": "OLD"},
            {"timestamp": new_ts, "ticker": "NEW"},
        ]
        _atomic_write_json(p, trades)
        trim_trade_log(p, max_age_days=90)
        result = json.loads(p.read_text())
        assert len(result) == 1
        assert result[0]["ticker"] == "NEW"

    def test_trims_by_max_count(self, tmp_path):
        p = tmp_path / "trades.json"
        ts = datetime.datetime.now().isoformat()
        trades = [{"timestamp": ts, "ticker": f"T{i}"} for i in range(20)]
        _atomic_write_json(p, trades)
        trim_trade_log(p, max_entries=5)
        result = json.loads(p.read_text())
        assert len(result) == 5
        # Should keep the most recent (last 5)
        assert result[0]["ticker"] == "T15"

    def test_no_op_when_empty(self, tmp_path):
        p = tmp_path / "trades.json"
        trim_trade_log(p)  # file doesn't exist — should not raise

    def test_no_op_when_all_recent(self, tmp_path):
        p = tmp_path / "trades.json"
        ts = datetime.datetime.now().isoformat()
        trades = [{"timestamp": ts, "ticker": "A"}]
        _atomic_write_json(p, trades)
        trim_trade_log(p, max_age_days=90)
        result = json.loads(p.read_text())
        assert len(result) == 1


# ===================================================================
# TradeManager tests
# ===================================================================

def _make_manager(tmp_path, config=None, **kwargs):
    """Helper to create a TradeManager with a mock client."""
    mock_client = MagicMock()
    mock_client.post.return_value = {
        "order": {"order_id": "test-123", "status": "resting"}
    }
    trades_path = tmp_path / "trades.json"
    cfg = config or {"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25}
    kill_path = tmp_path / "HALT"
    mgr = TradeManager(
        mock_client, trades_path, cfg,
        kill_switch_path=kill_path,
        **kwargs
    )
    return mgr, mock_client, kill_path


class TestTradeManager:

    def test_successful_trade(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path)
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is not None
        assert result["order_id"] == "test-123"
        client.post.assert_called_once()

    def test_trade_saved_to_file(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        trades = load_trades(mgr.trades_path)
        assert len(trades) == 1
        assert trades[0]["ticker"] == "TICK-1"

    def test_daily_trade_limit(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path, {"maxTradeAmount": 5, "maxDailyTrades": 2, "maxDailyLoss": 100})
        mgr.place_order("T1", "yes", 10, 1, "r1")
        mgr.place_order("T2", "yes", 10, 1, "r2")
        result = mgr.place_order("T3", "yes", 10, 1, "r3")
        assert result is None

    def test_daily_loss_limit(self, tmp_path):
        # maxDailyLoss=1 dollar = 100 cents
        mgr, _, _ = _make_manager(tmp_path, {"maxTradeAmount": 5, "maxDailyTrades": 100, "maxDailyLoss": 1})
        mgr.place_order("T1", "yes", 90, 1, "r1")
        # Cost so far: 90c. Next would be 50c -> total 140c > 100c limit
        result = mgr.place_order("T2", "yes", 50, 1, "r2")
        assert result is None

    def test_cost_cap_adjustment(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path, {"maxTradeAmount": 1, "maxDailyTrades": 100, "maxDailyLoss": 100})
        # maxTradeAmount=1 dollar = 100 cents. 50c * 5 = 250c > 100c, so count should be adjusted to 2
        mgr.place_order("T1", "yes", 50, 5, "r1")
        call_body = client.post.call_args[1]["body"]
        assert call_body["count"] == 2

    def test_dedup_blocks_repeat(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        mgr.place_order("T1", "yes", 50, 1, "r1")
        result = mgr.place_order("T1", "yes", 50, 1, "r2")
        assert result is None

    def test_kill_switch_blocks(self, tmp_path):
        mgr, _, kill_path = _make_manager(tmp_path)
        kill_path.touch()
        result = mgr.place_order("T1", "yes", 50, 1, "r1")
        assert result is None

    def test_balance_check_blocks(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        result = mgr.place_order("T1", "yes", 50, 3, "r1", available_balance_cents=50)
        assert result is None

    def test_invalid_side_rejected(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        result = mgr.place_order("T1", "buy", 50, 1, "r1")
        assert result is None

    def test_stale_data_rejected(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        result = mgr.place_order("T1", "yes", 50, 1, "r1", market_data_age_seconds=700)
        assert result is None

    def test_extra_fields_stored(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path)
        mgr.place_order("T1", "yes", 50, 1, "r1", confidence=0.95, strategy="test")
        trades = load_trades(mgr.trades_path)
        assert trades[0]["confidence"] == 0.95
        assert trades[0]["strategy"] == "test"

    def test_circuit_breaker_blocks_after_failures(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path)
        client.post.side_effect = Exception("API down")
        for i in range(5):
            mgr.place_order(f"T{i}", "yes", 10, 1, "r")
        # Now the breaker should be open, and even with a working client, orders are refused
        client.post.side_effect = None
        client.post.return_value = {"order": {"order_id": "ok", "status": "resting"}}
        result = mgr.place_order("T99", "yes", 10, 1, "r")
        assert result is None

    def test_no_side_sets_correct_price_key(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path)
        mgr.place_order("T1", "no", 40, 1, "r1")
        call_body = client.post.call_args[1]["body"]
        assert "no_price" in call_body
        assert "yes_price" not in call_body

    def test_daily_counters_reset_on_new_day(self, tmp_path):
        mgr, _, _ = _make_manager(tmp_path, {"maxTradeAmount": 5, "maxDailyTrades": 1, "maxDailyLoss": 100})
        mgr.place_order("T1", "yes", 10, 1, "r1")
        # Simulate next day
        mgr._daily_date = "1999-01-01"
        result = mgr.place_order("T2", "yes", 10, 1, "r2")
        assert result is not None
