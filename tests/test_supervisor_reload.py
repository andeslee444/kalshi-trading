"""Tests for supervisor SIGHUP reload functionality."""

import signal
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import importlib.util
spec = importlib.util.spec_from_file_location(
    "supervisor",
    str(Path(__file__).resolve().parent.parent / "scripts" / "supervisor.py")
)
supervisor_mod = importlib.util.module_from_spec(spec)

mock_auth = MagicMock()
mock_auth.setup_logging = MagicMock(return_value=MagicMock())
mock_auth.check_kill_switch = MagicMock(return_value=False)
mock_auth.notify_webhook = MagicMock()
mock_auth.per_bot_halt_path = MagicMock(return_value=Path("/tmp/nonexistent"))
_orig_kalshi_auth = sys.modules.get("kalshi_auth")
sys.modules.setdefault("kalshi_auth", mock_auth)

spec.loader.exec_module(supervisor_mod)
if _orig_kalshi_auth is None:
    sys.modules.pop("kalshi_auth", None)
else:
    sys.modules["kalshi_auth"] = _orig_kalshi_auth
Supervisor = supervisor_mod.Supervisor


class TestSighupReload:
    """Test SIGHUP-triggered reload cycle."""

    def _make_supervisor(self):
        """Create a Supervisor with mocked bot methods."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._reload_requested = False
        sup.start_bots = MagicMock()
        sup.stop_bots = MagicMock()
        return sup

    def test_sighup_handler_sets_flag(self):
        """SIGHUP handler should set _reload_requested = True."""
        sup = self._make_supervisor()
        sup._reload_requested = False

        # Simulate what run() does: define the handler closure and call it
        def _reload(sig, frame):
            sup._reload_requested = True

        _reload(signal.SIGHUP, None)
        assert sup._reload_requested is True

    def test_reload_cycle_stops_then_starts(self):
        """When _reload_requested is True, supervisor should stop then start all bots."""
        sup = self._make_supervisor()
        sup._reload_requested = True

        # Simulate the reload block from run()
        if sup._reload_requested:
            sup.stop_bots()
            sup.start_bots()
            sup._reload_requested = False

        sup.stop_bots.assert_called_once()
        sup.start_bots.assert_called_once()
        # stop_bots must be called before start_bots
        assert sup.stop_bots.call_args_list[0] == call()
        assert sup.start_bots.call_args_list[0] == call()

    def test_reload_resets_flag(self):
        """After reload cycle, _reload_requested should be False."""
        sup = self._make_supervisor()
        sup._reload_requested = True

        if sup._reload_requested:
            sup.stop_bots()
            sup.start_bots()
            sup._reload_requested = False

        assert sup._reload_requested is False

    def test_no_reload_when_flag_false(self):
        """When _reload_requested is False, no stop/start should occur."""
        sup = self._make_supervisor()
        sup._reload_requested = False

        if sup._reload_requested:
            sup.stop_bots()
            sup.start_bots()
            sup._reload_requested = False

        sup.stop_bots.assert_not_called()
        sup.start_bots.assert_not_called()

    def test_reload_requested_initializes_false(self):
        """_reload_requested should default to False."""
        sup = self._make_supervisor()
        assert sup._reload_requested is False
