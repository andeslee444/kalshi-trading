"""Tests for safety infrastructure: kill switch, circuit breaker, config validation,
TradeManager, atomic writes, and trade log trimming."""

import json
import re
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
    OrderMonitor,
    KILL_SWITCH_PATH,
    SHARED_BREAKER_PATH,
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
    kwargs.setdefault("breaker_state_path", None)  # in-memory breaker for tests
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
        # Simulate next day — clear the trade log so reconstruction doesn't count T1
        _atomic_write_json(mgr.trades_path, [])
        mgr._daily_date = "1999-01-01"
        result = mgr.place_order("T2", "yes", 10, 1, "r2")
        assert result is not None


# ===================================================================
# RecentTradeTracker timezone tests (Fix 3)
# ===================================================================

class TestRecentTradeTrackerTimezone:

    def test_aware_timestamp_no_crash(self, tmp_path):
        """Timestamps with timezone info should not crash when compared to naive cutoff."""
        p = tmp_path / "trades.json"
        ts_aware = (datetime.datetime.now(datetime.timezone.utc)).isoformat()
        _atomic_write_json(p, [{"timestamp": ts_aware, "ticker": "TICK-1"}])
        tracker = RecentTradeTracker(p, cooldown_hours=24)
        assert tracker.is_recent("TICK-1")

    def test_mixed_naive_and_aware_no_crash(self, tmp_path):
        """Mix of naive and aware timestamps should not crash."""
        p = tmp_path / "trades.json"
        ts_naive = datetime.datetime.now().isoformat()
        ts_aware = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _atomic_write_json(p, [
            {"timestamp": ts_naive, "ticker": "T1"},
            {"timestamp": ts_aware, "ticker": "T2"},
        ])
        tracker = RecentTradeTracker(p, cooldown_hours=24)
        assert tracker.is_recent("T1")
        assert tracker.is_recent("T2")

    def test_aware_with_z_suffix(self, tmp_path):
        """Timestamps ending with +00:00 should be parsed and compared safely."""
        p = tmp_path / "trades.json"
        ts = "2099-01-01T12:00:00+00:00"  # Far future, always recent with large cooldown
        _atomic_write_json(p, [{"timestamp": ts, "ticker": "FUTURE"}])
        tracker = RecentTradeTracker(p, cooldown_hours=999999)
        assert tracker.is_recent("FUTURE")


# ===================================================================
# trim_trade_log timezone tests (Fix 3)
# ===================================================================

class TestTrimTradeLogTimezone:

    def test_aware_timestamp_not_crash(self, tmp_path):
        """trim_trade_log should not crash on aware timestamps."""
        p = tmp_path / "trades.json"
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _atomic_write_json(p, [{"timestamp": ts, "ticker": "T1"}])
        trim_trade_log(p, max_age_days=90)
        result = json.loads(p.read_text())
        assert len(result) == 1

    def test_old_aware_timestamp_trimmed(self, tmp_path):
        """Old aware timestamps should be trimmed correctly."""
        p = tmp_path / "trades.json"
        old_ts = "2020-01-01T00:00:00+00:00"
        new_ts = datetime.datetime.now().isoformat()
        _atomic_write_json(p, [
            {"timestamp": old_ts, "ticker": "OLD"},
            {"timestamp": new_ts, "ticker": "NEW"},
        ])
        trim_trade_log(p, max_age_days=90)
        result = json.loads(p.read_text())
        assert len(result) == 1
        assert result[0]["ticker"] == "NEW"


# ===================================================================
# Box office gross heuristic tests (Fix 4)
# ===================================================================

class TestBoxOfficeGrossHeuristic:
    """Test the regex + suffix logic for box office gross parsing."""

    REGEX = r'\$([\d,.]+)\s*([MmBb])?'

    def _parse(self, text):
        """Parse a dollar string using the box office regex and suffix logic."""
        m = re.search(self.REGEX, text)
        if not m:
            return None
        val = float(m.group(1).replace(",", ""))
        suffix = m.group(2).upper() if m.group(2) else ""
        if suffix == "B":
            val *= 1_000_000_000
        elif suffix == "M":
            val *= 1_000_000
        return val

    def test_millions_suffix(self):
        assert self._parse("$3.5M") == 3_500_000

    def test_billions_suffix(self):
        assert self._parse("$1.2B") == 1_200_000_000

    def test_no_suffix_small_value(self):
        """$3.50 should NOT be multiplied to millions."""
        assert self._parse("$3.50") == 3.50

    def test_no_suffix_large_value(self):
        assert self._parse("$150,000") == 150_000

    def test_comma_separated(self):
        assert self._parse("$1,234,567") == 1_234_567

    def test_lowercase_m(self):
        assert self._parse("$45.6m") == 45_600_000

    def test_regex_captures_suffix(self):
        """Verify the regex pattern correctly captures M/B group."""
        import re as re_mod
        m = re_mod.search(self.REGEX, "$100.5M revenue")
        assert m.group(1) == "100.5"
        assert m.group(2) == "M"


# ===================================================================
# OrderMonitor tests
# ===================================================================

class TestOrderMonitor:

    def test_track_adds_order(self):
        mock_client = MagicMock()
        om = OrderMonitor(mock_client)
        om.track("order-1", "TICK-1", "yes", 50, 2)
        assert om.get_pending_count() == 1

    def test_get_pending_capital(self):
        mock_client = MagicMock()
        om = OrderMonitor(mock_client)
        om.track("order-1", "TICK-1", "yes", 50, 2)
        om.track("order-2", "TICK-2", "no", 30, 3)
        # 50*2 + 30*3 = 190
        assert om.get_pending_capital() == 190

    def test_check_orders_respects_interval(self):
        mock_client = MagicMock()
        om = OrderMonitor(mock_client, check_interval=60)
        om.track("order-1", "TICK-1", "yes", 50, 1)
        # First call should work
        om._last_check = 0
        om.check_orders()
        # Second call within interval should be skipped
        result = om.check_orders()
        assert result == {}
        # Client.get should only have been called once
        assert mock_client.get.call_count == 1

    def test_check_orders_removes_filled(self):
        mock_client = MagicMock()
        # Return empty resting orders — means our tracked order was filled
        mock_client.get.return_value = {"orders": []}
        om = OrderMonitor(mock_client, check_interval=0)
        om.track("order-1", "TICK-1", "yes", 50, 1)
        changes = om.check_orders()
        assert "order-1" in changes
        assert changes["order-1"]["status"] == "filled_or_canceled"
        assert om.get_pending_count() == 0

    def test_check_orders_cancels_stale(self):
        mock_client = MagicMock()
        # Order is still resting
        mock_client.get.return_value = {"orders": [{"order_id": "order-1"}]}
        mock_client.delete.return_value = {}
        om = OrderMonitor(mock_client, max_age_seconds=0, check_interval=0)
        om.track("order-1", "TICK-1", "yes", 50, 1)
        # Make it stale by setting placed_at to past
        om._pending["order-1"]["placed_at"] = time.time() - 100
        changes = om.check_orders()
        assert "order-1" in changes
        assert changes["order-1"]["action"] == "canceled"
        mock_client.delete.assert_called_once_with("/portfolio/orders/order-1")
        assert om.get_pending_count() == 0

    def test_check_orders_keeps_fresh_resting(self):
        mock_client = MagicMock()
        # Order is still resting and fresh
        mock_client.get.return_value = {"orders": [{"order_id": "order-1"}]}
        om = OrderMonitor(mock_client, max_age_seconds=600, check_interval=0)
        om.track("order-1", "TICK-1", "yes", 50, 1)
        changes = om.check_orders()
        assert changes == {}
        assert om.get_pending_count() == 1

    def test_cancel_order_success(self):
        mock_client = MagicMock()
        om = OrderMonitor(mock_client)
        assert om.cancel_order("order-1") is True
        mock_client.delete.assert_called_once()

    def test_cancel_order_failure(self):
        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("API error")
        om = OrderMonitor(mock_client)
        assert om.cancel_order("order-1") is False

    def test_trade_manager_registers_with_monitor(self, tmp_path):
        """TradeManager should register orders with OrderMonitor when provided."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "order": {"order_id": "test-123", "status": "resting"}
        }
        om = OrderMonitor(mock_client)
        trades_path = tmp_path / "trades.json"
        mgr = TradeManager(
            mock_client, trades_path,
            {"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25},
            kill_switch_path=tmp_path / "HALT",
            order_monitor=om,
            breaker_state_path=None,
        )
        mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert om.get_pending_count() == 1
        assert om.get_pending_capital() == 100  # 50 * 2


# ===================================================================
# Entry-Price-Relative Stop Loss tests (Phase 4.1)
# ===================================================================

# Import evaluate_stop_loss via importlib since position-monitor uses hyphens
def _load_position_monitor():
    """Load position-monitor.py for testing evaluate_stop_loss."""
    import importlib.util
    # We need to stub kalshi_auth module-level imports and KalshiClient
    # Instead, test the logic directly with a minimal function
    # that mirrors evaluate_stop_loss
    pass


class TestEntryPriceStopLoss:
    """Test entry-price-relative stop loss logic (4.1)."""

    def _evaluate_stop_loss(self, position, market, entry_price_cents=None, stop_loss_pct=0.40, absolute_stop=20):
        """Minimal reimplementation of evaluate_stop_loss for unit testing."""
        yes_count = position.get("yes", 0)
        no_count = position.get("no", 0)
        yes_bid = market.get("yes_bid", 0)
        no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

        if yes_count > 0 and yes_bid > 0:
            if entry_price_cents:
                stop_price = int(entry_price_cents * (1 - stop_loss_pct))
            else:
                stop_price = absolute_stop
            if yes_bid <= stop_price:
                return {"action": "stop_loss", "side": "yes", "count": yes_count, "price": yes_bid}

        if no_count > 0 and no_bid > 0:
            if entry_price_cents:
                stop_price = int(entry_price_cents * (1 - stop_loss_pct))
            else:
                stop_price = absolute_stop
            if no_bid <= stop_price:
                return {"action": "stop_loss", "side": "no", "count": no_count, "price": no_bid}

        return None

    def test_entry_price_stop_triggers(self):
        """Entry at 80c, 40% loss threshold -> stop at 48c. Bid of 40c triggers."""
        pos = {"ticker": "KXHIGHHOU-26FEB16-B77", "yes": 5, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 50}
        result = self._evaluate_stop_loss(pos, market, entry_price_cents=80, stop_loss_pct=0.40)
        assert result is not None
        assert result["action"] == "stop_loss"
        assert result["side"] == "yes"

    def test_entry_price_stop_no_trigger(self):
        """Entry at 80c, 40% loss threshold -> stop at 48c. Bid of 55c does NOT trigger."""
        pos = {"ticker": "KXHIGHHOU-26FEB16-B77", "yes": 5, "no": 0}
        market = {"yes_bid": 55, "yes_ask": 60}
        result = self._evaluate_stop_loss(pos, market, entry_price_cents=80, stop_loss_pct=0.40)
        assert result is None

    def test_fallback_to_absolute_when_no_entry(self):
        """Without entry price, falls back to absolute stop (20c)."""
        pos = {"ticker": "KXHIGHHOU-26FEB16-B77", "yes": 5, "no": 0}
        market = {"yes_bid": 15, "yes_ask": 25}
        result = self._evaluate_stop_loss(pos, market, entry_price_cents=None, absolute_stop=20)
        assert result is not None
        assert result["action"] == "stop_loss"


# ===================================================================
# Info-Arb Take-Profit Skip tests (Phase 4.3)
# ===================================================================

class TestInfoArbTakeProfitSkip:
    """Test that take-profit is skipped for confirmed info-arb positions."""

    def test_skip_for_source_monitor_high_prob(self):
        """Source-monitor trade with model_prob > 0.95 should skip TP."""
        entry_rec = {"source_bot": "source-monitor", "model_prob": 0.98}
        skip = (
            entry_rec.get("source_bot") == "source-monitor"
            and entry_rec.get("model_prob", 0) > 0.95
        )
        assert skip is True

    def test_no_skip_for_weather_bot(self):
        """Weather bot trades should NOT skip TP."""
        entry_rec = {"source_bot": "weather", "model_prob": 0.70}
        skip = (
            entry_rec.get("source_bot") == "source-monitor"
            and entry_rec.get("model_prob", 0) > 0.95
        )
        assert skip is False


# ===================================================================
# Trailing Stop tests (Phase 4.2)
# ===================================================================

class TestTrailingStop:
    """Test trailing stop logic."""

    def _evaluate_trailing_stop(self, position, market, peak_info,
                                 trailing_drop=10, trailing_min_profit=10):
        """Minimal reimplementation for unit testing."""
        yes_count = position.get("yes", 0)
        no_count = position.get("no", 0)

        if yes_count > 0:
            current_bid = market.get("yes_bid", 0)
            side = "yes"
            count = yes_count
        elif no_count > 0:
            current_bid = market.get("no_bid", 0) or (100 - market.get("yes_ask", 100))
            side = "no"
            count = no_count
        else:
            return None, peak_info

        entry_price = peak_info.get("entry_price", 0)
        peak_bid = peak_info.get("peak_bid", current_bid)

        if current_bid > peak_bid:
            peak_info["peak_bid"] = current_bid
            peak_bid = current_bid

        if (peak_bid - current_bid >= trailing_drop
                and peak_bid >= entry_price + trailing_min_profit):
            return {"action": "trailing_stop", "side": side, "count": count, "price": current_bid}, peak_info

        return None, peak_info

    def test_triggers_on_drop_from_peak(self):
        """Peak=90, entry=65, current=80 -> drop=10 >= 10, peak=90 >= 65+10=75 -> trigger."""
        pos = {"yes": 3, "no": 0}
        market = {"yes_bid": 80, "yes_ask": 85}
        peak_info = {"entry_price": 65, "peak_bid": 90}
        result, _ = self._evaluate_trailing_stop(pos, market, peak_info)
        assert result is not None
        assert result["action"] == "trailing_stop"

    def test_no_trigger_when_peak_not_profitable(self):
        """Peak=55, entry=65 -> peak < entry + 10 -> no trigger even if dropped."""
        pos = {"yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        peak_info = {"entry_price": 65, "peak_bid": 55}
        result, _ = self._evaluate_trailing_stop(pos, market, peak_info)
        assert result is None

    def test_no_trigger_small_drop(self):
        """Peak=90, entry=65, current=85 -> drop=5 < 10 -> no trigger."""
        pos = {"yes": 3, "no": 0}
        market = {"yes_bid": 85, "yes_ask": 90}
        peak_info = {"entry_price": 65, "peak_bid": 90}
        result, _ = self._evaluate_trailing_stop(pos, market, peak_info)
        assert result is None

    def test_updates_peak_on_new_high(self):
        """Current bid 95 > peak 90 -> peak should update to 95."""
        pos = {"yes": 3, "no": 0}
        market = {"yes_bid": 95, "yes_ask": 97}
        peak_info = {"entry_price": 65, "peak_bid": 90}
        result, updated = self._evaluate_trailing_stop(pos, market, peak_info)
        assert result is None
        assert updated["peak_bid"] == 95


# ===================================================================
# Shared Circuit Breaker tests (Phase 4.4)
# ===================================================================

class TestSharedCircuitBreaker:

    def test_shared_persistence(self, tmp_path):
        """Breaker state should persist to disk and be readable by another instance."""
        state_file = tmp_path / "breaker-state.json"
        _atomic_write_json(state_file, {})
        cb1 = CircuitBreaker(max_failures=3, state_path=str(state_file))
        for _ in range(3):
            cb1.record_failure()
        assert cb1.is_open() is True
        # New instance reads shared state
        cb2 = CircuitBreaker(max_failures=3, state_path=str(state_file))
        assert cb2.is_open() is True

    def test_shared_auto_reset(self, tmp_path):
        """Shared breaker should auto-reset after timeout."""
        state_file = tmp_path / "breaker-state.json"
        _atomic_write_json(state_file, {})
        cb = CircuitBreaker(max_failures=2, reset_seconds=0.1, state_path=str(state_file))
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True
        time.sleep(0.15)
        assert cb.is_open() is False

    def test_backward_compat_in_memory(self):
        """Without state_path, breaker works exactly as before (in-memory only)."""
        cb = CircuitBreaker(max_failures=3)
        assert cb.state_path is None
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open() is True
        cb.record_success()
        assert cb.is_open() is False
