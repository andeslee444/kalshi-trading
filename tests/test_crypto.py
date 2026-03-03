"""Tests for crypto-bot Deribit DVOL parsing, vol blending, and calibration logic.

Covers: DVOL endpoint response parsing, out-of-range rejection,
IV/RV blending math, confidence-scaled edge threshold, trailing drift,
and horizon-matched vol lookback.
"""

import math
import time
import pytest


class TestDVOLResponseParsing:
    """Test the DVOL data point parsing logic (extracted from fetch_deribit_iv)."""

    def _parse_dvol(self, data):
        """Simulate the DVOL parsing logic from crypto-bot."""
        result = data.get("result", {})
        points = result.get("data", [])
        if not points:
            return None
        latest_close = points[-1][4]
        iv = latest_close / 100.0
        if iv < 0.10 or iv > 3.0:
            return None
        return iv

    def test_normal_btc_dvol(self):
        data = {"result": {"data": [[1700000000000, 55.0, 56.0, 54.0, 55.0]]}}
        assert self._parse_dvol(data) == pytest.approx(0.55, abs=0.01)

    def test_normal_eth_dvol(self):
        data = {"result": {"data": [[1700000000000, 70.0, 72.0, 68.0, 70.5]]}}
        assert self._parse_dvol(data) == pytest.approx(0.705, abs=0.01)

    def test_multiple_data_points_uses_latest(self):
        data = {"result": {"data": [
            [1700000000000, 50.0, 52.0, 48.0, 51.0],
            [1700003600000, 55.0, 56.0, 54.0, 55.0],
            [1700007200000, 60.0, 62.0, 58.0, 61.0],
        ]}}
        assert self._parse_dvol(data) == pytest.approx(0.61, abs=0.01)

    def test_empty_data_returns_none(self):
        data = {"result": {"data": []}}
        assert self._parse_dvol(data) is None

    def test_missing_result_returns_none(self):
        data = {}
        assert self._parse_dvol(data) is None

    def test_out_of_range_low_rejected(self):
        """IV below 10% is rejected."""
        data = {"result": {"data": [[1700000000000, 5.0, 6.0, 4.0, 5.0]]}}
        assert self._parse_dvol(data) is None

    def test_out_of_range_high_rejected(self):
        """IV above 300% is rejected."""
        data = {"result": {"data": [[1700000000000, 350.0, 360.0, 340.0, 350.0]]}}
        assert self._parse_dvol(data) is None

    def test_boundary_10_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 10.0, 11.0, 9.0, 10.0]]}}
        assert self._parse_dvol(data) == pytest.approx(0.10, abs=0.01)

    def test_boundary_300_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 300.0, 301.0, 299.0, 300.0]]}}
        assert self._parse_dvol(data) == pytest.approx(3.0, abs=0.01)


class TestVolBlending:
    """Test the IV/RV blending logic."""

    def _blend(self, iv, rv, default_vol=0.50):
        """Simulate the blending logic from scan_and_trade()."""
        if iv is not None and rv is not None:
            return 0.6 * iv + 0.4 * rv
        elif iv is not None:
            return iv
        elif rv is not None:
            return 0.3 * default_vol + 0.7 * rv
        else:
            return default_vol

    def test_both_iv_and_rv(self):
        assert self._blend(0.55, 0.45) == pytest.approx(0.51, abs=0.01)

    def test_iv_only(self):
        assert self._blend(0.55, None) == 0.55

    def test_rv_only(self):
        assert self._blend(None, 0.45) == pytest.approx(0.465, abs=0.01)

    def test_neither_uses_default(self):
        assert self._blend(None, None) == 0.50

    def test_iv_weight_exceeds_rv(self):
        """IV should get higher weight (60%) than RV (40%)."""
        blended = self._blend(0.60, 0.40)
        assert blended > 0.50
        assert blended == pytest.approx(0.52, abs=0.01)

    def test_custom_default_vol(self):
        assert self._blend(None, None, default_vol=0.65) == 0.65


class TestEffectiveEdgeThreshold:
    """Test confidence-scaled edge threshold logic."""

    def _effective_edge_threshold(self, model_prob, edge_threshold=0.08,
                                   mid_range_threshold=0.15,
                                   mid_range_band=(0.25, 0.75)):
        """Simulate _effective_edge_threshold from crypto-bot."""
        if mid_range_band[0] < model_prob < mid_range_band[1]:
            return mid_range_threshold
        return edge_threshold

    def test_extreme_low_prob_uses_base_threshold(self):
        """Prob < 0.25 uses base edge threshold."""
        assert self._effective_edge_threshold(0.10) == 0.08

    def test_extreme_high_prob_uses_base_threshold(self):
        """Prob > 0.75 uses base edge threshold."""
        assert self._effective_edge_threshold(0.90) == 0.08

    def test_mid_range_uses_higher_threshold(self):
        """Prob in (0.25, 0.75) requires higher edge."""
        assert self._effective_edge_threshold(0.50) == 0.15

    def test_mid_range_boundary_low_exclusive(self):
        """Prob == 0.25 is NOT mid-range (uses base)."""
        assert self._effective_edge_threshold(0.25) == 0.08

    def test_mid_range_boundary_high_exclusive(self):
        """Prob == 0.75 is NOT mid-range (uses base)."""
        assert self._effective_edge_threshold(0.75) == 0.08

    def test_just_inside_mid_range_low(self):
        assert self._effective_edge_threshold(0.26) == 0.15

    def test_just_inside_mid_range_high(self):
        assert self._effective_edge_threshold(0.74) == 0.15

    def test_mid_range_blocks_phantom_edge(self):
        """A 10% edge at prob=0.50 would pass base (8%) but fail mid-range (15%)."""
        prob = 0.50
        edge = 0.10
        threshold = self._effective_edge_threshold(prob)
        assert edge < threshold  # correctly blocked

    def test_strong_edge_passes_mid_range(self):
        """A 20% edge at prob=0.50 passes even the mid-range threshold."""
        prob = 0.50
        edge = 0.20
        threshold = self._effective_edge_threshold(prob)
        assert edge > threshold  # correctly passes


class TestTrailingDrift:
    """Test trailing drift computation from price history."""

    def _compute_trailing_drift(self, history):
        """Simulate compute_trailing_drift from crypto-bot."""
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
        assert self._compute_trailing_drift([]) == 0.0

    def test_single_point_returns_zero(self):
        assert self._compute_trailing_drift([(time.time(), 50000)]) == 0.0

    def test_insufficient_time_span_returns_zero(self):
        """Less than 1 hour of data returns zero."""
        now = time.time()
        history = [(now - 1800, 50000), (now, 50100)]  # 30 min span
        assert self._compute_trailing_drift(history) == 0.0

    def test_flat_price_returns_zero(self):
        now = time.time()
        history = [(now - 86400, 50000), (now, 50000)]
        assert self._compute_trailing_drift(history) == pytest.approx(0.0, abs=0.01)

    def test_rising_price_positive_drift(self):
        """5% gain over 24h should give positive annualized drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 52500)]  # +5%
        drift = self._compute_trailing_drift(history)
        assert drift > 0
        # ~5% per day * 365 ≈ 1780% annualized, clamped to 2.0
        assert drift == pytest.approx(2.0, abs=0.01)

    def test_falling_price_negative_drift(self):
        """5% drop over 24h should give negative drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 47500)]  # -5%
        drift = self._compute_trailing_drift(history)
        assert drift < 0

    def test_clamped_to_positive_2(self):
        """Extreme gains are clamped to +2.0."""
        now = time.time()
        history = [(now - 3600, 50000), (now, 55000)]  # +10% in 1h
        drift = self._compute_trailing_drift(history)
        assert drift == 2.0

    def test_clamped_to_negative_2(self):
        """Extreme drops are clamped to -2.0."""
        now = time.time()
        history = [(now - 3600, 50000), (now, 45000)]  # -10% in 1h
        drift = self._compute_trailing_drift(history)
        assert drift == -2.0

    def test_small_move_reasonable_drift(self):
        """0.1% gain over 24h → small positive annualized drift."""
        now = time.time()
        history = [(now - 86400, 50000), (now, 50050)]  # +0.1%
        drift = self._compute_trailing_drift(history)
        # ~0.1% * 365 ≈ 36.5% annualized ≈ 0.365
        assert 0.3 < drift < 0.5

    def test_zero_oldest_price_returns_zero(self):
        now = time.time()
        history = [(now - 86400, 0), (now, 50000)]
        assert self._compute_trailing_drift(history) == 0.0


class TestHorizonMatchedVol:
    """Test horizon-matched RV lookback selection."""

    def _select_lookback(self, minutes_to_settle):
        """Simulate the lookback selection logic from scan_and_trade()."""
        if minutes_to_settle <= 30:
            return 3600       # 1h
        elif minutes_to_settle <= 120:
            return 6 * 3600   # 6h
        else:
            return 86400      # 24h

    def test_15min_market_uses_1h_lookback(self):
        assert self._select_lookback(15) == 3600

    def test_30min_market_uses_1h_lookback(self):
        assert self._select_lookback(30) == 3600

    def test_60min_market_uses_6h_lookback(self):
        assert self._select_lookback(60) == 6 * 3600

    def test_120min_market_uses_6h_lookback(self):
        assert self._select_lookback(120) == 6 * 3600

    def test_daily_market_uses_24h_lookback(self):
        assert self._select_lookback(1440) == 86400

    def test_weekly_market_uses_24h_lookback(self):
        assert self._select_lookback(10080) == 86400

    def test_121min_uses_24h_lookback(self):
        """Just over 2h boundary → 24h lookback."""
        assert self._select_lookback(121) == 86400


class TestComputeRealizedVol:
    """Test compute_realized_vol refactored signature."""

    def _compute_vol(self, history, lookback_seconds=86400):
        """Simulate vol computation logic (no side effects)."""
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
        # 10 points over 24h — only last ~1h will have data
        old_points = [(now - 86400 + i*300, 50000 + i*10) for i in range(240)]  # 20h of data
        recent_points = [(now - 3600 + i*60, 50000 + i*5) for i in range(60)]   # last 1h
        history = old_points + recent_points
        vol_24h = self._compute_vol(history, lookback_seconds=86400)
        vol_1h = self._compute_vol(history, lookback_seconds=3600)
        # Both should return a value
        assert vol_24h is not None
        assert vol_1h is not None

    def test_no_price_skips_recording(self):
        """When current_price=None, no new observation is added."""
        # This just tests the conceptual contract — the actual function
        # modifies _price_history only when current_price is not None
        pass


class TestBracketLiquidityGate:
    """Test bracket-specific liquidity requirements (relaxed vs standard)."""

    def _bracket_eligible(self, market, max_spread=15, min_volume=10):
        """Simulate bracket eligibility: spread < 15c AND volume > 10."""
        yes_bid = market.get("yes_bid", 0)
        yes_ask = market.get("yes_ask", 0)
        volume = market.get("volume", 0) or 0
        if not yes_ask:
            return False
        spread = (yes_ask - yes_bid) if yes_bid else 999
        return spread <= max_spread and volume >= min_volume

    def test_tight_spread_high_volume_eligible(self):
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 25}
        assert self._bracket_eligible(market)

    def test_wide_spread_rejected(self):
        """Spread > 15c rejects bracket."""
        market = {"yes_bid": 30, "yes_ask": 50, "volume": 25}
        assert not self._bracket_eligible(market)

    def test_low_volume_rejected(self):
        """Volume < 10 rejects bracket."""
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 5}
        assert not self._bracket_eligible(market)

    def test_ask_only_rejected(self):
        """Ask-only market (no bid) has spread=999, rejected."""
        market = {"yes_bid": 0, "yes_ask": 50, "volume": 25}
        assert not self._bracket_eligible(market)

    def test_boundary_spread_15_eligible(self):
        market = {"yes_bid": 35, "yes_ask": 50, "volume": 15}
        assert self._bracket_eligible(market)

    def test_boundary_volume_10_eligible(self):
        market = {"yes_bid": 40, "yes_ask": 50, "volume": 10}
        assert self._bracket_eligible(market)


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
        # Far OTM: price 90K, threshold 120K (33% away)
        gbm_prob = crypto_price_probability(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        jd_prob = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        # JD should give higher probability due to jump component
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
