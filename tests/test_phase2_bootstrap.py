"""Phase 2 bootstrap regression tests."""

import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

from conftest import PROJECT_DIR


SRC_ROOT = PROJECT_DIR / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


APP_MODULES = [
    "kalshi.apps.weather_bot",
    "kalshi.apps.crypto_bot",
    "kalshi.apps.economics_bot",
    "kalshi.apps.entertainment_bot",
    "kalshi.apps.source_monitor",
    "kalshi.apps.position_monitor",
    "kalshi.apps.strategy_trader",
    "kalshi.apps.market_maker",
    "kalshi.apps.cross_platform_arb",
    "kalshi.apps.beatrelease_scanner",
    "kalshi.apps.demo_trader",
    "kalshi.apps.hdd_scraper",
]


def _generic_stub(name):
    module = types.ModuleType(name)
    module.__getattr__ = lambda attr: MagicMock(name=f"{name}.{attr}")
    return module


def _trade_files_stub():
    module = types.ModuleType("trade_files")
    module.ALL_TRADE_PATHS = []
    return module


def _make_fake_auth(project_dir):
    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.PROJECT_DIR = project_dir
    fake_auth.setup_unbuffered = MagicMock()
    fake_auth.setup_signal_handlers = MagicMock()
    fake_auth.setup_logging = MagicMock(side_effect=lambda *a, **kw: logging.getLogger("phase2-import"))
    fake_auth.KalshiClient = MagicMock(side_effect=AssertionError("KalshiClient should not be constructed at import"))
    fake_auth.TradeManager = MagicMock(side_effect=AssertionError("TradeManager should not be constructed at import"))
    fake_auth.HealthCheckMonitor = MagicMock(side_effect=AssertionError("HealthCheckMonitor should not be constructed at import"))
    fake_auth.OrderMonitor = MagicMock(side_effect=AssertionError("OrderMonitor should not be constructed at import"))
    fake_auth.ScanSummary = MagicMock()
    fake_auth.retry_request = MagicMock()
    fake_auth.fetch_parallel = MagicMock()
    fake_auth.load_trades = MagicMock(return_value=[])
    fake_auth.trim_trade_log = MagicMock()
    fake_auth._atomic_write_json = MagicMock()
    fake_auth.atomic_write_json = MagicMock()
    fake_auth.notify_whatsapp = MagicMock()
    fake_auth.notify_webhook = MagicMock()
    fake_auth.build_market_snapshot = MagicMock(return_value={})
    fake_auth.normalize_markets = MagicMock(side_effect=lambda markets: markets)
    fake_auth.is_shutdown_requested = MagicMock(return_value=False)
    fake_auth.round_half_up = MagicMock(side_effect=lambda x: round(x))
    fake_auth._local_today = MagicMock(return_value="2026-03-14")
    fake_auth.CITY_TIMEZONES = {}
    return fake_auth


def test_app_modules_are_import_safe():
    stubs = {
        "kalshi_auth": _make_fake_auth(PROJECT_DIR),
        "probability": _generic_stub("probability"),
        "ticker_utils": _generic_stub("ticker_utils"),
        "capital_allocator": _generic_stub("capital_allocator"),
        "forecast_verifier": _generic_stub("forecast_verifier"),
        "weather_data": _generic_stub("weather_data"),
        "particle_filter": _generic_stub("particle_filter"),
        "regime_detector": _generic_stub("regime_detector"),
        "crypto_models": _generic_stub("crypto_models"),
        "vol_forecaster": _generic_stub("vol_forecaster"),
        "strategy_engine": _generic_stub("strategy_engine"),
        "hdd_parser": _generic_stub("hdd_parser"),
        "polymarket_client": _generic_stub("polymarket_client"),
        "cpi_belief_filter": _generic_stub("cpi_belief_filter"),
        "scenario_engine": _generic_stub("scenario_engine"),
        "trade_files": _trade_files_stub(),
        "singleton_lock": _generic_stub("singleton_lock"),
        "macro_engine": _generic_stub("macro_engine"),
    }

    saved_modules = {name: sys.modules.get(name) for name in stubs}
    for name, stub in stubs.items():
        sys.modules[name] = stub

    try:
        for module_name in APP_MODULES:
            sys.modules.pop(module_name, None)
            module = importlib.import_module(module_name)
            assert callable(module.build_app)
            assert getattr(module, "_APP_CONTEXT", None) is None
    finally:
        for module_name in APP_MODULES:
            sys.modules.pop(module_name, None)
        for name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    fake_auth = stubs["kalshi_auth"]
    fake_auth.setup_unbuffered.assert_not_called()
    fake_auth.setup_signal_handlers.assert_not_called()
    fake_auth.setup_logging.assert_not_called()
