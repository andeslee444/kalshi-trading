"""Shared fixtures and path setup for the Kalshi trading bot test suite."""

import sys
import types
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Make sure ``src/kalshi/`` is importable by all test modules.
# This insert lets us do ``from strategy_trader import half_kelly`` etc.
# (The source files use underscored module names once imported.)
# ---------------------------------------------------------------------------
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def make_fake_auth(**overrides):
    """Create a fake kalshi_auth module with all required stubs.

    Usage in test files:
        fake = make_fake_auth()
        sys.modules["kalshi_auth"] = fake
        # Now import your bot module
    """
    mod = types.ModuleType("kalshi_auth")

    # Core classes — callables that accept any args and return a MagicMock instance.
    # Using lambdas (not MagicMock class itself) avoids InvalidSpecError when
    # bot code passes mock objects as constructor arguments.
    mod.KalshiClient = lambda *a, **kw: MagicMock()
    mod.TradeManager = lambda *a, **kw: MagicMock()
    mod.HealthCheckMonitor = lambda *a, **kw: MagicMock()
    mod.OrderMonitor = lambda *a, **kw: MagicMock()
    mod.ScanSummary = lambda *a, **kw: MagicMock()
    mod.CircuitBreaker = lambda *a, **kw: MagicMock()
    mod.RecentTradeTracker = lambda *a, **kw: MagicMock()

    # Setup functions
    mod.setup_unbuffered = lambda: None
    mod.setup_signal_handlers = lambda: None
    mod.setup_logging = lambda name: __import__("logging").getLogger(name)

    # File operations
    mod.save_trade = MagicMock()
    mod.load_trades = MagicMock(return_value=[])
    mod.save_decision = MagicMock()
    mod._atomic_write_json = MagicMock()
    mod.atomic_write_json = MagicMock()
    mod.trim_trade_log = MagicMock()

    # Safety
    mod.check_kill_switch = MagicMock()
    mod.is_shutdown_requested = MagicMock(return_value=False)
    mod.validate_trade_config = MagicMock()
    mod.build_market_snapshot = MagicMock(return_value={})
    mod.fetch_parallel = MagicMock(return_value=[])
    mod.retry_request = MagicMock(return_value=None)
    mod.notify_whatsapp = MagicMock()
    mod.notify_webhook = MagicMock()
    mod.normalize_market = MagicMock(side_effect=lambda market: market)
    mod.normalize_markets = MagicMock(side_effect=lambda markets: markets)

    # Constants
    mod.PROJECT_DIR = PROJECT_DIR
    mod.CITY_TIMEZONES = {}
    mod._local_today = MagicMock()
    mod.round_half_up = MagicMock()

    # Apply overrides
    for k, v in overrides.items():
        setattr(mod, k, v)

    return mod


def load_bot_module(bot_filename, fake_auth=None, extra_stubs=None, initialize=True):
    """Load a hyphenated bot module (e.g., 'weather-bot.py') with fake kalshi_auth.

    Args:
        bot_filename: e.g., 'weather-bot.py'
        fake_auth: Optional pre-configured fake_auth module. If None, creates default.
        extra_stubs: Optional dict of {module_name: fake_module} for additional stubs.
        initialize: If True, call ``build_app()`` after import when available.

    Returns:
        The loaded bot module.
    """
    if fake_auth is None:
        fake_auth = make_fake_auth()

    stubs = {"kalshi_auth": fake_auth}
    if extra_stubs:
        stubs.update(extra_stubs)

    saved_modules = {}
    for mod_name, fake_mod in stubs.items():
        if mod_name in sys.modules:
            saved_modules[mod_name] = sys.modules[mod_name]
        sys.modules[mod_name] = fake_mod

    try:
        bot_path = Path(_SRC_DIR) / bot_filename
        spec = importlib.util.spec_from_file_location(
            bot_filename.replace("-", "_").replace(".py", ""),
            str(bot_path),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if initialize and hasattr(mod, "build_app"):
            project_dir = getattr(fake_auth, "PROJECT_DIR", None)
            mod.build_app(project_dir=project_dir)
        return mod
    finally:
        for mod_name, original in saved_modules.items():
            sys.modules[mod_name] = original
        for mod_name in stubs:
            if mod_name not in saved_modules and mod_name in sys.modules:
                del sys.modules[mod_name]
