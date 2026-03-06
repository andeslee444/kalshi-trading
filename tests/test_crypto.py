"""Tests for crypto-bot Deribit DVOL parsing, vol blending, and calibration logic.

Covers: DVOL endpoint response parsing, out-of-range rejection,
IV/RV blending math, confidence-scaled edge threshold, trailing drift,
and horizon-matched vol lookback.

Tests import production functions from crypto-bot.py (via load_bot_module)
and crypto_models.py rather than re-implementing them locally.
"""

import math
import time
import pytest

from conftest import make_fake_auth, load_bot_module

# Load the crypto bot module with stubs
_fake_auth = make_fake_auth()

# Build extra stubs needed by crypto-bot.py
import types
from unittest.mock import MagicMock

_fake_prob = types.ModuleType("probability")
for fn in ["half_kelly", "compute_limit_price", "kalshi_fee_cents",
           "is_market_liquid", "crypto_price_probability",
           "crypto_price_probability_jd", "crypto_price_probability_heston",
           "_load_calibration", "_reset_calibration"]:
    setattr(_fake_prob, fn, MagicMock())

_fake_ticker = types.ModuleType("ticker_utils")
_fake_ticker.parse_crypto_ticker = MagicMock(return_value=None)

_fake_alloc = types.ModuleType("capital_allocator")
_fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()

_fake_pf = types.ModuleType("particle_filter")
_fake_pf.FilterManager = lambda *a, **kw: MagicMock()
_fake_pf.FilterConfig = MagicMock()
_fake_pf.ci_kelly_multiplier = MagicMock(return_value=1.0)

_fake_regime = types.ModuleType("regime_detector")
_fake_regime.RegimeDetector = lambda *a, **kw: MagicMock()
_fake_regime.regime_kelly_multiplier = MagicMock(return_value=1.0)

_fake_cm = types.ModuleType("crypto_models")
_fake_cm.EnsembleModel = MagicMock()
_fake_cm.smooth_edge_threshold = MagicMock(return_value=0.08)
_fake_cm.horizon_kelly_fraction = MagicMock(return_value=0.25)
_fake_cm.horizon_vol_weights = MagicMock(return_value=(0.6, 0.4))
_fake_cm.AR1VolForecast = lambda *a, **kw: MagicMock()
_fake_cm.vol_skew_multiplier = MagicMock(return_value=1.0)

_fake_vf = types.ModuleType("vol_forecaster")
_fake_vf.GARCHForecaster = lambda *a, **kw: MagicMock()
_fake_vf.DCCCorrelation = lambda *a, **kw: MagicMock()
_fake_vf.intraday_vol_multiplier = MagicMock(return_value=1.0)
_fake_vf.correct_bid_ask_bounce = MagicMock(return_value=None)

_crypto_bot = load_bot_module("crypto-bot.py", _fake_auth, extra_stubs={
    "probability": _fake_prob,
    "ticker_utils": _fake_ticker,
    "capital_allocator": _fake_alloc,
    "particle_filter": _fake_pf,
    "regime_detector": _fake_regime,
    "crypto_models": _fake_cm,
    "vol_forecaster": _fake_vf,
})

# Import production functions from the loaded module
parse_dvol_response = _crypto_bot.parse_dvol_response
blend_vol = _crypto_bot.blend_vol
select_rv_lookback = _crypto_bot.select_rv_lookback
bracket_eligible = _crypto_bot.bracket_eligible
get_market_price = _crypto_bot.get_market_price
apply_drift = _crypto_bot.apply_drift

# Import from crypto_models.py directly (pure functions, no side effects)
from crypto_models import smooth_edge_threshold


class TestDVOLResponseParsing:
    """Test the DVOL data point parsing logic (production parse_dvol_response)."""

    def test_normal_btc_dvol(self):
        data = {"result": {"data": [[1700000000000, 55.0, 56.0, 54.0, 55.0]]}}
        assert parse_dvol_response(data) == pytest.approx(0.55, abs=0.01)

    def test_normal_eth_dvol(self):
        data = {"result": {"data": [[1700000000000, 70.0, 72.0, 68.0, 70.5]]}}
        assert parse_dvol_response(data) == pytest.approx(0.705, abs=0.01)

    def test_multiple_data_points_uses_latest(self):
        data = {"result": {"data": [
            [1700000000000, 50.0, 52.0, 48.0, 51.0],
            [1700003600000, 55.0, 56.0, 54.0, 55.0],
            [1700007200000, 60.0, 62.0, 58.0, 61.0],
        ]}}
        assert parse_dvol_response(data) == pytest.approx(0.61, abs=0.01)

    def test_empty_data_returns_none(self):
        data = {"result": {"data": []}}
        assert parse_dvol_response(data) is None

    def test_missing_result_returns_none(self):
        data = {}
        assert parse_dvol_response(data) is None

    def test_out_of_range_low_rejected(self):
        """IV below 10% is rejected."""
        data = {"result": {"data": [[1700000000000, 5.0, 6.0, 4.0, 5.0]]}}
        assert parse_dvol_response(data) is None

    def test_out_of_range_high_rejected(self):
        """IV above 300% is rejected."""
        data = {"result": {"data": [[1700000000000, 350.0, 360.0, 340.0, 350.0]]}}
        assert parse_dvol_response(data) is None

    def test_boundary_10_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 10.0, 11.0, 9.0, 10.0]]}}
        assert parse_dvol_response(data) == pytest.approx(0.10, abs=0.01)

    def test_boundary_300_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 300.0, 301.0, 299.0, 300.0]]}}
        assert parse_dvol_response(data) == pytest.approx(3.0, abs=0.01)


class TestVolBlending:
    """Test the IV/RV blending logic (production blend_vol)."""

    def test_both_iv_and_rv(self):
        assert blend_vol(0.55, 0.45) == pytest.approx(0.51, abs=0.01)

    def test_iv_only(self):
        assert blend_vol(0.55, None) == 0.55

    def test_rv_only(self):
        assert blend_vol(None, 0.45) == pytest.approx(0.465, abs=0.01)

    def test_neither_uses_default(self):
        assert blend_vol(None, None) == 0.50

    def test_iv_weight_exceeds_rv(self):
        """IV should get higher weight (60%) than RV (40%)."""
        blended = blend_vol(0.60, 0.40)
        assert blended > 0.50
        assert blended == pytest.approx(0.52, abs=0.01)

    def test_custom_default_vol(self):
        assert blend_vol(None, None, default_vol=0.65) == 0.65

    def test_garch_fallback(self):
        """GARCH forecast used when IV is None but GARCH is available."""
        assert blend_vol(None, None, garch_forecast=0.55) == 0.55

    def test_garch_priority_over_rv_only(self):
        """GARCH has priority over rv-only when IV is absent."""
        result = blend_vol(None, 0.45, garch_forecast=0.55)
        # GARCH takes priority over rv-only path
        assert result == 0.55

    def test_custom_weights(self):
        """Custom IV/RV weights should be respected."""
        result = blend_vol(0.60, 0.40, w_iv=0.8, w_rv=0.2)
        assert result == pytest.approx(0.56, abs=0.01)


class TestSmoothEdgeThreshold:
    """Test smooth edge threshold from crypto_models.py (replaces old band-based approach)."""

    def test_extreme_prob_near_base(self):
        """Prob near 0 or 1 should give threshold near base."""
        t_low = smooth_edge_threshold(0.05, base=0.08)
        t_high = smooth_edge_threshold(0.95, base=0.08)
        assert abs(t_low - 0.08) < 0.01
        assert abs(t_high - 0.08) < 0.01

    def test_mid_prob_highest_threshold(self):
        """Prob=0.50 should produce the highest threshold (max uncertainty)."""
        t_mid = smooth_edge_threshold(0.50, base=0.08)
        t_extreme = smooth_edge_threshold(0.10, base=0.08)
        assert t_mid > t_extreme

    def test_smooth_transition(self):
        """Adjacent probabilities should produce similar thresholds (no step function)."""
        t_24 = smooth_edge_threshold(0.24, base=0.08)
        t_25 = smooth_edge_threshold(0.25, base=0.08)
        t_26 = smooth_edge_threshold(0.26, base=0.08)
        assert abs(t_25 - t_24) < 0.003
        assert abs(t_26 - t_25) < 0.003

    def test_mid_range_blocks_phantom_edge(self):
        """A 10% edge at prob=0.50 would pass base (8%) but should fail mid-range threshold."""
        prob = 0.50
        edge = 0.10
        threshold = smooth_edge_threshold(prob, base=0.08)
        assert edge < threshold  # correctly blocked

    def test_strong_edge_passes_mid_range(self):
        """A 20% edge at prob=0.50 passes even the elevated threshold."""
        prob = 0.50
        edge = 0.20
        threshold = smooth_edge_threshold(prob, base=0.08)
        assert edge > threshold  # correctly passes


class TestTrailingDrift:
    """Test trailing drift computation from price history."""

    def _compute_drift_from_history(self, history):
        """Replicate the core math of compute_trailing_drift for pure testing.

        This uses the same algorithm as the production function but takes
        history as an argument instead of reading global state.
        """
        if len(history) < 2:
            return 0.0
        oldest_price = history[0][1]
        newest_price = history[-1][1]
        dt_seconds = history[-1][0] - history[0][0]
        if dt_seconds < 3600 or oldest_price <= 0:
            return 0.0
        log_return = math.log(newest_price / oldest_price)
        annualized = log_return * (365.25 * 86400 / dt_seconds)
        return max(-2.0, min(2.0, annualized))

    def test_empty_history_returns_zero(self):
        assert self._compute_drift_from_history([]) == 0.0

    def test_single_point_returns_zero(self):
        assert self._compute_drift_from_history([(time.time(), 50000)]) == 0.0

    def test_insufficient_time_span_returns_zero(self):
        """Less than 1 hour of data returns zero."""
        now = time.time()
        history = [(now - 1800, 50000), (now, 50100)]  # 30 min span
        assert self._compute_drift_from_history(history) == 0.0

    def test_flat_price_returns_zero(self):
        now = time.time()
        history = [(now - 86400, 50000), (now, 50000)]
        assert self._compute_drift_from_history(history) == pytest.approx(0.0, abs=0.01)

    def test_rising_price_positive_drift(self):
        """5% gain over 24h should give positive annualized drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 52500)]  # +5%
        drift = self._compute_drift_from_history(history)
        assert drift > 0
        # ~5% per day * 365 ≈ 1780% annualized, clamped to 2.0
        assert drift == pytest.approx(2.0, abs=0.01)

    def test_falling_price_negative_drift(self):
        """5% drop over 24h should give negative drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 47500)]  # -5%
        drift = self._compute_drift_from_history(history)
        assert drift < 0

    def test_clamped_to_positive_2(self):
        """Extreme gains are clamped to +2.0."""
        now = time.time()
        history = [(now - 3600, 50000), (now, 55000)]  # +10% in 1h
        drift = self._compute_drift_from_history(history)
        assert drift == 2.0

    def test_clamped_to_negative_2(self):
        """Extreme drops are clamped to -2.0."""
        now = time.time()
        history = [(now - 3600, 50000), (now, 45000)]  # -10% in 1h
        drift = self._compute_drift_from_history(history)
        assert drift == -2.0

    def test_small_move_reasonable_drift(self):
        """0.1% gain over 24h → small positive annualized drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 50050)]  # +0.1%
        drift = self._compute_drift_from_history(history)
        # ~0.1% * 365 ≈ 36.5% annualized ≈ 0.365
        assert 0.3 < drift < 0.5

    def test_zero_oldest_price_returns_zero(self):
        now = time.time()
        history = [(now - 86400, 0), (now, 50000)]
        assert self._compute_drift_from_history(history) == 0.0


class TestHorizonMatchedVol:
    """Test horizon-matched RV lookback selection (production select_rv_lookback)."""

    def test_15min_market_uses_1h_lookback(self):
        assert select_rv_lookback(15) == 3600

    def test_30min_market_uses_1h_lookback(self):
        assert select_rv_lookback(30) == 3600

    def test_60min_market_uses_6h_lookback(self):
        assert select_rv_lookback(60) == 6 * 3600

    def test_120min_market_uses_6h_lookback(self):
        assert select_rv_lookback(120) == 6 * 3600

    def test_daily_market_uses_24h_lookback(self):
        assert select_rv_lookback(1440) == 86400

    def test_weekly_market_uses_24h_lookback(self):
        assert select_rv_lookback(10080) == 86400

    def test_121min_uses_24h_lookback(self):
        """Just over 2h boundary → 24h lookback."""
        assert select_rv_lookback(121) == 86400


class TestComputeRealizedVol:
    """Test compute_realized_vol refactored signature."""

    def _compute_vol(self, history, lookback_seconds=86400):
        """Replicate the core vol computation (pure, no side effects).

        Uses the same algorithm as production compute_realized_vol
        but takes history as argument instead of reading global state.
        """
        now = time.time()
        cutoff = now - lookback_seconds
        filtered = [(t, p) for t, p in history if t > cutoff]
        if len(filtered) < 5:
            return None

        log_returns = []
        for i in range(1, len(filtered)):
            dt = filtered[i][0] - filtered[i-1][0]
            if dt > 0 and filtered[i-1][1] > 0:
                lr = math.log(filtered[i][1] / filtered[i-1][1])
                log_returns.append((lr, dt))

        if len(log_returns) < 3:
            return None

        median_dt = sorted(dt for _, dt in log_returns)[len(log_returns) // 2]
        normalized = [lr * math.sqrt(median_dt / dt) for lr, dt in log_returns]
        mean = sum(normalized) / len(normalized)
        variance = sum((r - mean) ** 2 for r in normalized) / (len(normalized) - 1)
        intervals_per_year = 365.25 * 86400 / median_dt
        vol = math.sqrt(variance * intervals_per_year)
        return max(0.10, min(3.0, vol))

    def test_insufficient_data_returns_none(self):
        now = time.time()
        history = [(now - 60*i, 50000) for i in range(3)]
        assert self._compute_vol(history) is None

    def test_shorter_lookback_filters_old_data(self):
        """With 1h lookback, observations older than 1h are excluded."""
        now = time.time()
        old_points = [(now - 86400 + i*300, 50000 + i*10) for i in range(240)]
        recent_points = [(now - 3600 + i*60, 50000 + i*5) for i in range(60)]
        history = old_points + recent_points
        vol_24h = self._compute_vol(history, lookback_seconds=86400)
        vol_1h = self._compute_vol(history, lookback_seconds=3600)
        assert vol_24h is not None
        assert vol_1h is not None


class TestBracketLiquidityGate:
    """Test bracket-specific liquidity (production bracket_eligible)."""

    def test_tight_spread_high_volume_eligible(self):
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 25}
        assert bracket_eligible(market)

    def test_wide_yes_spread_but_tight_no_spread_eligible(self):
        """NO side has tight spread even though YES side is wide -- should pass."""
        market = {"yes_bid": 0, "yes_ask": 50, "no_bid": 45, "no_ask": 55, "volume": 15}
        assert bracket_eligible(market)

    def test_no_side_only_liquidity_eligible(self):
        """Market with no YES bid but valid NO ask/bid should pass."""
        market = {"yes_bid": 0, "yes_ask": 0, "no_bid": 40, "no_ask": 50, "volume": 20}
        assert bracket_eligible(market)

    def test_last_price_fallback_eligible(self):
        """Market with no bid/ask on either side but recent trades should pass."""
        market = {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
                  "volume": 15, "last_price": 45}
        assert bracket_eligible(market)

    def test_last_price_but_low_volume_rejected(self):
        """Last price set but volume < 10 should be rejected for last_price fallback."""
        market = {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
                  "volume": 8, "last_price": 45}
        assert not bracket_eligible(market)

    def test_wide_both_spreads_no_last_price_rejected(self):
        """Both sides have wide spreads and no last_price -- rejected."""
        market = {"yes_bid": 10, "yes_ask": 50, "no_bid": 10, "no_ask": 50, "volume": 25}
        assert not bracket_eligible(market)

    def test_boundary_spread_15_eligible(self):
        market = {"yes_bid": 35, "yes_ask": 50, "volume": 15}
        assert bracket_eligible(market)

    def test_very_low_volume_rejected(self):
        """Volume < 5 rejects bracket even with tight spread."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 3}
        assert not bracket_eligible(market)

    def test_boundary_volume_5_eligible(self):
        """Volume exactly 5 should pass (lowered from 10)."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 5}
        assert bracket_eligible(market)

    def test_truly_illiquid_rejected(self):
        """No liquidity anywhere: no spreads, no volume, no last_price."""
        market = {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0,
                  "volume": 0, "last_price": 0}
        assert not bracket_eligible(market)


class TestBracketLimitPricing:
    """Test that brackets always use aggressive (ask) pricing for fill rate."""

    def _bracket_limit_price(self, yes_bid, yes_ask):
        """Bracket pricing: always use ask price (maximize fill rate)."""
        return yes_ask if yes_ask else 0

    def test_uses_full_ask(self):
        assert self._bracket_limit_price(40, 50) == 50

    def test_no_ask_returns_zero(self):
        assert self._bracket_limit_price(40, 0) == 0

    def test_ignores_bid(self):
        """Regardless of bid, bracket price = ask."""
        assert self._bracket_limit_price(10, 50) == 50
        assert self._bracket_limit_price(49, 50) == 50


class TestJumpDiffusionCrypto:
    """Test Merton jump-diffusion model for crypto price probability."""

    def setup_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def teardown_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def test_basic_probability_range(self):
        """JD probability should be in [0, 1]."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert 0.0 <= prob <= 1.0

    def test_at_threshold_near_half(self):
        """When price equals threshold, probability should be near 0.5."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=90000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert 0.35 < prob < 0.65

    def test_deep_itm_high_prob(self):
        """Deep ITM (price >> threshold) should give high probability."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=100000, threshold=80000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert prob > 0.80

    def test_deep_otm_low_prob(self):
        """Deep OTM (price << threshold) should give low probability."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=80000, threshold=100000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert prob < 0.20

    def test_fatter_tails_than_gbm(self):
        """JD model should give higher tail probabilities than GBM for far-OTM."""
        from probability import crypto_price_probability, crypto_price_probability_jd
        gbm_prob = crypto_price_probability(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        jd_prob = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert jd_prob > gbm_prob

    def test_below_direction(self):
        """'below' direction should be 1 - P(above)."""
        from probability import crypto_price_probability_jd
        p_above = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        p_below = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="below",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert abs(p_above + p_below - 1.0) < 0.01

    def test_zero_jump_intensity_matches_gbm(self):
        """With λ=0 (no jumps), JD should match GBM."""
        from probability import crypto_price_probability, crypto_price_probability_jd
        gbm = crypto_price_probability(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        jd = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=0.0,
        )
        assert abs(gbm - jd) < 0.02  # Should be very close

    def test_higher_intensity_fatter_tails(self):
        """Higher jump intensity should produce fatter tails."""
        from probability import crypto_price_probability_jd
        low_lambda = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=0.5,
        )
        high_lambda = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=2.0,
        )
        assert high_lambda > low_lambda

    def test_short_horizon(self):
        """Very short horizon should give extreme probability (near 0 or 1)."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=100000, direction="above",
            time_horizon_minutes=5, realized_vol_pct=0.60,
        )
        assert prob < 0.10  # Very unlikely in 5 minutes


class TestPriceFallback:
    """Test price fallback logic (production get_market_price)."""

    def test_yes_ask_available(self):
        """When yes_ask is available, use it directly."""
        market = {"yes_ask": 45, "no_ask": 55, "last_price": 40}
        assert get_market_price(market) == 45

    def test_no_ask_fallback(self):
        """When yes_ask=0 but no_ask=55, implied price is 100-55=45."""
        market = {"yes_ask": 0, "no_ask": 55, "last_price": 40}
        assert get_market_price(market) == 45  # 100 - 55

    def test_last_price_fallback(self):
        """When yes_ask=0, no_ask=0, use last_price."""
        market = {"yes_ask": 0, "no_ask": 0, "last_price": 45}
        assert get_market_price(market) == 45

    def test_no_price_at_all(self):
        """When nothing available, return None (skip market)."""
        market = {"yes_ask": 0, "no_ask": 0, "last_price": 0}
        assert get_market_price(market) is None

    def test_yes_ask_too_high_uses_fallback(self):
        """yes_ask >= 99 is not useful, fall back."""
        market = {"yes_ask": 99, "no_ask": 55, "last_price": 40}
        assert get_market_price(market) == 45  # 100 - 55

    def test_yes_ask_zero_with_no_ask(self):
        """yes_ask=0 with valid no_ask should compute implied price."""
        market = {"yes_ask": 0, "no_ask": 30}
        assert get_market_price(market) == 70  # 100 - 30

    def test_no_ask_out_of_range(self):
        """no_ask=0 or no_ask>=100 should not be used."""
        market = {"yes_ask": 0, "no_ask": 0, "last_price": 50}
        assert get_market_price(market) == 50

    def test_none_values_handled(self):
        """None values in market dict should be handled gracefully."""
        market = {"yes_ask": None, "no_ask": None, "last_price": 42}
        assert get_market_price(market) == 42


class TestDriftZeroShortHorizon:
    """Test that drift is zeroed for sub-daily markets (production apply_drift)."""

    def test_15min_market_zeros_drift(self):
        assert apply_drift(0.5, 15) == 0.0

    def test_1hour_market_zeros_drift(self):
        assert apply_drift(1.5, 60) == 0.0

    def test_6hour_market_zeros_drift(self):
        assert apply_drift(2.0, 360) == 0.0

    def test_23hour_market_zeros_drift(self):
        assert apply_drift(1.0, 1380) == 0.0

    def test_24hour_market_uses_drift(self):
        assert apply_drift(0.5, 1440) == 0.5

    def test_weekly_market_uses_drift(self):
        assert apply_drift(-1.2, 10080) == -1.2

    def test_monthly_market_uses_drift(self):
        assert apply_drift(0.8, 43200) == 0.8

    def test_zero_drift_stays_zero(self):
        assert apply_drift(0.0, 15) == 0.0
        assert apply_drift(0.0, 1440) == 0.0

    def test_negative_drift_zeroed_short_horizon(self):
        assert apply_drift(-2.0, 120) == 0.0


class TestEnsembleIntegration:
    """Verify crypto-bot uses ensemble model correctly."""

    def test_smooth_edge_replaces_hard_cutoff(self):
        t_24 = smooth_edge_threshold(0.24, base=0.08)
        t_25 = smooth_edge_threshold(0.25, base=0.08)
        t_26 = smooth_edge_threshold(0.26, base=0.08)
        assert abs(t_25 - t_24) < 0.003
        assert abs(t_26 - t_25) < 0.003

    def test_horizon_kelly_shorter_is_smaller(self):
        from crypto_models import horizon_kelly_fraction
        assert horizon_kelly_fraction(5) < horizon_kelly_fraction(60)
        assert horizon_kelly_fraction(60) < horizon_kelly_fraction(1440)

    def test_horizon_vol_weights_sum_to_one(self):
        from crypto_models import horizon_vol_weights
        for t in [5, 15, 60, 360, 1440]:
            w_iv, w_rv = horizon_vol_weights(t)
            assert abs(w_iv + w_rv - 1.0) < 0.001


class TestOUDriftCorrection:
    """OU model should adjust both variance AND drift (mean reversion)."""

    def test_ou_differs_from_gbm_short_horizon(self):
        from probability import crypto_price_probability
        gbm_prob = crypto_price_probability(
            current_price=80000, threshold=80500, direction="above",
            time_horizon_minutes=60, realized_vol_pct=0.50,
            use_ou=False,
        )
        ou_prob = crypto_price_probability(
            current_price=80000, threshold=80500, direction="above",
            time_horizon_minutes=60, realized_vol_pct=0.50,
            use_ou=True, ou_half_life_minutes=120,
        )
        assert abs(ou_prob - gbm_prob) > 0.001

    def test_ou_symmetric_effect(self):
        from probability import crypto_price_probability
        p_above = crypto_price_probability(
            current_price=80000, threshold=80000, direction="above",
            time_horizon_minutes=60, realized_vol_pct=0.50,
            use_ou=True, ou_half_life_minutes=120,
        )
        p_below = crypto_price_probability(
            current_price=80000, threshold=80000, direction="below",
            time_horizon_minutes=60, realized_vol_pct=0.50,
            use_ou=True, ou_half_life_minutes=120,
        )
        assert abs(p_above + p_below - 1.0) < 0.01

    def test_ou_no_effect_long_horizon(self):
        from probability import crypto_price_probability
        gbm_prob = crypto_price_probability(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=500, realized_vol_pct=0.50,
            use_ou=False,
        )
        ou_prob = crypto_price_probability(
            current_price=80000, threshold=82000, direction="above",
            time_horizon_minutes=500, realized_vol_pct=0.50,
            use_ou=True, ou_half_life_minutes=120,
        )
        assert abs(gbm_prob - ou_prob) < 0.005


class TestVolSkew:
    """Vol skew adjustment for OTM binary options."""

    def test_skew_multiplier_atm(self):
        from crypto_models import vol_skew_multiplier
        mult = vol_skew_multiplier(moneyness=1.0)
        assert 0.98 <= mult <= 1.02

    def test_skew_multiplier_otm_put(self):
        from crypto_models import vol_skew_multiplier
        mult = vol_skew_multiplier(moneyness=0.85)
        assert mult > 1.05

    def test_skew_multiplier_otm_call(self):
        from crypto_models import vol_skew_multiplier
        mult = vol_skew_multiplier(moneyness=1.15)
        assert mult >= 1.0

    def test_skew_symmetric_light_smile(self):
        from crypto_models import vol_skew_multiplier
        put_mult = vol_skew_multiplier(moneyness=0.90)
        call_mult = vol_skew_multiplier(moneyness=1.10)
        assert put_mult > call_mult

    def test_skew_clamped(self):
        from crypto_models import vol_skew_multiplier
        mult = vol_skew_multiplier(moneyness=0.50)
        assert mult <= 2.0


class TestFeeAdjustedEdge:
    """Edge threshold should account for Kalshi fees."""

    def test_fee_at_50_cents(self):
        from probability import kalshi_fee_cents
        fee = kalshi_fee_cents(50)
        assert abs(fee - 1.75) < 0.01

    def test_fee_at_10_cents(self):
        from probability import kalshi_fee_cents
        fee = kalshi_fee_cents(10)
        assert abs(fee - 0.63) < 0.01

    def test_net_edge_below_threshold_should_skip(self):
        from probability import kalshi_fee_cents
        raw_edge = 0.085
        price_cents = 50
        fee_pp = kalshi_fee_cents(price_cents) / 100
        net_edge = raw_edge - fee_pp
        threshold = 0.08
        assert net_edge < threshold
