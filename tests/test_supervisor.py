"""Tests for supervisor heartbeat staleness detection and hardening."""

import json
import time
import datetime
import os
import signal
import subprocess
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

    def test_strategy_is_daemon_and_hdd_is_disabled_oneshot(self):
        """Strategy should be supervised as a daemon; HDD should remain opt-in one-shot."""
        assert "strategy" in supervisor.DAEMON_BOTS
        assert "hdd-monitor" in supervisor.DAEMON_BOTS
        assert "hdd" not in supervisor.DAEMON_BOTS
        assert "hdd" in supervisor.ONESHOT_BOTS
        assert "hdd" in supervisor._ALWAYS_DISABLED


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

    def test_status_handles_permission_denied_pid_probe_as_running(self, capsys):
        """Permission-denied PID probes should not show a false stopped status."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0

        bot = BotProcess("weather", ["python3", "test.py"])
        sup.bots["weather"] = bot

        health = {"bots": {"weather": {"last_heartbeat": _minutes_ago_iso(1)}}}

        with patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(supervisor.os, "kill", side_effect=PermissionError), \
             patch.object(type(sup), "_load_health", return_value=health):
            sup.status()

        output = capsys.readouterr().out
        assert "weather" in output
        assert "running" in output
        assert "12345" in output

    def test_status_prints_weather_actual_source_summary(self, capsys, tmp_path):
        """status() should include recent weather actual-source mix when available."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0

        bot = BotProcess("weather", ["python3", "test.py"])
        sup.bots["weather"] = bot

        health = {"bots": {"weather": {"last_heartbeat": _minutes_ago_iso(1)}}}
        verification_path = tmp_path / "weather-verification.json"
        verification_path.write_text(json.dumps({
            "verified": [
                {"city": "MIA", "date": datetime.date.today().isoformat(), "actual_source": "nws_cli"},
                {"city": "NY", "date": datetime.date.today().isoformat(), "actual_source": "iem_fallback"},
            ]
        }))

        with patch.object(bot, "is_running", return_value=True), \
             patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(type(sup), "_load_health", return_value=health), \
             patch.object(supervisor, "WEATHER_VERIFICATION_PATH", verification_path):
            sup.status()

        output = capsys.readouterr().out
        assert "Weather actuals (7d): total=2" in output
        assert "nws_cli=1 (50.0%)" in output
        assert "iem_fallback=1 (50.0%)" in output

    def test_status_uses_pgrep_fallback_when_pid_file_missing(self, capsys):
        """status() should show running when pgrep finds a live worker but PID file is missing."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0

        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        sup.bots["weather"] = bot
        health = {"bots": {"weather": {"last_heartbeat": _minutes_ago_iso(1)}}}

        with patch.object(bot, "_read_pid", return_value=None), \
             patch.object(type(sup), "_load_health", return_value=health), \
             patch.object(supervisor, "_find_bot_processes", return_value=[14595]):
            sup.status()

        output = capsys.readouterr().out
        assert "weather" in output
        assert "running" in output
        assert "14595" in output


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


class TestOrphanCleanup:

    def test_kills_orphan_found_by_pgrep(self):
        """Orphan processes found by pattern should be killed."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234, 5678]), \
             patch.object(supervisor.os, "kill") as mock_kill, \
             patch.object(supervisor.time, "sleep"):
            sup._adopt_or_kill_orphans()

        # Should have tried to kill both orphans
        kill_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGTERM]
        assert len(kill_calls) == 2

    def test_cleans_pid_files(self):
        """All PID files should be cleaned up during orphan cleanup."""
        sup = Supervisor.__new__(Supervisor)
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        sup.bots = {"weather": bot}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[]), \
             patch.object(bot, "_remove_pid") as mock_remove:
            sup._adopt_or_kill_orphans()

        mock_remove.assert_called_once()

    def test_handles_already_dead_process(self):
        """Should not crash if orphan dies between pgrep and kill."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234]), \
             patch.object(supervisor.os, "kill", side_effect=ProcessLookupError), \
             patch.object(supervisor.time, "sleep"):
            sup._adopt_or_kill_orphans()  # should not raise

    def test_sigkill_after_sigterm_timeout(self):
        """Should SIGKILL processes that survive SIGTERM."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        kill_calls = []

        def track_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                return  # process still alive

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234]), \
             patch.object(supervisor.os, "kill", side_effect=track_kill), \
             patch.object(supervisor.time, "sleep"):
            sup._adopt_or_kill_orphans()

        assert (1234, signal.SIGTERM) in kill_calls
        assert (1234, signal.SIGKILL) in kill_calls


class TestIsRunning:

    def test_is_running_returns_true_on_permission_denied(self):
        bot = BotProcess("weather", ["python3", "test.py"])
        with patch.object(bot, "_read_pid", return_value=12345), \
             patch.object(supervisor.os, "kill", side_effect=PermissionError), \
             patch.object(bot, "_remove_pid") as mock_remove:
            assert bot.is_running() is True
        mock_remove.assert_not_called()

    def test_is_running_clears_exited_managed_process(self, tmp_path):
        """Exited child processes should not keep the bot marked as running."""
        bot = BotProcess("hdd", ["python3", "test.py"])
        bot.pid_file = tmp_path / "hdd.pid"
        bot.pid_file.write_text("12345")
        bot.process = MagicMock(pid=12345)
        bot.process.poll.return_value = 0

        assert bot.is_running() is False
        assert bot.process is None
        assert not bot.pid_file.exists()


class TestRestartAlerts:

    def test_restart_warning_fires_at_threshold(self):
        bot = BotProcess("weather", ["python3", "test.py"])
        now = 1_700_000_000
        bot.recent_crashes = [now - 100, now - 50]

        with patch.object(bot, "start", return_value=True), \
             patch.object(supervisor.time, "time", return_value=now), \
             patch.object(supervisor, "notify_webhook") as mock_notify:
            restarted = bot.check_and_restart()

        assert restarted is True
        mock_notify.assert_called_once()
        assert "restarted 3x" in mock_notify.call_args.args[0]

    def test_restart_warning_rate_limited(self):
        bot = BotProcess("weather", ["python3", "test.py"])
        now = 1_700_000_000
        bot.recent_crashes = [now - 100, now - 50]
        bot.last_restart_alert_at = now - 60

        with patch.object(bot, "start", return_value=True), \
             patch.object(supervisor.time, "time", return_value=now), \
             patch.object(supervisor, "notify_webhook") as mock_notify:
            restarted = bot.check_and_restart()

        assert restarted is True
        mock_notify.assert_not_called()


class TestPidFileSafety:

    def test_remove_pid_if_matches_preserves_newer_pid(self, tmp_path):
        bot = BotProcess("weather", ["python3", "test.py"])
        bot.pid_file = tmp_path / "weather.pid"
        bot.pid_file.write_text("22222")

        bot._remove_pid_if_matches(12345)
        assert bot.pid_file.exists()

        bot._remove_pid_if_matches(22222)
        assert not bot.pid_file.exists()


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
                if kill_zero_count[0] > 60:
                    raise ProcessLookupError
                return  # process still alive
            if sig == signal.SIGUSR1:
                return  # accept pre-shutdown signal
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


class TestFindBotProcesses:

    def test_finds_matching_pids(self):
        """Should return PIDs from pgrep output."""
        output = (
            "1234 /opt/homebrew/Python src/kalshi/weather-bot.py\n"
            "5678 /opt/homebrew/Python -u src/kalshi/weather-bot.py\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234, 5678]

    def test_excludes_own_pid(self):
        """Should exclude the supervisor's own PID from results."""
        output = (
            "1234 /opt/homebrew/Python src/kalshi/weather-bot.py\n"
            "9999 /opt/homebrew/Python src/kalshi/weather-bot.py\n"
            "5678 /opt/homebrew/Python src/kalshi/weather-bot.py\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234, 5678]

    def test_returns_empty_when_no_matches(self):
        """pgrep exits non-zero when no matches — should return empty list."""
        with patch.object(supervisor.subprocess, "check_output",
                          side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == []

    def test_handles_blank_lines(self):
        """Should handle trailing newlines and blank lines."""
        output = "1234 /opt/homebrew/Python src/kalshi/weather-bot.py\n\n"
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234]

    def test_uses_script_path_pattern_for_python_bots(self):
        """Python bot discovery should match by script path, not literal python3."""
        with patch.object(supervisor.subprocess, "check_output", return_value="") as mock_check_output, \
             patch.object(supervisor.os, "getpid", return_value=9999):
            supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])

        mock_check_output.assert_called_once_with(
            ["pgrep", "-fl", "src/kalshi/weather-bot.py"],
            text=True,
        )

    def test_ignores_non_python_commands_that_reference_script(self):
        """Editors or shells mentioning the script should not be treated as bot workers."""
        output = (
            "1234 vim src/kalshi/weather-bot.py\n"
            "5678 /opt/homebrew/Python src/kalshi/weather-bot.py\n"
            "6789 zsh -lc tail -f src/kalshi/weather-bot.py\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [5678]

    def test_ignores_malformed_pgrep_lines(self):
        """Malformed command lines with embedded newlines should not crash parsing."""
        output = (
            "19696 /bin/zsh -c cd /repo\n"
            "nohup python3 -u src/kalshi/weather-bot.py >> /tmp/out.log 2>&1 &\n"
            "19697 /opt/homebrew/Python -u src/kalshi/weather-bot.py\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [19697]

    def test_matches_python_script_with_required_args(self):
        """Commands sharing a script path should still be distinguishable by required args."""
        output = (
            "1234 /opt/homebrew/Python src/kalshi/hdd-scraper.py\n"
            "5678 /opt/homebrew/Python src/kalshi/hdd-scraper.py monitor\n"
            "6789 /opt/homebrew/Python src/kalshi/hdd-scraper.py monitor --interval 5\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/hdd-scraper.py", "monitor"])
        assert pids == [5678, 6789]

    def test_base_python_script_does_not_match_invocations_with_extra_args(self):
        """A bare script command should not absorb monitor-mode invocations of the same file."""
        output = (
            "1234 /opt/homebrew/Python src/kalshi/hdd-scraper.py\n"
            "5678 /opt/homebrew/Python src/kalshi/hdd-scraper.py monitor\n"
        )
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/hdd-scraper.py"])
        assert pids == [1234]

    def test_duplicate_block_detection_uses_mapped_log_name(self, tmp_path):
        """Duplicate-block detection should read logger-name log files too."""
        log_path = tmp_path / "source-monitor.log"
        log_path.write_text("2026-03-13 [source-monitor] WARNING: Duplicate source-monitor launch blocked; exiting.\n")

        with patch.object(supervisor, "LOG_DIR", tmp_path):
            assert supervisor._log_contains_duplicate_block("monitor") is True

    def test_duplicate_block_detection_uses_hdd_scraper_log_name(self, tmp_path):
        """hdd-monitor duplicate checks should read the hdd-scraper log file."""
        log_path = tmp_path / "hdd-scraper.log"
        log_path.write_text("2026-03-13 [hdd-scraper] WARNING: Duplicate HDD monitor launch blocked; exiting.\n")

        with patch.object(supervisor, "LOG_DIR", tmp_path):
            assert supervisor._log_contains_duplicate_block("hdd-monitor") is True


class TestSingletonBlockedStart:

    def test_handle_singleton_blocked_start_adopts_existing_pid(self, tmp_path):
        """A singleton-blocked launch should adopt the already-running worker."""
        bot = BotProcess("monitor", ["python3", "src/kalshi/source-monitor.py"])
        bot.pid_file = tmp_path / "monitor.pid"
        bot.process = MagicMock(pid=99999)

        log_path = tmp_path / "source-monitor.log"
        log_path.write_text("2026-03-13 [source-monitor] WARNING: Duplicate source-monitor launch blocked; exiting.\n")

        with patch.object(supervisor, "LOG_DIR", tmp_path), \
             patch.object(supervisor, "_find_bot_processes", return_value=[14595]):
            handled = bot.handle_singleton_blocked_start()

        assert handled is True
        assert bot._read_pid() == 14595
        assert bot.process is None


class TestSingletonLock:

    def test_acquire_lock_succeeds(self, tmp_path):
        """First supervisor should acquire lock successfully."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            sup._acquire_lock()
        assert sup._lock_file is not None
        sup._release_lock()

    def test_acquire_lock_fails_when_held(self, tmp_path):
        """Second supervisor should fail to acquire lock."""
        import fcntl
        lock_path = tmp_path / "supervisor.lock"

        # Hold the lock from "another process"
        held = open(lock_path, "w")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            with pytest.raises(SystemExit):
                sup._acquire_lock()

        held.close()

    def test_release_lock(self, tmp_path):
        """Lock should be released cleanly."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            sup._acquire_lock()
            sup._release_lock()
        assert sup._lock_file is None


class TestStartKillsExisting:

    def test_start_kills_existing_process_before_launch(self):
        """start() should kill any existing process matching this bot before launching."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])

        popen_mock = MagicMock()
        popen_mock.pid = 9999

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234]) as mock_find, \
             patch.object(supervisor.os, "kill") as mock_kill, \
             patch.object(supervisor.time, "sleep"), \
             patch.object(supervisor.subprocess, "Popen", return_value=popen_mock), \
             patch.object(bot, "_write_pid"), \
             patch("builtins.open", MagicMock()):
            bot.start()

        # Should have killed the existing process
        mock_kill.assert_any_call(1234, signal.SIGTERM)

    def test_start_proceeds_when_no_existing_process(self):
        """start() should work normally when no existing process found."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])

        popen_mock = MagicMock()
        popen_mock.pid = 9999

        with patch.object(supervisor, "_find_bot_processes", return_value=[]), \
             patch.object(supervisor.subprocess, "Popen", return_value=popen_mock), \
             patch.object(bot, "_write_pid"), \
             patch("builtins.open", MagicMock()):
            result = bot.start()

        assert result is True


class TestCheckAndRestartDedup:

    def test_skips_restart_if_process_already_running_by_pattern(self):
        """If pgrep finds the bot running, don't restart even if PID file is stale."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        bot.recent_crashes = []

        with patch.object(bot, "is_running", return_value=False), \
             patch.object(supervisor, "_find_bot_processes", return_value=[1234]), \
             patch.object(bot, "start") as mock_start:
            result = bot.check_and_restart()

        mock_start.assert_not_called()
        assert result is False

    def test_restarts_when_no_process_found_by_pattern(self):
        """If pgrep finds nothing, proceed with restart."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        bot.recent_crashes = []

        with patch.object(bot, "is_running", return_value=False), \
             patch.object(supervisor, "_find_bot_processes", return_value=[]), \
             patch.object(bot, "start", return_value=True) as mock_start:
            result = bot.check_and_restart()

        mock_start.assert_called_once()
        assert result is True

    def test_adopts_single_running_pid_when_pid_file_missing(self):
        """If a single worker exists, adopt its PID instead of restarting."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        bot.recent_crashes = []

        with patch.object(bot, "is_running", return_value=False), \
             patch.object(bot, "adopt_running_pid", return_value=14595) as mock_adopt, \
             patch.object(bot, "start") as mock_start:
            result = bot.check_and_restart()

        mock_adopt.assert_called_once()
        mock_start.assert_not_called()
        assert result is False


class TestOneShotStartup:

    def test_start_bots_treats_ok_oneshot_completion_as_success(self):
        """One-shot bots that finish successfully should not trigger startup-failure alerts."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"hdd": BotProcess("hdd", ["python3", "src/kalshi/hdd-scraper.py"])}
        sup._running = True
        sup._last_calibration_check = 0
        bot = sup.bots["hdd"]

        with patch.object(bot, "start", return_value=True), \
             patch.object(bot, "is_running", return_value=False), \
             patch.object(bot, "check_oneshot_completion", return_value="ok"), \
             patch.object(bot, "handle_singleton_blocked_start", return_value=False), \
             patch.object(supervisor.time, "sleep"), \
             patch.object(supervisor, "notify_webhook") as mock_notify, \
             patch.object(sup, "_save_state"):
            sup.start_bots(["hdd"])

        mock_notify.assert_not_called()
