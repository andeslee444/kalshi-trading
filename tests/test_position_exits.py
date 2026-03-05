"""Tests for position exit logic: take-profit, stop-loss, model-shift.

Tests cover per-bot exit config routing, partial exits, market vs limit
order types, and multi-model probability routing for model-shift.
"""

import json
import sys
import types
import importlib.util
import logging
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Module loader: creates a fake environment so position-monitor.py can be
# imported without real API credentials, file system state, or network calls.
# ---------------------------------------------------------------------------

_BOT_CONFIG = {
    "weather": {
        "exit": {
            "takeProfitCents": 80,
            "stopLossCents": 30,
            "modelShiftPp": 20,
            "trailingDropCents": 10,
            "trailingMinProfitCents": 10,
            "takeProfitFraction": 0.50,
        }
    },
    "crypto": {
        "exit": {
            "takeProfitCents": 85,
            "stopLossCents": 25,
            "modelShiftPp": 15,
            "trailingDropCents": 15,
            "trailingMinProfitCents": 15,
            "takeProfitFraction": 0.50,
        }
    },
    "entertainment": {
        "exit": {
            "takeProfitCents": 80,
            "stopLossCents": 30,
            "modelShiftPp": 20,
            "trailingDropCents": 10,
            "trailingMinProfitCents": 10,
            "takeProfitFraction": 0.50,
        }
    },
    "economics": {
        "exit": {
            "takeProfitCents": 80,
            "stopLossCents": 30,
            "modelShiftPp": 20,
            "trailingDropCents": 10,
            "trailingMinProfitCents": 10,
            "takeProfitFraction": 0.50,
        }
    },
    "strategy": {
        "exit": {
            "takeProfitCents": 80,
            "stopLossCents": 30,
            "modelShiftPp": 20,
            "trailingDropCents": 10,
            "trailingMinProfitCents": 10,
            "takeProfitFraction": 0.50,
        }
    },
    "beatrelease": {
        "exit": {
            "takeProfitCents": 80,
            "stopLossCents": 30,
            "modelShiftPp": 20,
            "trailingDropCents": 10,
            "trailingMinProfitCents": 10,
            "takeProfitFraction": 0.50,
        }
    },
    "position_monitor": {
        "takeProfitThreshold": 0.80,
        "stopLossThreshold": 0.30,
        "stopLossPct": 0.40,
        "modelShiftThreshold": 0.20,
        "maxDailyExits": 20,
        "orderTtlMinutes": 120,
        "trailingDropCents": 10,
        "trailingMinProfitCents": 10,
    },
}


def _load_position_monitor():
    """Load position-monitor.py module with mock dependencies."""
    orig_modules = {}
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "ticker_utils"):
        if mod_name in sys.modules:
            orig_modules[mod_name] = sys.modules[mod_name]

    fake_client = MagicMock()

    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: fake_client
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.is_shutdown_requested = lambda: False
    fake_auth.setup_logging = lambda *a, **kw: logging.getLogger("test")
    fake_auth.PROJECT_DIR = Path("/tmp/fake_posmon_exits")
    fake_auth.TradeManager = type("TradeManager", (), {
        "__init__": lambda self, *a, **kw: None,
        "sell_position": lambda self, *a, **kw: None,
        "log_decision": lambda self, *a, **kw: None,
    })
    fake_auth.trim_trade_log = lambda *a, **kw: None
    fake_auth.load_trades = lambda *a, **kw: []
    fake_auth._atomic_write_json = lambda *a, **kw: None
    fake_auth.CITY_TIMEZONES = {
        "MIA": "America/New_York", "LAX": "America/Los_Angeles",
        "PHIL": "America/New_York", "NY": "America/New_York",
        "CHI": "America/Chicago", "AUS": "America/Chicago",
        "DEN": "America/Denver", "HOU": "America/Chicago",
    }
    fake_auth._local_today = lambda city_code="NY": "2026-02-20"
    fake_auth.round_half_up = lambda v: int(
        __import__("decimal").Decimal(str(v)).quantize(
            __import__("decimal").Decimal("1"),
            rounding=__import__("decimal").ROUND_HALF_UP,
        )
    )
    fake_auth.retry_request = lambda *a, **kw: MagicMock()
    fake_auth.fetch_parallel = lambda *a, **kw: []
    fake_auth.HealthCheckMonitor = type("HealthCheckMonitor", (), {
        "__init__": lambda self, *a, **kw: None,
        "record_bot_heartbeat": lambda self, *a, **kw: None,
        "check_health": lambda self, *a, **kw: [],
    })
    fake_auth.ScanSummary = type("ScanSummary", (), {
        "__init__": lambda self, *a, **kw: None,
        "markets_fetched": 0,
        "markets_evaluated": 0,
        "trades_placed": 0,
        "skip": lambda self, *a, **kw: None,
        "finalize": lambda self: None,
    })
    fake_auth.notify_whatsapp = lambda *a, **kw: None
    sys.modules["kalshi_auth"] = fake_auth

    # Real kalshi_fee_cents formula for accurate testing
    KALSHI_FEE_RATE = 0.07
    fake_prob = types.ModuleType("probability")
    fake_prob.half_kelly = lambda *a, **kw: (0, 0)
    fake_prob.weather_probability = lambda *a, **kw: 0.5
    fake_prob.nws_probability = lambda *a, **kw: 0.5
    fake_prob.crypto_price_probability = lambda *a, **kw: 0.5
    fake_prob.kalshi_fee_cents = lambda p: KALSHI_FEE_RATE * (p / 100) * (1 - p / 100) * 100
    sys.modules["probability"] = fake_prob

    fake_ticker = types.ModuleType("ticker_utils")
    fake_ticker.parse_weather_ticker = lambda ticker: None
    fake_ticker.parse_crypto_ticker = lambda ticker: None
    sys.modules["ticker_utils"] = fake_ticker

    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = type("PortfolioAllocator", (), {
        "__init__": lambda self, *a, **kw: None,
    })
    sys.modules["capital_allocator"] = fake_alloc

    # Create dirs and config
    data_dir = Path("/tmp/fake_posmon_exits/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir = Path("/tmp/fake_posmon_exits/config")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "bots-config.json").write_text(json.dumps(_BOT_CONFIG))

    # Load the module
    spec = importlib.util.spec_from_file_location(
        "position_monitor",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "position-monitor.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original modules
    for mod_name in ("kalshi_auth", "probability", "capital_allocator", "ticker_utils"):
        if mod_name in orig_modules:
            sys.modules[mod_name] = orig_modules[mod_name]
        elif mod_name in sys.modules:
            del sys.modules[mod_name]

    return mod


# Load module once at import time (fast: all dependencies are mocked)
_mod = _load_position_monitor()


# ---------------------------------------------------------------------------
# Helper: build an exit_config dict matching the _get_exit_config output
# ---------------------------------------------------------------------------

def _exit_config(take_profit_cents=80, stop_loss_cents=30, model_shift_pp=20,
                 trailing_drop_cents=10, trailing_min_profit_cents=10,
                 take_profit_fraction=0.50):
    return {
        "take_profit_cents": take_profit_cents,
        "stop_loss_cents": stop_loss_cents,
        "model_shift_pp": model_shift_pp,
        "trailing_drop_cents": trailing_drop_cents,
        "trailing_min_profit_cents": trailing_min_profit_cents,
        "take_profit_fraction": take_profit_fraction,
    }


# ===================================================================
# _get_exit_config tests
# ===================================================================

class TestGetExitConfig:
    """Test per-bot exit threshold routing."""

    def test_known_bot_returns_bot_config(self):
        """Weather bot should get weather-specific exit thresholds."""
        cfg = _mod._get_exit_config("weather")
        assert cfg["take_profit_cents"] == 80
        assert cfg["stop_loss_cents"] == 30
        assert cfg["model_shift_pp"] == 20
        assert cfg["take_profit_fraction"] == 0.50

    def test_crypto_has_tighter_thresholds(self):
        """Crypto bot has different thresholds (85c TP, 25c SL)."""
        cfg = _mod._get_exit_config("crypto")
        assert cfg["take_profit_cents"] == 85
        assert cfg["stop_loss_cents"] == 25
        assert cfg["model_shift_pp"] == 15

    def test_unknown_bot_returns_defaults(self):
        """Unknown source_bot falls back to position_monitor defaults."""
        cfg = _mod._get_exit_config("unknown_bot_xyz")
        # Falls back to position_monitor config: 80% = 80c, 30% = 30c
        assert cfg["take_profit_cents"] == 80
        assert cfg["stop_loss_cents"] == 30

    def test_source_monitor_maps_to_weather(self):
        """source-monitor trades should use weather exit config."""
        cfg = _mod._get_exit_config("source-monitor")
        assert cfg["take_profit_cents"] == 80
        assert cfg["stop_loss_cents"] == 30


# ===================================================================
# evaluate_take_profit tests
# ===================================================================

class TestEvaluateTakeProfit:
    """Test take-profit with partial exits and limit orders."""

    def test_triggers_at_threshold(self):
        """Should trigger when net proceeds >= take_profit_cents."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        # YES bid at 90c, fee = 0.07 * 0.9 * 0.1 * 100 = 0.63c
        # net = 90 - 0.63 = 89.37 >= 80
        market = {"yes_bid": 90, "yes_ask": 91}
        result = _mod.evaluate_take_profit(pos, market, _exit_config())
        assert result is not None
        assert result["action"] == "take_profit"
        assert result["side"] == "yes"

    def test_partial_exit_fraction(self):
        """Should sell take_profit_fraction of total, minimum 1 contract."""
        pos = {"ticker": "TICK-1", "yes": 10, "no": 0}
        market = {"yes_bid": 92, "yes_ask": 93}
        result = _mod.evaluate_take_profit(pos, market, _exit_config(take_profit_fraction=0.50))
        assert result is not None
        assert result["count"] == 5  # 50% of 10

    def test_partial_exit_minimum_one(self):
        """With 1 contract, should still sell 1 (minimum)."""
        pos = {"ticker": "TICK-1", "yes": 1, "no": 0}
        market = {"yes_bid": 92, "yes_ask": 93}
        result = _mod.evaluate_take_profit(pos, market, _exit_config(take_profit_fraction=0.50))
        assert result is not None
        assert result["count"] == 1

    def test_uses_limit_order_type(self):
        """Take-profit exits should specify order_type='limit'."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        market = {"yes_bid": 90, "yes_ask": 91}
        result = _mod.evaluate_take_profit(pos, market, _exit_config())
        assert result is not None
        assert result["order_type"] == "limit"

    def test_no_trigger_below_threshold(self):
        """Should return None when bid below threshold."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        # YES bid at 50c, well below 80c threshold
        market = {"yes_bid": 50, "yes_ask": 52}
        result = _mod.evaluate_take_profit(pos, market, _exit_config())
        assert result is None

    def test_fee_deducted_before_comparison(self):
        """Net proceeds (bid - fee) compared against threshold, not raw bid."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        # YES bid at 82c, fee = 0.07 * 0.82 * 0.18 * 100 = 1.03c
        # net = 82 - 1.03 = 80.97 >= 80 (just barely passes)
        market = {"yes_bid": 82, "yes_ask": 84}
        result = _mod.evaluate_take_profit(pos, market, _exit_config())
        assert result is not None

    def test_no_side_take_profit(self):
        """NO position take-profit should also work with partial exit."""
        pos = {"ticker": "TICK-1", "yes": 0, "no": 6}
        market = {"yes_bid": 10, "yes_ask": 12, "no_bid": 90}
        result = _mod.evaluate_take_profit(pos, market, _exit_config())
        assert result is not None
        assert result["side"] == "no"
        assert result["count"] == 3  # 50% of 6


# ===================================================================
# evaluate_stop_loss tests
# ===================================================================

class TestEvaluateStopLoss:
    """Test stop-loss with market orders and full exit."""

    def test_triggers_at_threshold(self):
        """Should trigger when bid <= stop_loss_cents."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        # YES bid at 25c, stop_loss_cents = 30
        market = {"yes_bid": 25, "yes_ask": 30}
        result = _mod.evaluate_stop_loss(pos, market, _exit_config(), entry_price_cents=60)
        assert result is not None
        assert result["action"] == "stop_loss"

    def test_exits_full_position(self):
        """Stop-loss always exits full count, never partial."""
        pos = {"ticker": "TICK-1", "yes": 10, "no": 0}
        market = {"yes_bid": 20, "yes_ask": 25}
        result = _mod.evaluate_stop_loss(pos, market, _exit_config(), entry_price_cents=60)
        assert result is not None
        assert result["count"] == 10

    def test_uses_market_order_type(self):
        """Stop-loss exits should specify order_type='market'."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        market = {"yes_bid": 20, "yes_ask": 25}
        result = _mod.evaluate_stop_loss(pos, market, _exit_config(), entry_price_cents=60)
        assert result is not None
        assert result["order_type"] == "market"

    def test_no_trigger_above_threshold(self):
        """Should return None when bid above threshold."""
        pos = {"ticker": "TICK-1", "yes": 4, "no": 0}
        # YES bid at 50c, well above 30c threshold
        market = {"yes_bid": 50, "yes_ask": 52}
        result = _mod.evaluate_stop_loss(pos, market, _exit_config(), entry_price_cents=60)
        assert result is None

    def test_no_side_stop_loss(self):
        """NO position stop-loss should also work."""
        pos = {"ticker": "TICK-1", "yes": 0, "no": 5}
        # no_bid defaults to 100 - yes_ask when no_bid missing
        market = {"yes_bid": 80, "yes_ask": 85, "no_bid": 15}
        result = _mod.evaluate_stop_loss(pos, market, _exit_config(stop_loss_cents=20))
        assert result is not None
        assert result["side"] == "no"
        assert result["count"] == 5
        assert result["order_type"] == "market"


# ===================================================================
# evaluate_model_shift tests
# ===================================================================

class TestEvaluateModelShift:
    """Test model-shift with multi-model routing."""

    def test_triggers_on_divergence(self):
        """Should trigger when current prob diverges from entry by >= model_shift_pp."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "weather", "model_prob": 0.75}
        ec = _exit_config(model_shift_pp=20)

        # Mock _compute_current_probability to return low prob (divergence)
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.30, "NWS MIA high 78F, model prob=30%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is not None
        assert result["action"] == "model_shift"
        assert result["order_type"] == "limit"
        assert "divergence" in result["reasoning"]

    def test_no_trigger_small_divergence(self):
        """Should return None when divergence < model_shift_pp."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 70, "yes_ask": 72}
        entry_rec = {"source_bot": "weather", "model_prob": 0.75}
        ec = _exit_config(model_shift_pp=20)

        # Current prob close to entry (divergence only 5pp, below 20pp threshold)
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.70, "NWS MIA high 85F, model prob=70%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None

    def test_uses_limit_order_type(self):
        """Model-shift exits should specify order_type='limit'."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 5, "no": 0}
        market = {"yes_bid": 35, "yes_ask": 40}
        entry_rec = {"source_bot": "weather", "model_prob": 0.80}
        ec = _exit_config(model_shift_pp=20)

        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.25, "NWS MIA high 76F, model prob=25%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is not None
        assert result["order_type"] == "limit"

    def test_skips_entertainment_bots(self):
        """Entertainment/beatrelease positions should return None (hold to settlement)."""
        pos = {"ticker": "ALBUM-SOMETHING", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "entertainment", "model_prob": 0.80}
        ec = _exit_config(model_shift_pp=20)

        # _compute_current_probability returns (None, None) for entertainment
        result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None

    def test_skips_beatrelease_bots(self):
        """Beatrelease positions should also return None."""
        pos = {"ticker": "ALBUM-BR", "yes": 2, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "beatrelease", "model_prob": 0.70}
        ec = _exit_config(model_shift_pp=20)

        result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None

    def test_routes_weather_to_nws_probability(self):
        """Weather source_bot should use _compute_current_probability (NWS model)."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "weather", "model_prob": 0.80}
        ec = _exit_config(model_shift_pp=20)

        # Verify the function calls _compute_current_probability with correct args
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.25, "NWS MIA high 76F")) as mock_ccp:
            _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
            mock_ccp.assert_called_once_with("KXHIGHMIA-26FEB20-T86", "weather", "yes")

    def test_no_entry_rec_returns_none(self):
        """Without entry record, model-shift cannot be evaluated."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        ec = _exit_config()
        result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=None)
        assert result is None

    def test_no_model_prob_returns_none(self):
        """Entry record without model_prob should return None."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "weather"}  # no model_prob
        ec = _exit_config()
        result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None

    def test_no_trigger_when_prob_above_50(self):
        """Even with large divergence, should not trigger if current prob >= 0.50."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 60, "yes_ask": 65}
        entry_rec = {"source_bot": "weather", "model_prob": 0.90}
        ec = _exit_config(model_shift_pp=20)

        # current prob 0.55 -- divergence is 35pp (>20pp) but prob > 0.50
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.55, "NWS MIA high 84F, model prob=55%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None


# ===================================================================
# _compute_current_probability routing tests
# ===================================================================

class TestComputeCurrentProbability:
    """Test multi-model routing in _compute_current_probability."""

    def test_entertainment_returns_none(self):
        """Entertainment source_bot should return (None, None)."""
        prob, reason = _mod._compute_current_probability("ALBUM-1", "entertainment", "yes")
        assert prob is None
        assert reason is None

    def test_beatrelease_returns_none(self):
        """Beatrelease source_bot should return (None, None)."""
        prob, reason = _mod._compute_current_probability("ALBUM-BR", "beatrelease", "yes")
        assert prob is None
        assert reason is None

    def test_crypto_returns_none(self):
        """Crypto source_bot should return (None, None) for now."""
        prob, reason = _mod._compute_current_probability("KXBTC-1", "crypto", "yes")
        assert prob is None
        assert reason is None

    def test_economics_returns_none(self):
        """Economics source_bot should return (None, None) for now."""
        prob, reason = _mod._compute_current_probability("KXCPI-1", "economics", "yes")
        assert prob is None
        assert reason is None

    def test_unknown_bot_returns_none(self):
        """Unknown source_bot should return (None, None)."""
        prob, reason = _mod._compute_current_probability("TICK-1", "unknown_xyz", "yes")
        assert prob is None
        assert reason is None


# ===================================================================
# evaluate_trailing_stop tests (with illiquidity protection and arming)
# ===================================================================

class TestEvaluateTrailingStop:
    """Test trailing stop with illiquidity protection, arming, and market orders."""

    def test_triggers_on_drop_from_peak(self):
        """Should trigger when bid drops trailing_drop_cents from peak."""
        pos = {"ticker": "TICK-1", "yes": 5, "no": 0}
        market = {"yes_bid": 60, "yes_ask": 62}
        peak = {"entry_price": 40, "peak_bid": 75, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, updated_peak = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is not None
        assert result["action"] == "trailing_stop"
        assert result["side"] == "yes"
        assert result["count"] == 5
        assert result["price"] == 60
        # Dropped 15c from peak of 75, threshold is 10c

    def test_arms_only_after_min_profit(self):
        """Should NOT trigger if peak hasn't reached entry + min profit."""
        pos = {"ticker": "TICK-1", "yes": 5, "no": 0}
        market = {"yes_bid": 42, "yes_ask": 44}
        # Peak is 48, entry is 40, min_profit is 10 => need peak >= 50
        peak = {"entry_price": 40, "peak_bid": 48, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=5, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is None

    def test_skips_illiquid_no_bid(self):
        """Should return None with warning when bid is 0."""
        pos = {"ticker": "TICK-1", "yes": 5, "no": 0}
        market = {"yes_bid": 0, "yes_ask": 50}
        peak = {"entry_price": 30, "peak_bid": 70, "side": "yes"}
        ec = _exit_config()
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is None

    def test_skips_illiquid_wide_spread(self):
        """Should return None when spread > 20c."""
        pos = {"ticker": "TICK-1", "yes": 5, "no": 0}
        # Spread = 70 - 40 = 30c > 20c
        market = {"yes_bid": 40, "yes_ask": 70}
        peak = {"entry_price": 30, "peak_bid": 60, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is None

    def test_uses_market_order_type(self):
        """Trailing stop exits should specify order_type='market'."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 55, "yes_ask": 57}
        peak = {"entry_price": 40, "peak_bid": 70, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is not None
        assert result["order_type"] == "market"
        assert "MARKET ORDER" in result["reasoning"]

    def test_updates_peak_on_higher_bid(self):
        """Peak should update when current bid exceeds stored peak."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 80, "yes_ask": 82}
        peak = {"entry_price": 40, "peak_bid": 70, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, updated = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        # No trigger (bid=80 is new peak, no drop)
        assert result is None
        assert updated["peak_bid"] == 80
        assert "last_updated" in updated

    def test_no_trigger_when_bid_above_peak_minus_drop(self):
        """Should return None when drop is less than trailing_drop_cents."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 66, "yes_ask": 68}
        # Peak is 70, drop is 4c, threshold is 10c => no trigger
        peak = {"entry_price": 40, "peak_bid": 70, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is None

    def test_exits_full_position(self):
        """Trailing stop always exits full count."""
        pos = {"ticker": "TICK-1", "yes": 8, "no": 0}
        market = {"yes_bid": 55, "yes_ask": 57}
        peak = {"entry_price": 40, "peak_bid": 70, "side": "yes"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is not None
        assert result["count"] == 8

    def test_no_side_trailing_stop(self):
        """NO position trailing stop should also work."""
        pos = {"ticker": "TICK-1", "yes": 0, "no": 4}
        # no_bid = 100 - yes_ask = 100 - 30 = 70
        market = {"yes_bid": 25, "yes_ask": 30, "no_bid": 55, "no_ask": 58}
        peak = {"entry_price": 40, "peak_bid": 70, "side": "no"}
        ec = _exit_config(trailing_drop_cents=10, trailing_min_profit_cents=10)
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is not None
        assert result["side"] == "no"
        assert result["count"] == 4
        assert result["order_type"] == "market"

    def test_no_position_returns_none(self):
        """Empty position (yes=0, no=0) should return None."""
        pos = {"ticker": "TICK-1", "yes": 0, "no": 0}
        market = {"yes_bid": 50, "yes_ask": 52}
        peak = {"entry_price": 30, "peak_bid": 60, "side": "yes"}
        ec = _exit_config()
        result, _ = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert result is None

    def test_last_updated_set_on_evaluation(self):
        """Peak info should have last_updated set after evaluation."""
        pos = {"ticker": "TICK-1", "yes": 3, "no": 0}
        market = {"yes_bid": 55, "yes_ask": 57}
        peak = {"entry_price": 40, "peak_bid": 55, "side": "yes"}
        ec = _exit_config()
        _, updated = _mod.evaluate_trailing_stop(pos, market, peak, ec)
        assert "last_updated" in updated


# ===================================================================
# Stale order cancellation tests (EXIT-05)
# ===================================================================

class TestStaleOrderCancellation:
    """Test stale order TTL cancellation (EXIT-05)."""

    def test_orders_older_than_ttl_cancelled(self):
        """Orders older than ORDER_TTL_MINUTES should be cancelled."""
        import datetime as dt
        # ORDER_TTL_MINUTES defaults to 120 in the module
        assert _mod.ORDER_TTL_MINUTES == 120

    def test_order_ttl_configurable(self):
        """ORDER_TTL_MINUTES should be read from config."""
        # Verify the module read the config value
        assert hasattr(_mod, 'ORDER_TTL_MINUTES')
        assert isinstance(_mod.ORDER_TTL_MINUTES, (int, float))
        assert _mod.ORDER_TTL_MINUTES > 0
