"""Focused tests for HDD scraper monitor-mode process control."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "kalshi" / "hdd-scraper.py"


def _load_module(tmp_path):
    mock_auth = types.ModuleType("kalshi_auth")
    mock_auth.KalshiClient = MagicMock(return_value=MagicMock())
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.setup_logging = MagicMock(return_value=MagicMock())
    mock_auth.PROJECT_DIR = tmp_path
    mock_auth._atomic_write_json = MagicMock()
    mock_auth.is_shutdown_requested = MagicMock(return_value=False)
    mock_auth.HealthCheckMonitor = MagicMock(return_value=MagicMock())

    mock_probability = types.ModuleType("probability")
    mock_probability.info_arb_probability = MagicMock(return_value=0.75)
    mock_probability.album_data_sigma = MagicMock(return_value=0.1)

    mock_parser = types.ModuleType("hdd_parser")
    mock_parser.sanity_query = MagicMock()
    mock_parser.fetch_latest_chart = MagicMock(return_value=None)
    mock_parser.fetch_recent_articles = MagicMock(return_value=[])
    mock_parser.fetch_articles_with_sales_keywords = MagicMock(return_value=[])
    mock_parser.parse_chart_data = MagicMock(return_value=[])
    mock_parser.clean_number = MagicMock(side_effect=lambda x: x)
    mock_parser.extract_sales_from_text = MagicMock(return_value=[])
    mock_parser.parse_album_threshold = MagicMock(return_value=None)
    mock_parser.SANITY_PROJECT = "test"
    mock_parser.SANITY_DATASET = "production"
    mock_parser.SANITY_BASE = "https://example.test"

    mock_singleton = types.ModuleType("singleton_lock")
    mock_singleton.acquire_process_singleton = MagicMock(return_value=True)

    with patch.dict(
        sys.modules,
        {
            "kalshi_auth": mock_auth,
            "probability": mock_probability,
            "hdd_parser": mock_parser,
            "singleton_lock": mock_singleton,
        },
    ):
        spec = importlib.util.spec_from_file_location("hdd_scraper_test_mod", str(MODULE_PATH))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.build_app(project_dir=tmp_path)

    return module, mock_auth, mock_singleton


def test_monitor_command_exits_before_loop_when_singleton_blocked(tmp_path):
    mod, _, _ = _load_module(tmp_path)

    with patch.object(mod, "acquire_process_singleton", return_value=False), \
         patch.object(mod, "monitor_loop") as mock_monitor:
        mod.main(["monitor"])

    mock_monitor.assert_not_called()


def test_monitor_loop_stops_after_shutdown_request(tmp_path):
    mod, _, _ = _load_module(tmp_path)

    with patch.object(mod, "run_full_scan") as mock_scan, \
         patch.object(mod, "is_shutdown_requested", side_effect=[True]):
        mod.monitor_loop(interval_minutes=15)

    mock_scan.assert_called_once()
    mod.health.record_bot_heartbeat.assert_called_once_with("hdd-monitor")


def test_monitor_command_acquires_monitor_singleton(tmp_path):
    mod, _, _ = _load_module(tmp_path)

    with patch.object(mod, "acquire_process_singleton", return_value=True) as mock_lock, \
         patch.object(mod, "monitor_loop") as mock_monitor:
        mod.main(["monitor", "--interval", "7"])

    mock_lock.assert_called_once_with(
        "hdd-monitor",
        mod.PROJECT_DIR,
        mod.log,
        display_name="hdd-monitor",
    )
    mock_monitor.assert_called_once_with(7)
