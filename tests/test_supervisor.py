"""Tests for supervisor heartbeat staleness detection and hardening."""

import json
import time
import datetime
import os
import signal
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
mock_auth.notify_webhook = MagicMock()
sys.modules.setdefault("kalshi_auth", mock_auth)

spec.loader.exec_module(supervisor)
BotProcess = supervisor.BotProcess
Supervisor = supervisor.Supervisor
HEARTBEAT_NAMES = supervisor.HEARTBEAT_NAMES
BOT_SCAN_INTERVALS = supervisor.BOT_SCAN_INTERVALS
HEARTBEAT_GRACE_PERIOD = supervisor.HEARTBEAT_GRACE_PERIOD
DAEMON_BOTS = supervisor.DAEMON_BOTS
SUPERVISOR_STATE_PATH = supervisor.SUPERVISOR_STATE_PATH


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
        for bot_name in DAEMON_BOTS:
            assert bot_name in HEARTBEAT_NAMES, f"{bot_name} missing from HEARTBEAT_NAMES"
            assert bot_name in BOT_SCAN_INTERVALS, f"{bot_name} missing from BOT_SCAN_INTERVALS"


class TestStatusHeartbeatLookup:

    def test_status_uses_heartbeat_names_mapping(self, capsys):
        """status() should look up heartbeat by HEARTBEAT_NAMES mapped name, not supervisor name."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0

        # Create a "positions" bot (maps to "position-monitor" in health)
        bot = BotProcess("positions", ["python3", "test.py"])
        bot.started_at = time.time() - 3600  # 1 hour ago
        sup.bots["positions"] = bot

        # Health data uses the mapped name "position-monitor"
        health = {
            "bots": {
                "position-monitor": {"last_heartbeat": _minutes_ago_iso(2)},
            }
        }

        with patch.object(bot, "is_running", return_value=True), \
             patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(type(sup), "_load_health", return_value=health):
            sup.status()

        output = capsys.readouterr().out
        # Should show the heartbeat timestamp (not "-")
        assert "-" not in output.split("positions")[1].split("\n")[0].rsplit(" ", 1)[-1] or \
            _minutes_ago_iso(2)[:16] in output

    def test_status_shows_stale_indicator(self, capsys):
        """status() should append STALE when heartbeat is stale."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0

        bot = BotProcess("weather", ["python3", "test.py"])
        bot.started_at = time.time() - 7200  # 2 hours ago
        sup.bots["weather"] = bot

        # Heartbeat from 100 min ago — stale (threshold = 3*30 = 90 min)
        health = {
            "bots": {
                "weather": {"last_heartbeat": _minutes_ago_iso(100)},
            }
        }

        with patch.object(bot, "is_running", return_value=True), \
             patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(type(sup), "_load_health", return_value=health):
            sup.status()

        output = capsys.readouterr().out
        assert "STALE" in output


class TestStatePersistence:

    def test_state_persistence_round_trip(self, tmp_path):
        """Save state, create new Supervisor, verify state restored for running bots."""
        state_file = tmp_path / "supervisor-state.json"
        started = time.time() - 600  # 10 min ago

        # Write state file
        state = {
            "weather": {"started_at": started, "restart_count": 3},
            "crypto": {"started_at": started, "restart_count": 1},
        }
        state_file.write_text(json.dumps(state))

        # Create supervisor with patched state path
        with patch.object(supervisor, "SUPERVISOR_STATE_PATH", state_file):
            sup = Supervisor.__new__(Supervisor)
            sup.bots = {
                name: BotProcess(name, cmd)
                for name, cmd in supervisor.BOT_COMMANDS.items()
            }
            sup._running = True
            sup._last_calibration_check = 0

            # Only "weather" is "running" — crypto is not
            with patch.object(sup.bots["weather"], "is_running", return_value=True), \
                 patch.object(sup.bots["crypto"], "is_running", return_value=False):
                sup._load_state()

        # Weather was running → state restored
        assert sup.bots["weather"].started_at == started
        assert sup.bots["weather"].restart_count == 3

        # Crypto was not running → state NOT restored
        assert sup.bots["crypto"].started_at is None
        assert sup.bots["crypto"].restart_count == 0

    def test_save_state_writes_json(self, tmp_path):
        """_save_state() writes correct JSON."""
        state_file = tmp_path / "supervisor-state.json"
        started = time.time() - 300

        with patch.object(supervisor, "SUPERVISOR_STATE_PATH", state_file):
            sup = Supervisor.__new__(Supervisor)
            sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
            sup.bots["weather"].started_at = started
            sup.bots["weather"].restart_count = 2
            sup._save_state()

        data = json.loads(state_file.read_text())
        assert data["weather"]["started_at"] == started
        assert data["weather"]["restart_count"] == 2

    def test_load_state_handles_missing_file(self):
        """_load_state() should not crash if state file doesn't exist."""
        with patch.object(supervisor, "SUPERVISOR_STATE_PATH", Path("/nonexistent/path.json")):
            sup = Supervisor.__new__(Supervisor)
            sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
            sup._running = True
            sup._last_calibration_check = 0
            sup._load_state()  # should not raise
        assert sup.bots["weather"].started_at is None

    def test_load_state_handles_corrupt_file(self, tmp_path):
        """_load_state() should not crash on corrupt JSON."""
        state_file = tmp_path / "supervisor-state.json"
        state_file.write_text("{bad json")

        with patch.object(supervisor, "SUPERVISOR_STATE_PATH", state_file):
            sup = Supervisor.__new__(Supervisor)
            sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
            sup._running = True
            sup._last_calibration_check = 0
            sup._load_state()  # should not raise
        assert sup.bots["weather"].started_at is None


class TestOrphanAdoption:

    def test_orphan_adoption_restores_state(self, tmp_path):
        """Running orphan should be adopted with started_at from state file."""
        state_file = tmp_path / "supervisor-state.json"
        started = time.time() - 1800  # 30 min ago
        state = {"weather": {"started_at": started, "restart_count": 5}}
        state_file.write_text(json.dumps(state))

        with patch.object(supervisor, "SUPERVISOR_STATE_PATH", state_file):
            sup = Supervisor.__new__(Supervisor)
            sup.bots = {
                name: BotProcess(name, cmd)
                for name, cmd in supervisor.BOT_COMMANDS.items()
            }
            sup._running = True
            sup._last_calibration_check = 0
            sup._load_state = Supervisor._load_state.__get__(sup, Supervisor)

            # Weather has a PID file and is running
            with patch.object(sup.bots["weather"], "_read_pid", return_value=9999), \
                 patch.object(sup.bots["weather"], "is_running", return_value=True):
                sup._load_state()
                sup._adopt_or_kill_orphans()

        # started_at should come from state file (loaded by _load_state)
        assert sup.bots["weather"].started_at == started
        assert sup.bots["weather"].restart_count == 5

    def test_orphan_dead_process_cleans_pid(self):
        """Dead orphan should have its PID file removed."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(sup.bots["weather"], "_read_pid", return_value=9999), \
             patch.object(sup.bots["weather"], "is_running", return_value=False), \
             patch.object(sup.bots["weather"], "_remove_pid") as mock_remove:
            sup._adopt_or_kill_orphans()

        mock_remove.assert_called_once()

    def test_orphan_no_pid_file_skipped(self):
        """Bot with no PID file should be skipped during orphan cleanup."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(sup.bots["weather"], "_read_pid", return_value=None), \
             patch.object(sup.bots["weather"], "_remove_pid") as mock_remove:
            sup._adopt_or_kill_orphans()

        mock_remove.assert_not_called()

    def test_orphan_adoption_fallback_to_current_time(self):
        """Orphan with no saved state should get started_at = now."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "test.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        before = time.time()
        with patch.object(sup.bots["weather"], "_read_pid", return_value=9999), \
             patch.object(sup.bots["weather"], "is_running", return_value=True):
            sup._adopt_or_kill_orphans()
        after = time.time()

        assert before <= sup.bots["weather"].started_at <= after


class TestProcessGroupShutdown:
    """Tests for process group shutdown (os.killpg).

    Patches os/time on the supervisor module object directly to avoid
    sys.modules issues when running the full test suite.
    """

    def test_stop_sends_to_process_group(self):
        """stop() should try os.killpg() before falling back to os.kill()."""
        bot = BotProcess("weather", ["python3", "test.py"])

        with patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(supervisor, "os", wraps=os) as mock_os, \
             patch.object(bot, "_remove_pid"):
            mock_os.killpg = MagicMock()
            mock_os.getpgid = MagicMock(return_value=12345)
            mock_os.kill = MagicMock(side_effect=ProcessLookupError)
            bot.stop()

        mock_os.getpgid.assert_called_with(12345)
        mock_os.killpg.assert_called_with(12345, signal.SIGTERM)

    def test_stop_falls_back_to_single_kill(self):
        """stop() should fall back to os.kill() if killpg fails."""
        bot = BotProcess("weather", ["python3", "test.py"])

        kill_calls = []

        def track_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError  # process gone after SIGTERM

        with patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(supervisor, "os", wraps=os) as mock_os, \
             patch.object(bot, "_remove_pid"):
            mock_os.killpg = MagicMock(side_effect=PermissionError)
            mock_os.getpgid = MagicMock(return_value=12345)
            mock_os.kill = MagicMock(side_effect=track_kill)
            bot.stop()

        # Should have fallen back to os.kill with SIGTERM
        assert (12345, signal.SIGTERM) in kill_calls

    def test_stop_sigkill_uses_process_group(self):
        """If SIGTERM doesn't work, SIGKILL should also use killpg."""
        bot = BotProcess("weather", ["python3", "test.py"])

        killpg_calls = []

        def track_killpg(pgid, sig):
            killpg_calls.append((pgid, sig))

        kill_zero_count = [0]

        def stubbed_kill(pid, sig):
            if sig == 0:
                kill_zero_count[0] += 1
                if kill_zero_count[0] > 20:
                    raise ProcessLookupError
                return  # process still alive
            raise ProcessLookupError

        with patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(supervisor, "os", wraps=os) as mock_os, \
             patch.object(supervisor, "time", wraps=time) as mock_time, \
             patch.object(bot, "_remove_pid"):
            mock_os.killpg = MagicMock(side_effect=track_killpg)
            mock_os.getpgid = MagicMock(return_value=12345)
            mock_os.kill = MagicMock(side_effect=stubbed_kill)
            mock_time.sleep = MagicMock()
            bot.stop()

        # Both SIGTERM and SIGKILL should go through killpg
        assert (12345, signal.SIGTERM) in killpg_calls
        assert (12345, signal.SIGKILL) in killpg_calls
