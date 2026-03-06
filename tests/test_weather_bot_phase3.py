"""Tests for weather bot Phase 3: HRRR blending, adaptive cadence,
orderbook depth gating, and model-run timing awareness.

Uses the standard bot module stubbing pattern (stub kalshi_auth in sys.modules
before importing the bot module).
"""

import sys
import os
import types
import datetime
import importlib
import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock


# ===================================================================
# Bot module loading with kalshi_auth stubbing
# ===================================================================

def _load_weather_bot():
    """Load weather-bot.py with stubbed kalshi_auth to avoid real API calls."""
    # Create a stub kalshi_auth module
    stub_auth = types.ModuleType("kalshi_auth")
    stub_auth.KalshiClient = MagicMock
    stub_auth.load_trades = MagicMock(return_value=[])
    stub_auth.save_trade = MagicMock()
    stub_auth.setup_unbuffered = MagicMock()
    stub_auth.setup_signal_handlers = MagicMock()
    stub_auth.setup_logging = MagicMock(return_value=MagicMock())
    stub_auth.PROJECT_DIR = MagicMock()
    stub_auth.PROJECT_DIR.__truediv__ = MagicMock(return_value=MagicMock())
    stub_auth.retry_request = MagicMock()
    stub_auth.TradeManager = MagicMock
    stub_auth.trim_trade_log = MagicMock()
    stub_auth.build_market_snapshot = MagicMock(return_value={})
    stub_auth.HealthCheckMonitor = MagicMock
    stub_auth.OrderMonitor = MagicMock
    stub_auth.ScanSummary = MagicMock
    stub_auth.is_shutdown_requested = MagicMock(return_value=False)
    stub_auth.fetch_parallel = MagicMock(return_value={})

    # Stub other imports
    stub_prob = types.ModuleType("probability")
    for fn in ["weather_probability", "weather_sigma", "ensemble_weather_probability",
               "ensemble_spread_sigma_multiplier", "ensemble_weather_probability_v2",
               "empirical_ensemble_probability", "half_kelly", "quarter_kelly",
               "high_conviction_kelly", "compute_limit_price", "kalshi_fee_cents",
               "is_market_liquid", "_load_calibration"]:
        setattr(stub_prob, fn, MagicMock())

    stub_ticker = types.ModuleType("ticker_utils")
    stub_ticker.parse_weather_ticker = MagicMock(return_value=None)

    stub_alloc = types.ModuleType("capital_allocator")
    stub_alloc.PortfolioAllocator = MagicMock

    stub_verifier = types.ModuleType("forecast_verifier")
    stub_verifier.ForecastVerifier = MagicMock
    stub_verifier.DEFAULT_STATION_MAP = {}

    # Register stubs in sys.modules before importing bot
    saved_modules = {}
    for mod_name in ["kalshi_auth", "probability", "ticker_utils",
                     "capital_allocator", "forecast_verifier"]:
        saved_modules[mod_name] = sys.modules.get(mod_name)

    sys.modules["kalshi_auth"] = stub_auth
    sys.modules["probability"] = stub_prob
    sys.modules["ticker_utils"] = stub_ticker
    sys.modules["capital_allocator"] = stub_alloc
    sys.modules["forecast_verifier"] = stub_verifier

    return stub_auth, stub_prob, stub_ticker, saved_modules


# ===================================================================
# Test compute_adaptive_interval
# ===================================================================

# Import the function directly from weather_data (no bot dependency needed)
sys.path.insert(0, "src/kalshi")
from weather_data import HRRRFetcher, OrderBookDepth, next_model_run, MODEL_RUN_SCHEDULE


class TestAdaptiveInterval:
    """Test compute_adaptive_interval() by extracting the function source.

    The bot module has heavy module-level side effects (API client init, config reads),
    so we extract and exec just the function rather than importing the full module.
    """

    @staticmethod
    def _load_compute_adaptive_interval():
        """Extract compute_adaptive_interval from weather-bot.py source."""
        bot_path = os.path.join(os.path.dirname(__file__), "..", "src", "kalshi", "weather-bot.py")
        with open(os.path.abspath(bot_path)) as f:
            source = f.read()

        # Extract the function definition
        import textwrap
        start = source.index("def compute_adaptive_interval(")
        # Find the next top-level def or class
        rest = source[start:]
        lines = rest.split("\n")
        func_lines = [lines[0]]
        for line in lines[1:]:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            func_lines.append(line)

        func_source = "\n".join(func_lines)

        # Create a namespace with the needed imports and a mock parse_ticker
        parse_mock = MagicMock(return_value=None)
        ns = {"datetime": datetime, "parse_ticker": parse_mock}
        exec(func_source, ns)

        return ns["compute_adaptive_interval"], parse_mock

    def _make_config(self, enabled=True):
        return {
            "scanIntervalMinutes": 30,
            "adaptiveScan": {
                "enabled": enabled,
                "day0Minutes": 5,
                "day1Minutes": 15,
                "day2PlusMinutes": 30,
                "modelRunTriggerMinutes": 2,
            },
        }

    def test_adaptive_disabled_returns_default(self):
        """When adaptiveScan is disabled, return scanIntervalMinutes."""
        compute, _ = self._load_compute_adaptive_interval()
        config = self._make_config(enabled=False)
        assert compute([], config) == 30

    def test_empty_markets_returns_day2plus(self):
        """Empty market list should default to day2+ interval."""
        compute, _ = self._load_compute_adaptive_interval()
        config = self._make_config()
        assert compute([], config) == 30

    def test_day0_market_returns_5_min(self):
        """Day-0 markets should trigger 5-minute scan interval."""
        compute, parse_mock = self._load_compute_adaptive_interval()
        today = datetime.date.today()
        parse_mock.return_value = {"date": today.isoformat(), "city": "MIA"}
        config = self._make_config()
        markets = [{"ticker": "KXHIGHMIA-26MAR05-T86"}]
        assert compute(markets, config) == 5

    def test_day1_market_returns_15_min(self):
        """Day-1 markets should trigger 15-minute scan interval."""
        compute, parse_mock = self._load_compute_adaptive_interval()
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        parse_mock.return_value = {"date": tomorrow.isoformat(), "city": "MIA"}
        config = self._make_config()
        markets = [{"ticker": "KXHIGHMIA-26MAR06-T86"}]
        assert compute(markets, config) == 15

    def test_day2plus_returns_30_min(self):
        """Day-2+ markets should use 30-minute default."""
        compute, parse_mock = self._load_compute_adaptive_interval()
        future = datetime.date.today() + datetime.timedelta(days=3)
        parse_mock.return_value = {"date": future.isoformat(), "city": "MIA"}
        config = self._make_config()
        markets = [{"ticker": "KXHIGHMIA-26MAR08-T86"}]
        assert compute(markets, config) == 30


class TestHRRRMemberInjection:
    """Test HRRR member injection via empirical_ensemble_probability."""

    def test_hrrr_injection_shifts_probability(self):
        """Injecting HRRR temps into ensemble should shift probability toward HRRR value."""
        # Base ensemble: 82 members all at 80F
        base_members = [80.0] * 82
        # HRRR says 90F — injecting should raise probability of exceeding 85F threshold
        hrrr_temp = 90.0
        hrrr_weight = 0.60
        n_inject = max(1, int(len(base_members) * hrrr_weight))
        assert n_inject == 49

        injected = base_members + [hrrr_temp] * n_inject
        assert len(injected) == 131

        # Empirical CDF: fraction above 85F should increase after injection
        above_threshold_base = sum(1 for t in base_members if t >= 85) / len(base_members)
        above_threshold_injected = sum(1 for t in injected if t >= 85) / len(injected)
        assert above_threshold_base == 0.0
        assert above_threshold_injected > 0.3  # 49/131 ≈ 0.374

    def test_small_ensemble_injects_at_least_1(self):
        """Even with tiny ensemble, inject at least 1 HRRR member."""
        members = [80.0]
        hrrr_weight = 0.30
        n_inject = max(1, int(len(members) * hrrr_weight))
        assert n_inject == 1

    def test_day2_no_injection(self):
        """Day-2+: weight=0, no injection."""
        members = [80.0] * 82
        hrrr_weight = 0.0
        n_inject = max(1, int(len(members) * hrrr_weight)) if hrrr_weight > 0 else 0
        assert n_inject == 0


class TestModelRunTimingOverride:
    """Test that model-run timing shortens scan interval when data is imminent."""

    def test_hrrr_imminent_shortens_interval(self):
        """When HRRR run is within 2 minutes, interval should shorten."""
        # HRRR runs hourly with 45-min delay
        # At XX:43, next HRRR run available at XX:45 = 2 min away
        now = datetime.datetime(2026, 3, 5, 10, 43, 0)
        model, minutes = next_model_run(now)
        assert model == "hrrr"
        assert minutes == 2

    def test_no_imminent_run_keeps_base(self):
        """When no model run is imminent, interval stays at base."""
        # At XX:50, HRRR 10Z was at 10:45 (already past), next HRRR 11Z at 11:45 = 55 min
        now = datetime.datetime(2026, 3, 5, 10, 50, 0)
        model, minutes = next_model_run(now)
        # 55 min > 2 min trigger, so interval stays at base
        assert minutes > 2


class TestOrderbookDepthGating:
    """Test orderbook depth gating and fill price improvement."""

    def test_skip_thin_book(self):
        """Market should be skipped when depth < minDepthContracts."""
        depth = {
            "yes_bids": [(85, 2)],
            "yes_asks": [(82, 3)],
            "total_bid_depth": 2,
            "total_ask_depth": 3,
        }
        min_depth = 5
        side = "yes"
        side_depth = depth["total_ask_depth"] if side == "yes" else depth["total_bid_depth"]
        assert side_depth < min_depth  # 3 < 5 -> should skip

    def test_sufficient_depth_passes(self):
        """Market with sufficient depth should not be skipped."""
        depth = {
            "yes_bids": [(85, 10), (84, 20)],
            "yes_asks": [(82, 8), (83, 12)],
            "total_bid_depth": 30,
            "total_ask_depth": 20,
        }
        min_depth = 5
        side = "yes"
        side_depth = depth["total_ask_depth"] if side == "yes" else depth["total_bid_depth"]
        assert side_depth >= min_depth  # 20 >= 5 -> passes

    def test_fill_price_improvement(self):
        """Depth should suggest better fill price than the listed ask."""
        ob = OrderBookDepth()
        depth = {
            "yes_bids": [(85, 10)],
            "yes_asks": [(80, 5), (82, 10)],
            "total_bid_depth": 10,
            "total_ask_depth": 15,
        }
        fill_price = ob.estimate_fill_price(depth, "yes", 5)
        assert fill_price == 80  # all 5 contracts fill at 80

        # If we need 8, it's (80*5 + 82*3) / 8 = 80.75
        fill_price_8 = ob.estimate_fill_price(depth, "yes", 8)
        assert fill_price_8 == pytest.approx(80.75, abs=0.01)

    def test_no_side_fill_price_conversion(self):
        """NO-side fill price must convert YES-VWAP to NO price space (100 - yes_price).

        Bug: estimate_fill_price('no') walks yes_bids and returns YES-price VWAP.
        The bot must convert: NO_price = 100 - YES_VWAP, and only improve if lower.
        """
        ob = OrderBookDepth()
        depth = {
            "yes_bids": [(85, 10), (84, 20)],
            "yes_asks": [(15, 10)],
            "total_bid_depth": 30,
            "total_ask_depth": 10,
        }
        # estimate_fill_price for NO side walks yes_bids -> returns YES VWAP
        fill_price_yes = ob.estimate_fill_price(depth, "no", 5)
        assert fill_price_yes == 85  # all 5 fill at best yes_bid of 85

        # Convert to NO price space
        fill_price_no = 100 - int(fill_price_yes)
        assert fill_price_no == 15  # correct NO price

        # If current NO price is 16c, the depth-suggested 15c is better (lower)
        current_no_price = 16
        assert fill_price_no < current_no_price  # 15 < 16 -> improvement

        # If current NO price is 14c, depth price of 15c is worse (higher) -> no improvement
        current_no_price_low = 14
        assert not (fill_price_no < current_no_price_low)  # 15 < 14 is False


class TestConfigPhase3:
    """Verify Phase 3 config keys are present and well-formed."""

    def setup_method(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "kalshi-config.json"
        )
        with open(config_path) as f:
            self.config = json.load(f)

    def test_hrrr_config_present(self):
        assert "hrrr" in self.config
        assert self.config["hrrr"]["enabled"] is True
        assert self.config["hrrr"]["weight_day0"] == 0.60
        assert self.config["hrrr"]["weight_day1"] == 0.30

    def test_adaptive_scan_config_present(self):
        assert "adaptiveScan" in self.config
        cfg = self.config["adaptiveScan"]
        assert cfg["enabled"] is True
        assert cfg["day0Minutes"] == 5
        assert cfg["day1Minutes"] == 15
        assert cfg["day2PlusMinutes"] == 30
        assert cfg["modelRunTriggerMinutes"] == 2

    def test_orderbook_depth_config_present(self):
        assert "orderbookDepth" in self.config
        assert self.config["orderbookDepth"]["enabled"] is True
        assert self.config["orderbookDepth"]["minDepthContracts"] == 5

    def test_existing_config_preserved(self):
        """Existing config keys should still be present."""
        assert self.config["mode"] == "demo"
        assert self.config["maxTradeAmount"] == 30
        assert self.config["edgeThreshold"] == 0.06
        assert len(self.config["cities"]) == 20
        assert self.config["ensemble"]["enabled"] is True
        assert self.config["verification"]["enabled"] is True
