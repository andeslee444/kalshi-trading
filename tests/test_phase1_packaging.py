"""Phase 1 packaging and wrapper regression tests."""

import importlib
import runpy
import sys
import tomllib
import types
from pathlib import Path
from unittest.mock import MagicMock

from conftest import PROJECT_DIR, make_fake_auth
from source_paths import resolve_bot_source_path


SRC_ROOT = PROJECT_DIR / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_stubs(stubs):
    saved = {}
    for name, mod in stubs.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _restore_stubs(saved, names):
    for name in names:
        original = saved.get(name)
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def test_legacy_flat_import_aliases_package_module():
    flat_src_dir = str(PROJECT_DIR / "src" / "kalshi")
    removed = False
    if flat_src_dir in sys.path:
        sys.path.remove(flat_src_dir)
        removed = True
    sys.modules.pop("bot_registry", None)
    sys.modules.pop("kalshi.bot_registry", None)
    try:
        pkg_module = importlib.import_module("kalshi.bot_registry")
        flat_module = importlib.import_module("bot_registry")
        assert flat_module is pkg_module
    finally:
        sys.modules.pop("bot_registry", None)
        sys.modules.pop("kalshi.bot_registry", None)
        if removed:
            sys.path.insert(0, flat_src_dir)


def test_subpackage_aliases_resolve_extracted_phase5_modules():
    flat_src_dir = str(PROJECT_DIR / "src" / "kalshi")
    removed = False
    if flat_src_dir in sys.path:
        sys.path.remove(flat_src_dir)
        removed = True
    for name in (
        "domain.crypto",
        "domain.crypto.models",
        "domain.economics",
        "domain.economics.models",
        "domain.entertainment",
        "domain.entertainment.models",
        "domain.longshot",
        "domain.longshot.models",
        "domain",
        "domain.shared",
        "domain.shared.stats",
        "domain.shared.sizing",
        "domain.weather",
        "domain.weather.models",
        "execution",
        "execution.order_monitor",
        "execution.trade_manager",
        "infra",
        "infra.kalshi_client",
        "ops",
        "ops.health_monitor",
        "ops.logging",
        "ops.notifications",
        "risk",
        "risk.kill_switch",
        "kalshi.domain.crypto",
        "kalshi.domain.crypto.models",
        "kalshi.domain.economics",
        "kalshi.domain.economics.models",
        "kalshi.domain.entertainment",
        "kalshi.domain.entertainment.models",
        "kalshi.domain.longshot",
        "kalshi.domain.longshot.models",
        "kalshi.domain",
        "kalshi.domain.shared",
        "kalshi.domain.shared.stats",
        "kalshi.domain.shared.sizing",
        "kalshi.domain.weather",
        "kalshi.domain.weather.models",
        "kalshi.execution",
        "kalshi.execution.order_monitor",
        "kalshi.execution.trade_manager",
        "kalshi.infra",
        "kalshi.infra.kalshi_client",
        "kalshi.ops",
        "kalshi.ops.health_monitor",
        "kalshi.ops.logging",
        "kalshi.ops.notifications",
        "kalshi.risk",
        "kalshi.risk.kill_switch",
    ):
        sys.modules.pop(name, None)
    try:
        pkg_crypto = importlib.import_module("kalshi.domain.crypto.models")
        flat_crypto = importlib.import_module("domain.crypto.models")
        assert Path(flat_crypto.__file__).resolve() == Path(pkg_crypto.__file__).resolve()

        pkg_economics = importlib.import_module("kalshi.domain.economics.models")
        flat_economics = importlib.import_module("domain.economics.models")
        assert Path(flat_economics.__file__).resolve() == Path(pkg_economics.__file__).resolve()

        pkg_entertainment = importlib.import_module("kalshi.domain.entertainment.models")
        flat_entertainment = importlib.import_module("domain.entertainment.models")
        assert Path(flat_entertainment.__file__).resolve() == Path(pkg_entertainment.__file__).resolve()

        pkg_longshot = importlib.import_module("kalshi.domain.longshot.models")
        flat_longshot = importlib.import_module("domain.longshot.models")
        assert Path(flat_longshot.__file__).resolve() == Path(pkg_longshot.__file__).resolve()

        pkg_stats = importlib.import_module("kalshi.domain.shared.stats")
        flat_stats = importlib.import_module("domain.shared.stats")
        assert Path(flat_stats.__file__).resolve() == Path(pkg_stats.__file__).resolve()

        pkg_domain = importlib.import_module("kalshi.domain.shared.sizing")
        flat_domain = importlib.import_module("domain.shared.sizing")
        assert Path(flat_domain.__file__).resolve() == Path(pkg_domain.__file__).resolve()

        pkg_weather = importlib.import_module("kalshi.domain.weather.models")
        flat_weather = importlib.import_module("domain.weather.models")
        assert Path(flat_weather.__file__).resolve() == Path(pkg_weather.__file__).resolve()

        pkg_execution = importlib.import_module("kalshi.execution.order_monitor")
        flat_execution = importlib.import_module("execution.order_monitor")
        assert Path(flat_execution.__file__).resolve() == Path(pkg_execution.__file__).resolve()

        pkg_trade_manager = importlib.import_module("kalshi.execution.trade_manager")
        flat_trade_manager = importlib.import_module("execution.trade_manager")
        assert Path(flat_trade_manager.__file__).resolve() == Path(pkg_trade_manager.__file__).resolve()

        pkg_infra = importlib.import_module("kalshi.infra.kalshi_client")
        flat_infra = importlib.import_module("infra.kalshi_client")
        assert Path(flat_infra.__file__).resolve() == Path(pkg_infra.__file__).resolve()

        pkg_ops = importlib.import_module("kalshi.ops.logging")
        flat_ops = importlib.import_module("ops.logging")
        assert Path(flat_ops.__file__).resolve() == Path(pkg_ops.__file__).resolve()

        pkg_health = importlib.import_module("kalshi.ops.health_monitor")
        flat_health = importlib.import_module("ops.health_monitor")
        assert Path(flat_health.__file__).resolve() == Path(pkg_health.__file__).resolve()

        pkg_notifications = importlib.import_module("kalshi.ops.notifications")
        flat_notifications = importlib.import_module("ops.notifications")
        assert Path(flat_notifications.__file__).resolve() == Path(pkg_notifications.__file__).resolve()

        pkg_risk = importlib.import_module("kalshi.risk.kill_switch")
        flat_risk = importlib.import_module("risk.kill_switch")
        assert Path(flat_risk.__file__).resolve() == Path(pkg_risk.__file__).resolve()
    finally:
        for name in (
            "domain.crypto",
            "domain.crypto.models",
            "domain.economics",
            "domain.economics.models",
            "domain.entertainment",
            "domain.entertainment.models",
            "domain.longshot",
            "domain.longshot.models",
            "domain",
            "domain.shared",
            "domain.shared.stats",
            "domain.shared.sizing",
            "domain.weather",
            "domain.weather.models",
            "execution",
            "execution.order_monitor",
            "execution.trade_manager",
            "infra",
            "infra.kalshi_client",
            "ops",
            "ops.health_monitor",
            "ops.logging",
            "ops.notifications",
            "risk",
            "risk.kill_switch",
            "kalshi.domain.crypto",
            "kalshi.domain.crypto.models",
            "kalshi.domain.economics",
            "kalshi.domain.economics.models",
            "kalshi.domain.entertainment",
            "kalshi.domain.entertainment.models",
            "kalshi.domain.longshot",
            "kalshi.domain.longshot.models",
            "kalshi.domain",
            "kalshi.domain.shared",
            "kalshi.domain.shared.stats",
            "kalshi.domain.shared.sizing",
            "kalshi.domain.weather",
            "kalshi.domain.weather.models",
            "kalshi.execution",
            "kalshi.execution.order_monitor",
            "kalshi.execution.trade_manager",
            "kalshi.infra",
            "kalshi.infra.kalshi_client",
            "kalshi.ops",
            "kalshi.ops.health_monitor",
            "kalshi.ops.logging",
            "kalshi.ops.notifications",
            "kalshi.risk",
            "kalshi.risk.kill_switch",
        ):
            sys.modules.pop(name, None)
        if removed:
            sys.path.insert(0, flat_src_dir)


def test_weather_app_imports_via_package_name_without_hyphen_loader():
    fake_auth = make_fake_auth(PROJECT_DIR=PROJECT_DIR, normalize_markets=lambda markets: markets)
    fake_probability = _stub_module(
        "probability",
        weather_probability=lambda *a, **kw: 0.5,
        weather_sigma=lambda *a, **kw: 3.0,
        weather_sigma_hourly=lambda *a, **kw: 3.0,
        ensemble_weather_probability=lambda *a, **kw: 0.5,
        ensemble_spread_sigma_multiplier=lambda *a, **kw: 1.0,
        ensemble_weather_probability_v2=lambda *a, **kw: 0.5,
        empirical_ensemble_probability=lambda *a, **kw: 0.5,
        half_kelly=lambda *a, **kw: (0, 0),
        quarter_kelly=lambda *a, **kw: (0, 0),
        high_conviction_kelly=lambda *a, **kw: (0, 0),
        compute_limit_price=lambda *a, **kw: 50,
        kalshi_fee_cents=lambda *a, **kw: 0,
        is_market_liquid=lambda *a, **kw: True,
        _load_calibration=lambda *a, **kw: {},
    )
    fake_ticker_utils = _stub_module("ticker_utils", parse_weather_ticker=lambda *a, **kw: None)
    fake_allocator = _stub_module("capital_allocator", PortfolioAllocator=lambda *a, **kw: MagicMock())
    fake_verifier = _stub_module(
        "forecast_verifier",
        ForecastVerifier=lambda *a, **kw: MagicMock(),
        NWSCrossCheckVerifier=lambda *a, **kw: MagicMock(),
        DEFAULT_STATION_MAP={},
    )
    fake_weather_data = _stub_module(
        "weather_data",
        EnsembleCollector=lambda *a, **kw: MagicMock(),
        HRRRFetcher=lambda *a, **kw: MagicMock(),
        NAMFetcher=lambda *a, **kw: MagicMock(),
        PreviousRunsFetcher=lambda *a, **kw: MagicMock(),
        OrderBookDepth=lambda *a, **kw: MagicMock(),
        next_model_run=lambda *a, **kw: ("gfs", 60),
        latest_available_model_run=lambda *a, **kw: None,
        canonical_model_name=lambda name: name,
        open_meteo_model_name=lambda name: name,
        STATION_MAP={},
        NWSForecastFetcher=lambda *a, **kw: MagicMock(),
        BiasCorrector=lambda *a, **kw: MagicMock(),
    )
    fake_singleton = _stub_module(
        "singleton_lock",
        acquire_process_singleton=lambda *a, **kw: True,
        release_process_singleton=lambda *a, **kw: None,
    )
    stubs = {
        "kalshi_auth": fake_auth,
        "probability": fake_probability,
        "ticker_utils": fake_ticker_utils,
        "capital_allocator": fake_allocator,
        "forecast_verifier": fake_verifier,
        "weather_data": fake_weather_data,
        "singleton_lock": fake_singleton,
    }
    saved = _install_stubs(stubs)
    sys.modules.pop("kalshi.apps.weather_bot", None)
    try:
        module = importlib.import_module("kalshi.apps.weather_bot")
    finally:
        _restore_stubs(saved, stubs)
        sys.modules.pop("kalshi.apps.weather_bot", None)

    assert callable(module.main)


def test_wrapper_scripts_delegate_to_apps_main():
    importlib.import_module("kalshi.apps")

    wrappers = {
        "weather-bot.py": "kalshi.apps.weather_bot",
        "crypto-bot.py": "kalshi.apps.crypto_bot",
        "economics-bot.py": "kalshi.apps.economics_bot",
        "entertainment-bot.py": "kalshi.apps.entertainment_bot",
        "source-monitor.py": "kalshi.apps.source_monitor",
        "position-monitor.py": "kalshi.apps.position_monitor",
        "strategy-trader.py": "kalshi.apps.strategy_trader",
        "market-maker.py": "kalshi.apps.market_maker",
        "cross-platform-arb.py": "kalshi.apps.cross_platform_arb",
        "beatrelease-scanner.py": "kalshi.apps.beatrelease_scanner",
        "demo-trader.py": "kalshi.apps.demo_trader",
        "hdd-scraper.py": "kalshi.apps.hdd_scraper",
    }

    installed = {}
    try:
        for wrapper_name, module_name in wrappers.items():
            main_mock = MagicMock()
            stub = _stub_module(module_name, main=main_mock)
            installed[module_name] = sys.modules.get(module_name)
            sys.modules[module_name] = stub
            runpy.run_path(str(PROJECT_DIR / "src" / "kalshi" / wrapper_name), run_name="__main__")
            main_mock.assert_called_once_with()
    finally:
        for module_name, original in installed.items():
            if original is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = original


def test_pyproject_declares_phase1_console_entrypoints():
    pyproject = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    expected = {
        "kalshi-weather",
        "kalshi-crypto",
        "kalshi-economics",
        "kalshi-monitor",
        "kalshi-positions",
        "kalshi-strategy",
        "kalshi-dashboard",
        "kalshi-supervisor",
    }
    assert expected.issubset(scripts)


def test_canonical_source_paths_resolve_to_apps_modules():
    assert resolve_bot_source_path("weather-bot.py") == PROJECT_DIR / "src" / "kalshi" / "apps" / "weather_bot.py"
    assert resolve_bot_source_path(Path("src/kalshi/strategy-trader.py")) == PROJECT_DIR / "src" / "kalshi" / "apps" / "strategy_trader.py"
