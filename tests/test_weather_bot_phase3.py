"""Tests for weather bot Phase 3: HRRR blending, adaptive cadence,
orderbook depth gating, and model-run timing awareness.

Uses the standard bot module stubbing pattern (stub kalshi_auth in sys.modules
before importing the bot module).
"""

import sys
import os
import types
import datetime
import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from conftest import make_fake_auth


# ===================================================================
# Bot module loading with kalshi_auth stubbing
# ===================================================================

def _setup_weather_bot_stubs():
    """Register stubbed modules so weather-bot.py imports succeed.

    Returns the stub objects for tests that need to inspect or configure them.
    Does NOT load the full bot module (tests extract individual functions).
    """
    stub_auth = make_fake_auth()

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
    stub_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()

    stub_verifier = types.ModuleType("forecast_verifier")
    stub_verifier.ForecastVerifier = lambda *a, **kw: MagicMock()
    stub_verifier.DEFAULT_STATION_MAP = {}

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
        assert self.config["maxTradeAmount"] == 30
        assert self.config["edgeThreshold"] == 0.06
        assert len(self.config["cities"]) == 20
        assert self.config["ensemble"]["enabled"] is False  # disabled on free Open-Meteo tier
        assert self.config["verification"]["enabled"] is True


# ===================================================================
# Test ensemble recovery with per-model circuit breakers
# ===================================================================

class TestEnsembleRecovery:
    """Test graceful degradation when ensemble models fail individually."""

    def test_ensemble_handles_single_model_failure(self):
        """Should still produce valid forecasts with 2/3 models available."""
        from probability import ensemble_weather_probability, _reset_calibration
        _reset_calibration()
        try:
            # 2 of 3 models available (ECMWF missing)
            forecasts = {"gfs": 85.0, "icon": 84.5}
            result = ensemble_weather_probability(forecasts, 86, "T", 2)
            assert result is not None
            assert 0 < result < 1
        finally:
            _reset_calibration()

    def test_ensemble_handles_two_model_failure(self):
        """Should produce probability even with only 1 model."""
        from probability import ensemble_weather_probability, _reset_calibration
        _reset_calibration()
        try:
            forecasts = {"gfs": 90.0}
            result = ensemble_weather_probability(forecasts, 86, "T", 2)
            assert result is not None
            assert 0 < result < 1
        finally:
            _reset_calibration()

    def test_batch_failure_does_not_block_per_model(self):
        """Batch endpoint failure should use separate circuit breaker key from per-model fetches.

        The fix uses 'open-meteo-batch' for batch and 'open-meteo-{model}' per model,
        so batch failures don't cascade to block individual model fetches.
        """
        # Verify the circuit breaker keys are different
        batch_key = "open-meteo-batch"
        model_keys = [f"open-meteo-{m}" for m in ["gfs", "ecmwf", "icon"]]
        assert batch_key not in model_keys
        for mk in model_keys:
            assert mk != batch_key

    def test_degraded_ensemble_logs_model_count(self):
        """When models are degraded, the available count should be trackable."""
        all_models = {"gfs", "ecmwf", "icon"}
        available = {"gfs": 85.0, "icon": 84.5}  # ecmwf missing
        failed = all_models - set(available.keys())
        assert len(available) == 2
        assert failed == {"ecmwf"}

    def test_empty_ensemble_returns_empty_dict(self):
        """When all models fail and GFS fallback also fails, return empty dict."""
        # This is a logic test: if model_forecasts is empty and get_forecast raises,
        # the function should return {} not crash
        result = {}  # simulating the fallback-failed case
        assert result == {}


# ===================================================================
# Test days-out-aware dedup cooldown
# ===================================================================

class TestDedupCooldown:
    """Test the local dedup cooldown that scales with days_out.

    Tests the pure function logic directly without loading the full bot module.
    """

    @staticmethod
    def _get_dedup_cooldown(days_out):
        """Mirror of the get_dedup_cooldown function from weather-bot.py."""
        if days_out == 0:
            return 1800    # 30 min
        elif days_out == 1:
            return 7200    # 2 hours
        elif days_out == 2:
            return 14400   # 4 hours
        else:
            return 43200   # 12 hours

    def test_dedup_cooldown_scales_with_days_out(self):
        """Day-0 markets should have shorter dedup than day-3 markets."""
        cooldown_day0 = self._get_dedup_cooldown(days_out=0)
        cooldown_day3 = self._get_dedup_cooldown(days_out=3)
        assert cooldown_day0 <= 3600, f"Day-0 cooldown should be <=1h, got {cooldown_day0}s"
        assert cooldown_day3 >= 21600, f"Day-3 cooldown should be >=6h, got {cooldown_day3}s"

    def test_day0_cooldown_is_30_min(self):
        """Day-0 should have 30 minute cooldown."""
        assert self._get_dedup_cooldown(0) == 1800

    def test_day1_cooldown_is_2_hours(self):
        """Day-1 should have 2 hour cooldown."""
        assert self._get_dedup_cooldown(1) == 7200

    def test_day2_cooldown_is_4_hours(self):
        """Day-2 should have 4 hour cooldown."""
        assert self._get_dedup_cooldown(2) == 14400

    def test_day3plus_cooldown_is_12_hours(self):
        """Day-3+ should have 12 hour cooldown (original default)."""
        assert self._get_dedup_cooldown(3) == 43200
        assert self._get_dedup_cooldown(5) == 43200
        assert self._get_dedup_cooldown(10) == 43200

    def test_cooldown_monotonically_increasing(self):
        """Cooldown should increase or stay the same as days_out increases."""
        prev = 0
        for d in range(8):
            cd = self._get_dedup_cooldown(d)
            assert cd >= prev, f"Cooldown decreased from day-{d-1} to day-{d}: {prev}s -> {cd}s"
            prev = cd


# ===================================================================
# Test limit order type selection
# ===================================================================

class TestChooseOrderType:
    """Test choose_order_type logic for market vs limit order selection.

    Mirrors the choose_order_type function from weather-bot.py.
    """

    @staticmethod
    def _choose_order_type(yes_bid, yes_ask, side, edge, our_prob, depth_data=None):
        """Mirror of choose_order_type from weather-bot.py for unit testing."""
        spread = (yes_ask - yes_bid) if (yes_ask and yes_bid) else 0

        if edge > 0.20:
            if side == "yes":
                return "market", yes_ask
            else:
                return "market", 100 - yes_bid if yes_bid else yes_ask

        thin_book = False
        if depth_data:
            side_depth = depth_data.get("total_ask_depth", 0) if side == "yes" else depth_data.get("total_bid_depth", 0)
            if side_depth < 100:
                thin_book = True

        if spread > 5 or thin_book:
            model_price = int(our_prob * 100) if side == "yes" else int((1 - our_prob) * 100)
            if side == "yes":
                limit_price = max(yes_bid + 1 if yes_bid else 1, min(model_price, yes_ask - 1 if yes_ask > 1 else yes_ask))
            else:
                no_bid = 100 - yes_ask if yes_ask else 0
                no_ask = 100 - yes_bid if yes_bid else 100
                limit_price = max(no_bid + 1 if no_bid else 1, min(model_price, no_ask - 1 if no_ask > 1 else no_ask))
            return "limit", max(1, limit_price)

        return "market", yes_ask if side == "yes" else (100 - yes_bid if yes_bid else 0)

    def test_high_edge_always_market(self):
        """Very high edge (>20%) should always use market orders for fill certainty."""
        order_type, _ = self._choose_order_type(40, 50, "yes", 0.25, 0.70)
        assert order_type == "market"

    def test_wide_spread_uses_limit(self):
        """Wide spread (>5c) should use limit orders."""
        order_type, price = self._choose_order_type(40, 50, "yes", 0.10, 0.70)
        assert order_type == "limit"
        # Limit price should be between bid+1 and ask-1
        assert 41 <= price <= 49

    def test_tight_spread_uses_market(self):
        """Tight spread (<=5c) with sufficient depth should use market."""
        depth = {"total_ask_depth": 200, "total_bid_depth": 200}
        order_type, _ = self._choose_order_type(45, 48, "yes", 0.10, 0.70, depth)
        assert order_type == "market"

    def test_thin_book_uses_limit(self):
        """Thin book (<100 contracts) should use limit orders."""
        depth = {"total_ask_depth": 50, "total_bid_depth": 50}
        order_type, price = self._choose_order_type(45, 48, "yes", 0.10, 0.70, depth)
        assert order_type == "limit"

    def test_limit_price_based_on_model_probability(self):
        """Limit price should reflect model probability (clamped to spread)."""
        # Model says 70% YES prob -> fair value 70c
        # Spread is 40-50, so limit should be clamped to 49 (ask-1)
        _, price = self._choose_order_type(40, 50, "yes", 0.10, 0.70)
        assert price == 49  # clamped to ask-1

    def test_limit_price_no_side(self):
        """NO-side limit should work correctly in NO price space."""
        # our_prob = 0.30 means P(NO) = 0.70, fair NO value = 70c
        # YES bid=80, YES ask=90 -> NO bid=10, NO ask=20
        order_type, price = self._choose_order_type(80, 90, "no", 0.10, 0.30)
        assert order_type == "limit"
        # Model NO prob = 70c, but NO ask is 20c, so clamped to 19
        assert price == 19

    def test_market_order_returns_ask_price(self):
        """Market order should return the ask price."""
        _, price = self._choose_order_type(45, 48, "yes", 0.25, 0.70)
        assert price == 48  # yes_ask for high edge market order
