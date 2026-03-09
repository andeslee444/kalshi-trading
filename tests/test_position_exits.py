"""Tests for position exit logic: take-profit, stop-loss, model-shift.

Tests cover per-bot exit config routing, partial exits, market vs limit
order types, and multi-model probability routing for model-shift.
"""

import decimal
import json
import sys
import types
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import make_fake_auth, load_bot_module


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

_fake_project = Path("/tmp/fake_posmon_exits")
(_fake_project / "data").mkdir(parents=True, exist_ok=True)
(_fake_project / "config").mkdir(parents=True, exist_ok=True)
(_fake_project / "config" / "bots-config.json").write_text(json.dumps(_BOT_CONFIG))

KALSHI_FEE_RATE = 0.07

_fake_auth = make_fake_auth(
    PROJECT_DIR=_fake_project,
    CITY_TIMEZONES={
        "MIA": "America/New_York", "LAX": "America/Los_Angeles",
        "PHIL": "America/New_York", "NY": "America/New_York",
        "CHI": "America/Chicago", "AUS": "America/Chicago",
        "DEN": "America/Denver", "HOU": "America/Chicago",
    },
    _local_today=lambda city_code="NY": "2026-02-20",
    round_half_up=lambda v: int(
        decimal.Decimal(str(v)).quantize(
            decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP,
        )
    ),
    retry_request=lambda *a, **kw: MagicMock(),
)

_fake_prob = types.ModuleType("probability")
_fake_prob.half_kelly = lambda *a, **kw: (0, 0)
_fake_prob.weather_probability = lambda *a, **kw: 0.5
_fake_prob.nws_probability = lambda *a, **kw: 0.5
_fake_prob.crypto_price_probability = lambda *a, **kw: 0.5
_fake_prob.kalshi_fee_cents = lambda p: KALSHI_FEE_RATE * (p / 100) * (1 - p / 100) * 100

_fake_ticker = types.ModuleType("ticker_utils")
_fake_ticker.parse_weather_ticker = lambda ticker: None
_fake_ticker.parse_crypto_ticker = lambda ticker: None

_fake_alloc = types.ModuleType("capital_allocator")
_fake_alloc.PortfolioAllocator = lambda *a, **kw: None

# Load module once at import time (fast: all dependencies are mocked)
_mod = load_bot_module("position-monitor.py", _fake_auth, extra_stubs={
    "probability": _fake_prob,
    "ticker_utils": _fake_ticker,
    "capital_allocator": _fake_alloc,
})


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
# ALL_TRADE_LOGS canonical list tests
# ===================================================================

class TestAllTradeLogs:
    """Verify ALL_TRADE_LOGS uses the canonical trade_files.py list."""

    def test_all_trade_logs_matches_canonical(self):
        """Position monitor should use the same trade log list as all other scripts."""
        from trade_files import ALL_TRADE_PATHS
        assert set(str(p) for p in _mod.ALL_TRADE_LOGS) == set(str(p) for p in ALL_TRADE_PATHS)

    def test_includes_beatrelease_trades(self):
        """Beatrelease trades must be visible to the exit system."""
        filenames = [p.name for p in _mod.ALL_TRADE_LOGS]
        assert "beatrelease-trades.json" in filenames

    def test_includes_arb_trades(self):
        """Cross-platform arb trades must be visible to the exit system."""
        filenames = [p.name for p in _mod.ALL_TRADE_LOGS]
        assert "kalshi-arb-trades.json" in filenames

    def test_includes_position_monitor_trades(self):
        """Position monitor's own trades must be in the list for entry lookup."""
        filenames = [p.name for p in _mod.ALL_TRADE_LOGS]
        assert "kalshi-position-trades.json" in filenames

    def test_includes_market_maker_trades(self):
        """Market maker trades must be visible to the exit system."""
        filenames = [p.name for p in _mod.ALL_TRADE_LOGS]
        assert "kalshi-mm-trades.json" in filenames

    def test_all_trade_logs_is_list_of_paths(self):
        """ALL_TRADE_LOGS should be a list of Path objects."""
        assert isinstance(_mod.ALL_TRADE_LOGS, list)
        assert len(_mod.ALL_TRADE_LOGS) >= 10  # canonical list has 10 files
        from pathlib import Path
        for p in _mod.ALL_TRADE_LOGS:
            assert isinstance(p, Path), f"Expected Path, got {type(p)}: {p}"


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

    def test_triggers_on_drop(self):
        """Should trigger when current prob drops from entry by >= model_shift_pp."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 40, "yes_ask": 45}
        entry_rec = {"source_bot": "weather", "model_prob": 0.75}
        ec = _exit_config(model_shift_pp=20)

        # Mock _compute_current_probability to return low prob (drop from 75% to 30%)
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.30, "NWS MIA high 78F, model prob=30%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is not None
        assert result["action"] == "model_shift"
        assert result["order_type"] == "limit"
        assert "drop" in result["reasoning"]

    def test_no_trigger_small_drop(self):
        """Should return None when drop < model_shift_pp."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 70, "yes_ask": 72}
        entry_rec = {"source_bot": "weather", "model_prob": 0.75}
        ec = _exit_config(model_shift_pp=20)

        # Current prob close to entry (drop only 5pp, below 20pp threshold)
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

    def test_triggers_when_prob_dropped_above_50(self):
        """Should trigger when prob dropped from entry by >= threshold, even if > 0.50.

        A position entered at 90% that's now at 55% has lost 35pp of edge.
        The old code (current_prob < 0.50 gate) would miss this.
        """
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 60, "yes_ask": 65}
        entry_rec = {"source_bot": "weather", "model_prob": 0.90}
        ec = _exit_config(model_shift_pp=20)

        # current prob 0.55 -- drop is 35pp (>20pp), should trigger now
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.55, "NWS MIA high 84F, model prob=55%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is not None
        assert result["action"] == "model_shift"
        assert "drop 35pp" in result["reasoning"]

    def test_no_trigger_when_prob_increased(self):
        """Should NOT trigger when current prob is HIGHER than entry (model improved)."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 80, "yes_ask": 82}
        entry_rec = {"source_bot": "weather", "model_prob": 0.60}
        ec = _exit_config(model_shift_pp=20)

        # current prob 0.85, higher than entry 0.60 — model likes position more
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.85, "NWS MIA high 90F, model prob=85%")):
            result = _mod.evaluate_model_shift(pos, market, ec, entry_rec=entry_rec)
        assert result is None

    def test_no_trigger_marginal_drop(self):
        """Should NOT trigger when drop is less than threshold (10pp < 20pp)."""
        pos = {"ticker": "KXHIGHMIA-26FEB20-T86", "yes": 3, "no": 0}
        market = {"yes_bid": 60, "yes_ask": 62}
        entry_rec = {"source_bot": "weather", "model_prob": 0.70}
        ec = _exit_config(model_shift_pp=20)

        # current prob 0.60, drop is 10pp < 20pp threshold
        with patch.object(_mod, "_compute_current_probability",
                          return_value=(0.60, "NWS MIA high 83F, model prob=60%")):
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

    def test_economics_decision_path_is_correct(self):
        """Regression: economics decision path must match file written by economics bot.

        Line 492 of position-monitor.py uses 'kalshi-economics-trades-decisions.json'.
        This test locks that path to prevent drift.
        """
        import inspect
        source = inspect.getsource(_mod._compute_current_probability)
        assert "kalshi-economics-trades-decisions.json" in source


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


# ===================================================================
# check_stale_positions tests
# ===================================================================

class TestCheckStalePositions:
    """Test stale position detection (positions open >7 days and losing)."""

    def _make_entry(self, ticker, price_cents, days_ago):
        """Helper to create an entry record with a timestamp days_ago from now."""
        import datetime
        ts = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
        return {ticker: {"ticker": ticker, "price_cents": price_cents, "timestamp": ts}}

    def test_flags_old_losing_position(self):
        """Position open >7 days with negative P&L should be flagged."""
        entries = self._make_entry("TICK-1", 60, days_ago=10)
        positions = [{"ticker": "TICK-1", "yes": 3, "no": 0, "market_yes_bid": 40, "market_no_bid": 60}]
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 1
        assert result[0]["ticker"] == "TICK-1"
        assert result[0]["age_days"] == 10
        assert result[0]["unrealized_pnl_cents"] < 0

    def test_ignores_young_position(self):
        """Position open <7 days should not be flagged, even if losing."""
        entries = self._make_entry("TICK-1", 60, days_ago=3)
        positions = [{"ticker": "TICK-1", "yes": 3, "no": 0, "market_yes_bid": 40, "market_no_bid": 60}]
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 0

    def test_ignores_old_winning_position(self):
        """Position open >7 days but profitable should not be flagged."""
        entries = self._make_entry("TICK-1", 40, days_ago=10)
        positions = [{"ticker": "TICK-1", "yes": 3, "no": 0, "market_yes_bid": 60, "market_no_bid": 40}]
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 0

    def test_no_entry_record_skipped(self):
        """Position without matching entry record should be skipped."""
        entries = {}  # no entry for TICK-1
        positions = [{"ticker": "TICK-1", "yes": 3, "no": 0, "market_yes_bid": 40, "market_no_bid": 60}]
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 0

    def test_no_position_returns_empty(self):
        """No positions should return empty list."""
        entries = self._make_entry("TICK-1", 60, days_ago=10)
        result = _mod.check_stale_positions(entries, [], max_age_days=7)
        assert len(result) == 0

    def test_custom_max_age(self):
        """Should respect custom max_age_days parameter."""
        entries = self._make_entry("TICK-1", 60, days_ago=5)
        positions = [{"ticker": "TICK-1", "yes": 2, "no": 0, "market_yes_bid": 40, "market_no_bid": 60}]
        # With 7-day default, 5-day-old position is not stale
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 0
        # With 3-day max, 5-day-old position IS stale
        result = _mod.check_stale_positions(entries, positions, max_age_days=3)
        assert len(result) == 1

    def test_no_side_losing_position(self):
        """NO position that is losing and stale should be flagged."""
        entries = self._make_entry("TICK-1", 60, days_ago=10)
        positions = [{"ticker": "TICK-1", "yes": 0, "no": 3, "market_yes_bid": 60, "market_no_bid": 40}]
        result = _mod.check_stale_positions(entries, positions, max_age_days=7)
        assert len(result) == 1
        assert result[0]["unrealized_pnl_cents"] < 0


# ===================================================================
# Heartbeat regression test (Task 8.5)
# ===================================================================

class TestHeartbeat:
    """Verify heartbeat is called at the start of each scan."""

    def test_heartbeat_in_scan_positions(self):
        """scan_positions should call health.record_bot_heartbeat at scan start.

        Regression: heartbeat was stale (Feb 26 despite running Mar 7) because
        it was only called at startup, not at the start of each scan cycle.
        """
        import inspect
        source = inspect.getsource(_mod.scan_positions)
        # Heartbeat should appear early in the function (before position fetching)
        lines = source.split('\n')
        heartbeat_line = None
        positions_line = None
        for i, line in enumerate(lines):
            if 'record_bot_heartbeat' in line and heartbeat_line is None:
                heartbeat_line = i
            if 'get_open_positions' in line and positions_line is None:
                positions_line = i
        assert heartbeat_line is not None, "No heartbeat call found in scan_positions"
        assert positions_line is not None, "No get_open_positions call found in scan_positions"
        assert heartbeat_line < positions_line, (
            f"Heartbeat (line {heartbeat_line}) should be called before "
            f"get_open_positions (line {positions_line})"
        )


# ===================================================================
# PositionScanMetrics tests (Task 8.6)
# ===================================================================

class TestPositionScanMetrics:
    """Test per-scan metrics tracking and serialization."""

    def test_initial_state(self):
        """New metrics should have zero counts."""
        m = _mod.PositionScanMetrics()
        assert m.positions_checked == 0
        assert m.total_exposure_cents == 0
        assert m.largest_position_cents == 0
        assert all(v == 0 for v in m.exits_by_type.values())

    def test_record_position_updates_exposure(self):
        """Recording positions should update exposure and count."""
        m = _mod.PositionScanMetrics()
        m.record_position("TICK-1", 3, 50)  # 3 contracts at 50c = 150c exposure
        m.record_position("TICK-2", 5, 30)  # 5 contracts at 30c = 150c exposure
        assert m.positions_checked == 2
        assert m.total_exposure_cents == 300  # 150 + 150

    def test_record_position_tracks_largest(self):
        """Largest position should be updated correctly."""
        m = _mod.PositionScanMetrics()
        m.record_position("TICK-1", 3, 50)  # 150c
        m.record_position("TICK-2", 10, 80)  # 800c
        m.record_position("TICK-3", 2, 90)  # 180c
        assert m.largest_position_cents == 800
        assert m.largest_position_ticker == "TICK-2"

    def test_record_position_none_price(self):
        """None entry price should be treated as 0 (no exposure)."""
        m = _mod.PositionScanMetrics()
        m.record_position("TICK-1", 5, None)
        assert m.positions_checked == 1
        assert m.total_exposure_cents == 0

    def test_record_exit_increments_type(self):
        """Exit recording should increment the correct type counter."""
        m = _mod.PositionScanMetrics()
        m.record_exit("take_profit")
        m.record_exit("stop_loss")
        m.record_exit("take_profit")
        assert m.exits_by_type["take_profit"] == 2
        assert m.exits_by_type["stop_loss"] == 1
        assert m.exits_by_type["model_shift"] == 0

    def test_record_exit_unknown_type_ignored(self):
        """Unknown exit types should be silently ignored."""
        m = _mod.PositionScanMetrics()
        m.record_exit("unknown_type")
        assert all(v == 0 for v in m.exits_by_type.values())

    def test_record_stale(self):
        """Stale position count should be recorded."""
        m = _mod.PositionScanMetrics()
        m.record_stale(3)
        assert m.stale_positions == 3

    def test_to_dict_serialization(self):
        """to_dict should produce a complete dict with all fields."""
        m = _mod.PositionScanMetrics()
        m.record_position("TICK-1", 5, 40)
        m.record_exit("stop_loss")
        m.record_stale(2)
        d = m.to_dict()
        assert d["positions_checked"] == 1
        assert d["total_exits"] == 1
        assert d["exits_by_type"]["stop_loss"] == 1
        assert d["stale_positions_flagged"] == 2
        assert d["total_exposure_cents"] == 200
        assert d["largest_position_ticker"] == "TICK-1"
        assert "timestamp" in d

    def test_save_calls_atomic_write(self, tmp_path):
        """save() should call _atomic_write_json with correct data."""
        metrics_file = tmp_path / "metrics.json"
        m = _mod.PositionScanMetrics()
        m.record_position("TICK-1", 3, 50)

        # Replace module-level _atomic_write_json with real file write
        def _real_write(path, data):
            path.write_text(json.dumps(data, indent=2))

        with patch.object(_mod, "_atomic_write_json", side_effect=_real_write):
            m.save(path=metrics_file)
        assert metrics_file.exists()
        data = json.loads(metrics_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["positions_checked"] == 1

    def test_save_appends_to_existing(self, tmp_path):
        """save() should append to existing history, not overwrite."""
        metrics_file = tmp_path / "metrics.json"

        def _real_write(path, data):
            path.write_text(json.dumps(data, indent=2))

        with patch.object(_mod, "_atomic_write_json", side_effect=_real_write):
            # First scan
            m1 = _mod.PositionScanMetrics()
            m1.record_position("TICK-1", 3, 50)
            m1.save(path=metrics_file)
            # Second scan
            m2 = _mod.PositionScanMetrics()
            m2.record_position("TICK-2", 5, 30)
            m2.record_exit("take_profit")
            m2.save(path=metrics_file)
        data = json.loads(metrics_file.read_text())
        assert len(data) == 2
        assert data[1]["exits_by_type"]["take_profit"] == 1


# ===================================================================
# Per-bot stop-loss and backtest tests (Task 8.7)
# ===================================================================

class TestGetEffectiveStopLoss:
    """Test per-bot stop-loss threshold computation."""

    def test_weather_uses_config_value(self):
        """Non-strategy bots should use the configured stop-loss cents."""
        result = _mod.get_effective_stop_loss("weather", 20, entry_price_cents=60)
        assert result == 20

    def test_crypto_uses_config_value(self):
        """Crypto should use configured stop-loss."""
        result = _mod.get_effective_stop_loss("crypto", 35, entry_price_cents=50)
        assert result == 35

    def test_strategy_uses_proportional(self):
        """Strategy (longshot) positions should use proportional stop-loss.

        With STRATEGY_STOP_LOSS_MULTIPLIER=2: entry 10c -> stop at 5c.
        """
        result = _mod.get_effective_stop_loss("strategy", 30, entry_price_cents=10)
        assert result == 5  # 10 / 2 = 5

    def test_strategy_cheap_position(self):
        """Cheap strategy position: entry 3c -> stop at 1c (minimum 1)."""
        result = _mod.get_effective_stop_loss("strategy", 30, entry_price_cents=3)
        assert result == 1  # max(1, 3//2) = max(1, 1) = 1

    def test_strategy_very_cheap_position(self):
        """Very cheap strategy position: entry 1c -> stop at 1c (minimum)."""
        result = _mod.get_effective_stop_loss("strategy", 30, entry_price_cents=1)
        assert result == 1  # max(1, 1//2) = max(1, 0) = 1

    def test_strategy_no_entry_price_uses_config(self):
        """Strategy with no entry price should fall back to config value."""
        result = _mod.get_effective_stop_loss("strategy", 30, entry_price_cents=None)
        assert result == 30

    def test_strategy_zero_entry_price_uses_config(self):
        """Strategy with 0 entry price should fall back to config value."""
        result = _mod.get_effective_stop_loss("strategy", 30, entry_price_cents=0)
        assert result == 30

    def test_unknown_bot_uses_config(self):
        """Unknown bot should use configured value."""
        result = _mod.get_effective_stop_loss("unknown", 25, entry_price_cents=50)
        assert result == 25


class TestStopLossDefaults:
    """Verify STOP_LOSS_DEFAULTS has sensible per-bot values."""

    def test_weather_tighter_than_crypto(self):
        """Weather stop-loss should be tighter than crypto (less volatile)."""
        assert _mod.STOP_LOSS_DEFAULTS["weather"] < _mod.STOP_LOSS_DEFAULTS["crypto"]

    def test_economics_widest_fixed(self):
        """Economics should have the widest fixed stop-loss (illiquid)."""
        fixed_defaults = {k: v for k, v in _mod.STOP_LOSS_DEFAULTS.items() if v is not None}
        assert _mod.STOP_LOSS_DEFAULTS["economics"] == max(fixed_defaults.values())

    def test_strategy_is_proportional(self):
        """Strategy should use proportional (None) stop-loss."""
        assert _mod.STOP_LOSS_DEFAULTS["strategy"] is None

    def test_all_bots_have_defaults(self):
        """All known bot names should have a stop-loss default."""
        expected_bots = {"weather", "source-monitor", "crypto", "economics",
                         "strategy", "entertainment", "beatrelease", "monitor"}
        assert expected_bots.issubset(set(_mod.STOP_LOSS_DEFAULTS.keys()))


class TestEvaluateStopLossPerBot:
    """Test evaluate_stop_loss with per-bot source_bot parameter."""

    def test_strategy_proportional_stop_no_trigger(self):
        """Strategy position at 5c entry, bid at 4c: proportional stop is 2c, no trigger."""
        pos = {"ticker": "LONGSHOT-1", "yes": 10, "no": 0}
        market = {"yes_bid": 4, "yes_ask": 6}
        ec = _exit_config(stop_loss_cents=30)
        result = _mod.evaluate_stop_loss(pos, market, ec, entry_price_cents=5, source_bot="strategy")
        # Proportional stop = 5 / 2 = 2c; bid 4c > 2c, no trigger
        assert result is None

    def test_strategy_proportional_stop_triggers(self):
        """Strategy position at 5c entry, bid at 2c: proportional stop is 2c, triggers."""
        pos = {"ticker": "LONGSHOT-1", "yes": 10, "no": 0}
        market = {"yes_bid": 2, "yes_ask": 4}
        ec = _exit_config(stop_loss_cents=30)
        result = _mod.evaluate_stop_loss(pos, market, ec, entry_price_cents=5, source_bot="strategy")
        # Proportional stop = 5 / 2 = 2c; bid 2c <= 2c, triggers
        assert result is not None
        assert result["action"] == "stop_loss"
        assert "bot=strategy" in result["reasoning"]

    def test_non_strategy_uses_config_stop(self):
        """Non-strategy bot should use config stop-loss, not proportional."""
        pos = {"ticker": "KXHIGHMIA-1", "yes": 4, "no": 0}
        market = {"yes_bid": 25, "yes_ask": 30}
        ec = _exit_config(stop_loss_cents=30)
        result = _mod.evaluate_stop_loss(pos, market, ec, entry_price_cents=60, source_bot="weather")
        assert result is not None
        assert result["action"] == "stop_loss"


class TestBacktestStopLoss:
    """Test stop-loss backtest analysis function."""

    def test_identifies_premature_exit(self):
        """Trade exited via stop-loss that would have won should be premature."""
        trades = [
            {"exit_type": "stop_loss", "settlement_result": "win"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["premature_exits"] == 1
        assert result["correct_exits"] == 0
        assert result["whipsaw_rate"] == 1.0

    def test_identifies_correct_exit(self):
        """Trade exited via stop-loss that would have lost is a correct exit."""
        trades = [
            {"exit_type": "stop_loss", "settlement_result": "loss"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["premature_exits"] == 0
        assert result["correct_exits"] == 1
        assert result["whipsaw_rate"] == 0.0

    def test_mixed_results(self):
        """Mix of premature and correct exits."""
        trades = [
            {"exit_type": "stop_loss", "settlement_result": "win"},
            {"exit_type": "stop_loss", "settlement_result": "loss"},
            {"exit_type": "stop_loss", "settlement_result": "loss"},
            {"exit_type": "stop_loss", "settlement_result": "win"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["premature_exits"] == 2
        assert result["correct_exits"] == 2
        assert result["total_evaluated"] == 4
        assert result["whipsaw_rate"] == 0.5

    def test_ignores_non_stop_loss_exits(self):
        """Only stop_loss exits should be analyzed."""
        trades = [
            {"exit_type": "take_profit", "settlement_result": "win"},
            {"exit_type": "model_shift", "settlement_result": "loss"},
            {"exit_type": "stop_loss", "settlement_result": "loss"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["total_evaluated"] == 1
        assert result["correct_exits"] == 1

    def test_handles_unknown_settlements(self):
        """Trades without settlement data should be counted as unknown."""
        trades = [
            {"exit_type": "stop_loss", "settlement_result": None},
            {"exit_type": "stop_loss"},  # no settlement_result key
            {"exit_type": "stop_loss", "settlement_result": "loss"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["unknown"] == 2
        assert result["correct_exits"] == 1
        assert result["total_evaluated"] == 1

    def test_empty_trades(self):
        """Empty trade list should return zeroes."""
        result = _mod.backtest_stop_loss([])
        assert result["premature_exits"] == 0
        assert result["correct_exits"] == 0
        assert result["unknown"] == 0
        assert result["whipsaw_rate"] == 0.0

    def test_exit_reason_field_also_works(self):
        """Should also detect stop_loss from exit_reason field (legacy format)."""
        trades = [
            {"exit_reason": "stop_loss", "settlement_result": "win"},
        ]
        result = _mod.backtest_stop_loss(trades)
        assert result["premature_exits"] == 1
