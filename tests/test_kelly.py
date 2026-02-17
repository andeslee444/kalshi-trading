"""Tests for the half-Kelly position-sizing function in strategy-trader.py."""

import importlib
import math
import pytest


# ---------------------------------------------------------------------------
# Import helper -- the source file is named with a hyphen so we use
# importlib to load it.
# ---------------------------------------------------------------------------

def _load_strategy_trader():
    """Import strategy-trader.py without executing its module-level side effects.

    The module instantiates a KalshiClient at import time, which requires
    credentials.  We patch the heavy imports away so we can unit-test the
    pure ``half_kelly`` function in isolation.
    """
    import types, sys

    # Save the real kalshi_auth entry (if any) so we can restore it after
    # loading strategy-trader.py.  Without this, the fake module leaks into
    # other test files (e.g. test_trades.py) that need the real one.
    orig_auth = sys.modules.get("kalshi_auth")

    # Provide a lightweight stub for kalshi_auth so the top-level import
    # succeeds without real credentials or a private key file.
    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: None
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = __import__("pathlib").Path("/tmp/fake_project")
    fake_auth.load_trades = lambda *a, **kw: []
    fake_auth.save_trade = lambda *a, **kw: None
    sys.modules["kalshi_auth"] = fake_auth

    from pathlib import Path
    import json

    # Create stub config file that strategy-trader.py reads at import time
    config_dir = Path("/tmp/fake_project/config")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "bots-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps({
            "strategy": {"maxBetCents": 500},
            "entertainment": {"maxTradeAmount": 5, "maxDailyTrades": 10,
                              "confidenceThreshold": 0.85, "scanIntervalMinutes": 15, "tickers": []},
            "beatrelease": {"checkIntervalHours": 4, "maxTradeCents": 500, "blogUrls": []},
        }))

    spec = importlib.util.spec_from_file_location(
        "strategy_trader",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "strategy-trader.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore the original kalshi_auth so later test modules get the real one.
    if orig_auth is not None:
        sys.modules["kalshi_auth"] = orig_auth
    else:
        del sys.modules["kalshi_auth"]

    return mod


_mod = _load_strategy_trader()
half_kelly = _mod.half_kelly
MAX_BET = _mod.MAX_BET


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHalfKelly:
    """Unit tests for ``half_kelly(edge, price_cents, bankroll)``."""

    def test_returns_zero_when_edge_is_zero(self):
        assert half_kelly(0, 5, 50000) == 0

    def test_returns_zero_when_edge_is_negative(self):
        assert half_kelly(-0.10, 5, 50000) == 0

    def test_returns_zero_when_price_cents_is_zero(self):
        assert half_kelly(0.20, 0, 50000) == 0

    def test_returns_zero_when_price_cents_is_negative(self):
        assert half_kelly(0.20, -5, 50000) == 0

    def test_returns_zero_when_price_cents_at_100(self):
        assert half_kelly(0.20, 100, 50000) == 0

    def test_returns_zero_when_price_cents_above_100(self):
        assert half_kelly(0.20, 150, 50000) == 0

    def test_positive_contracts_for_valid_edge(self):
        """A healthy edge of 20% on a 5-cent contract with a $500 bankroll
        should produce a positive number of contracts."""
        contracts = half_kelly(0.20, 5, 50000)
        assert contracts > 0

    def test_result_is_integer(self):
        contracts = half_kelly(0.20, 5, 50000)
        assert isinstance(contracts, int)

    def test_respects_max_bet_cap(self):
        """Even with a huge bankroll the number of contracts must be capped
        by MAX_BET // risk_per_contract."""
        huge_bankroll = 10_000_000  # $100 000
        contracts = half_kelly(0.50, 5, huge_bankroll)
        risk_per = 100 - 5  # 95 cents
        assert contracts <= MAX_BET // risk_per

    def test_returns_zero_for_tiny_edge(self):
        """An edge so small that half-Kelly fraction rounds to 0 contracts."""
        contracts = half_kelly(0.001, 50, 1000)
        assert contracts == 0

    def test_larger_bankroll_more_contracts(self):
        """Doubling the bankroll should not decrease the number of contracts."""
        c1 = half_kelly(0.20, 5, 10000)
        c2 = half_kelly(0.20, 5, 20000)
        assert c2 >= c1

    def test_higher_edge_more_contracts(self):
        """A bigger edge (same bankroll, same price) should give at least as
        many contracts."""
        c_low = half_kelly(0.10, 5, 50000)
        c_high = half_kelly(0.30, 5, 50000)
        assert c_high >= c_low
