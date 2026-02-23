"""Tests for supervisor heartbeat staleness detection."""

import time
import datetime
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "supervisor",
    str(Path(__file__).resolve().parent.parent / "scripts" / "supervisor.py")
)
supervisor = importlib.util.module_from_spec(spec)

# Stub setup_logging and check_kill_switch before exec
mock_auth = MagicMock()
mock_auth.setup_logging = MagicMock(return_value=MagicMock())
mock_auth.check_kill_switch = MagicMock(return_value=False)
sys.modules.setdefault("kalshi_auth", mock_auth)

spec.loader.exec_module(supervisor)
BotProcess = supervisor.BotProcess
HEARTBEAT_NAMES = supervisor.HEARTBEAT_NAMES
BOT_SCAN_INTERVALS = supervisor.BOT_SCAN_INTERVALS
HEARTBEAT_GRACE_PERIOD = supervisor.HEARTBEAT_GRACE_PERIOD


def _now_iso():
    return datetime.datetime.now().isoformat()


def _minutes_ago_iso(minutes):
    dt = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
    return dt.isoformat()


class TestHeartbeatStaleness:

    def _make_bot(self, name="weather", started_minutes_ago=10):
        bot = BotProcess(name, ["python3", "test.py"])
        bot.started_at = time.time() - (started_minutes_ago * 60)
        return bot

    def test_fresh_heartbeat_not_stale(self):
        """A heartbeat from 5 minutes ago should not be stale (weather interval=30min)."""
        bot = self._make_bot("weather", started_minutes_ago=60)
        health = {"bots": {"weather": {"last_heartbeat": _minutes_ago_iso(5)}}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is False

    def test_old_heartbeat_triggers_stale(self):
        """A heartbeat from 100 minutes ago should be stale (weather: 3x30=90 threshold)."""
        bot = self._make_bot("weather", started_minutes_ago=120)
        health = {"bots": {"weather": {"last_heartbeat": _minutes_ago_iso(100)}}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is True
        assert age > 90

    def test_grace_period_prevents_false_positive(self):
        """Within 5-minute grace period, heartbeat should not be checked."""
        bot = self._make_bot("weather", started_minutes_ago=2)  # started 2 min ago
        health = {"bots": {}}  # no heartbeat yet
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is False

    def test_economics_6h_interval(self):
        """Economics bot (360min interval) shouldn't be stale at 6h, but stale at 18h+."""
        bot = self._make_bot("economics", started_minutes_ago=1200)

        # 6 hours ago = 360 min, threshold is 3*360 = 1080 min
        health = {"bots": {"economics": {"last_heartbeat": _minutes_ago_iso(360)}}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is False

        # 20 hours ago = 1200 min, > 1080 threshold
        health = {"bots": {"economics": {"last_heartbeat": _minutes_ago_iso(1200)}}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is True

    def test_unknown_bot_not_checked(self):
        """Bots not in HEARTBEAT_NAMES mapping should not be checked."""
        bot = self._make_bot("hdd", started_minutes_ago=60)
        health = {"bots": {}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is False

    def test_no_heartbeat_after_2x_interval(self):
        """Bot running > 2x interval with no heartbeat recorded should be stale."""
        bot = self._make_bot("crypto", started_minutes_ago=15)  # 2x interval = 10 min
        health = {"bots": {}}  # no heartbeat for crypto
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is True

    def test_no_heartbeat_within_2x_interval(self):
        """Bot running < 2x interval with no heartbeat should not be stale."""
        bot = self._make_bot("weather", started_minutes_ago=30)  # 2x = 60 min
        health = {"bots": {}}
        is_stale, age = bot.is_heartbeat_stale(health)
        assert is_stale is False

    def test_heartbeat_names_mapping(self):
        """All daemon bots should have heartbeat mappings."""
        daemon_bots = {"weather", "entertainment", "crypto", "economics", "positions", "monitor"}
        for bot_name in daemon_bots:
            assert bot_name in HEARTBEAT_NAMES, f"{bot_name} missing from HEARTBEAT_NAMES"
            assert bot_name in BOT_SCAN_INTERVALS, f"{bot_name} missing from BOT_SCAN_INTERVALS"
