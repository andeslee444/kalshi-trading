"""Tests for per-bot kill switches with auto-recovery."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kalshi_auth import (
    per_bot_halt_path,
    PER_BOT_HALT_PREFIX,
    BOT_SOURCE_MAP,
    TradeManager,
    HealthCheckMonitor,
    check_kill_switch,
    KILL_SWITCH_PATH,
    PROJECT_DIR,
)


# ===================================================================
# per_bot_halt_path() tests
# ===================================================================

class TestPerBotHaltPath:

    def test_returns_correct_path_format(self):
        p = per_bot_halt_path("weather")
        assert p.name == "HALT_bot_weather"
        assert p.parent.name == "data"

    def test_different_bots_get_different_paths(self):
        assert per_bot_halt_path("weather") != per_bot_halt_path("crypto")

    def test_prefix_used(self):
        p = per_bot_halt_path("crypto")
        assert PER_BOT_HALT_PREFIX in p.name


# ===================================================================
# TradeManager per-bot halt tests
# ===================================================================

def _make_manager(tmp_path, bot_name=None, config=None):
    """Helper to create a TradeManager with a mock client.

    Overrides _per_bot_halt_path to use tmp_path so tests don't touch real data/.
    """
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
        breaker_state_path=None,
        bot_name=bot_name,
    )
    # Override per-bot halt path to use tmp_path instead of real data/
    if bot_name:
        mgr._per_bot_halt_path = tmp_path / f"HALT_bot_{bot_name}"
    return mgr, mock_client, kill_path


class TestTradeManagerPerBotHalt:

    def test_blocks_trade_when_per_bot_halt_exists(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path, bot_name="weather")
        mgr._per_bot_halt_path.write_text("test halt")
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is None
        client.post.assert_not_called()

    def test_allows_trade_when_no_per_bot_halt(self, tmp_path):
        mgr, client, _ = _make_manager(tmp_path, bot_name="weather")
        assert not mgr._per_bot_halt_path.exists()
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is not None
        assert result["order_id"] == "test-123"

    def test_no_bot_name_ignores_per_bot_halt(self, tmp_path):
        """TradeManager(bot_name=None) should not check per-bot halt (backward compat)."""
        mgr, client, _ = _make_manager(tmp_path, bot_name=None)
        assert mgr._per_bot_halt_path is None
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is not None

    def test_global_halt_still_blocks_all(self, tmp_path):
        """Global HALT_TRADING blocks even when per-bot halt is absent."""
        mgr, client, kill_path = _make_manager(tmp_path, bot_name="weather")
        kill_path.write_text("global halt")
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is None
        client.post.assert_not_called()

    def test_global_halt_blocks_regardless_of_per_bot(self, tmp_path):
        """Global halt takes precedence — checked before per-bot."""
        mgr, client, kill_path = _make_manager(tmp_path, bot_name="crypto")
        kill_path.write_text("global halt")
        mgr._per_bot_halt_path.write_text("per-bot halt")
        result = mgr.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is None

    def test_per_bot_halt_only_affects_that_bot(self, tmp_path):
        """Weather halt should not block crypto."""
        (tmp_path / "w").mkdir()
        (tmp_path / "c").mkdir()
        mgr_weather, _, _ = _make_manager(tmp_path / "w", bot_name="weather")
        mgr_crypto, client_crypto, _ = _make_manager(tmp_path / "c", bot_name="crypto")

        # Halt weather (writes to tmp_path/w/HALT_bot_weather)
        mgr_weather._per_bot_halt_path.write_text("halt weather")

        # Crypto halt path is in tmp_path/c/ — different location, so not blocked
        result = mgr_crypto.place_order("TICK-1", "yes", 50, 2, "test reason")
        assert result is not None


# ===================================================================
# HealthCheckMonitor.check_per_bot_halts() tests
# ===================================================================

class TestCheckPerBotHalts:

    def _make_monitor(self, tmp_path, cooldown=0):
        """Create HealthCheckMonitor with tmp state path."""
        state_path = tmp_path / "health-state.json"
        monitor = HealthCheckMonitor(
            state_path=state_path,
            auto_halt=True,
            per_bot_halt_cooldown_seconds=cooldown,
        )
        return monitor

    def _set_source_errors(self, monitor, source, error_count):
        """Set a source's error count in the monitor state."""
        if source not in monitor._state["sources"]:
            monitor._state["sources"][source] = {
                "last_success": None, "last_error": None, "error_count": 0
            }
        monitor._state["sources"][source]["error_count"] = error_count

    def test_creates_halt_when_all_sources_failing(self, tmp_path):
        monitor = self._make_monitor(tmp_path)
        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 5)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["weather"] == "halted"
        assert (tmp_path / "HALT_bot_weather").exists()

    def test_no_halt_on_partial_failure(self, tmp_path):
        """Economics has 3 sources — one failing shouldn't trigger halt."""
        monitor = self._make_monitor(tmp_path)
        self._set_source_errors(monitor, "cleveland-fed", 5)
        self._set_source_errors(monitor, "gdpnow", 0)
        self._set_source_errors(monitor, "cme-fedwatch", 0)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["economics"] == "unchanged"
        assert not (tmp_path / "HALT_bot_economics").exists()

    def test_removes_halt_when_sources_recover(self, tmp_path):
        monitor = self._make_monitor(tmp_path)
        # Create existing halt file
        halt_file = tmp_path / "HALT_bot_weather"
        halt_file.write_text("previously halted")

        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 0)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["weather"] == "recovered"
        assert not halt_file.exists()

    def test_cooldown_prevents_rapid_halt(self, tmp_path):
        monitor = self._make_monitor(tmp_path, cooldown=600)
        monitor._halt_transitions["weather"] = time.time()

        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 5)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["weather"] == "unchanged"
        assert not (tmp_path / "HALT_bot_weather").exists()

    def test_cooldown_prevents_rapid_recovery(self, tmp_path):
        monitor = self._make_monitor(tmp_path, cooldown=600)
        halt_file = tmp_path / "HALT_bot_weather"
        halt_file.write_text("halted")
        monitor._halt_transitions["weather"] = time.time()

        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 0)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["weather"] == "unchanged"
        assert halt_file.exists()  # still halted due to cooldown

    def test_check_health_no_global_halt(self, tmp_path):
        """check_health() with auto_halt=True should NOT create global HALT_TRADING."""
        monitor = self._make_monitor(tmp_path)
        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 10)
        self._set_source_errors(monitor, "cleveland-fed", 10)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            issues = monitor.check_health()
        # Global kill switch should NOT be created
        assert not KILL_SWITCH_PATH.exists()

    def test_only_bots_with_all_failing_sources_halted(self, tmp_path):
        monitor = self._make_monitor(tmp_path)
        # Weather: all sources failing
        for src in BOT_SOURCE_MAP["weather"]:
            self._set_source_errors(monitor, src, 5)
        # Economics: only one of three failing
        self._set_source_errors(monitor, "cleveland-fed", 5)
        self._set_source_errors(monitor, "gdpnow", 0)
        self._set_source_errors(monitor, "cme-fedwatch", 0)

        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        assert status["weather"] == "halted"
        assert status["economics"] == "unchanged"

    def test_unknown_sources_treated_as_ok(self, tmp_path):
        """Sources not in health state default to error_count=0 (not failing)."""
        monitor = self._make_monitor(tmp_path)
        with patch("kalshi_auth.per_bot_halt_path", side_effect=lambda name: tmp_path / f"HALT_bot_{name}"):
            status = monitor.check_per_bot_halts()
        for bot_name in BOT_SOURCE_MAP:
            assert status[bot_name] == "unchanged"
