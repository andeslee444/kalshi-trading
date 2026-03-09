"""Tests for GARCH(1,1), DCC correlation, and intraday seasonality."""
import math
import time
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "kalshi"))


class TestGARCH:
    """GARCH(1,1) volatility model."""

    def test_garch_forecast_after_updates(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed 20 returns
        for r in [0.01, -0.02, 0.015, -0.01, 0.03, -0.025, 0.005, -0.005,
                   0.02, -0.015, 0.01, -0.01, 0.025, -0.02, 0.01, -0.015,
                   0.02, -0.01, 0.005, -0.005]:
            garch.update(r)
        vol = garch.forecast_vol()
        assert vol is not None
        assert 0.01 < vol < 2.0  # reasonable annualized range

    def test_garch_vol_increases_after_shock(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed calm returns
        for _ in range(20):
            garch.update(0.005)
        calm_vol = garch.forecast_vol()
        # Feed a shock
        garch.update(0.15)  # 15% return (huge)
        shock_vol = garch.forecast_vol()
        assert shock_vol > calm_vol

    def test_garch_mean_reverts(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed high vol
        for _ in range(30):
            garch.update(0.10)
        high_vol = garch.forecast_vol()
        # Feed calm
        for _ in range(30):
            garch.update(0.005)
        calm_vol = garch.forecast_vol()
        assert calm_vol < high_vol

    def test_garch_insufficient_data(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        garch.update(0.01)
        assert garch.forecast_vol() is None

    def test_garch_vol_of_vol(self):
        """GARCH should estimate vol-of-vol for Heston param."""
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        for r in [0.01, -0.02, 0.015, -0.01, 0.03, -0.025, 0.005, -0.005,
                   0.02, -0.015, 0.01, -0.01, 0.025, -0.02, 0.01, -0.015,
                   0.02, -0.01, 0.005, -0.005]:
            garch.update(r)
        vov = garch.vol_of_vol()
        assert vov is not None
        assert vov > 0


class TestDCC:
    """Dynamic Conditional Correlation."""

    def test_dcc_returns_matrix(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        # Feed correlated returns
        for _ in range(30):
            btc_r = 0.01
            eth_r = 0.012  # correlated
            dcc.update({"BTC": btc_r, "ETH": eth_r})
        corr = dcc.correlation_matrix()
        assert corr is not None
        assert corr.shape == (2, 2)
        # Diagonal = 1
        assert abs(corr[0, 0] - 1.0) < 0.01
        assert abs(corr[1, 1] - 1.0) < 0.01

    def test_dcc_correlated_assets(self):
        from vol_forecaster import DCCCorrelation
        import random
        random.seed(42)
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        for _ in range(100):
            base = random.gauss(0, 0.02)
            dcc.update({"BTC": base + random.gauss(0, 0.005),
                        "ETH": base * 0.8 + random.gauss(0, 0.008)})
        corr = dcc.correlation_matrix()
        # BTC-ETH should be positively correlated
        assert corr[0, 1] > 0.3

    def test_dcc_pair_correlation(self):
        from vol_forecaster import DCCCorrelation
        import random
        random.seed(42)
        dcc = DCCCorrelation(assets=["BTC", "ETH", "SOL"])
        for _ in range(50):
            base = random.gauss(0, 0.02)
            dcc.update({"BTC": base, "ETH": base * 0.8, "SOL": base * 0.5})
        rho = dcc.pair_correlation("BTC", "ETH")
        assert rho is not None
        assert rho > 0

    def test_dcc_insufficient_data(self):
        from vol_forecaster import DCCCorrelation
        dcc = DCCCorrelation(assets=["BTC", "ETH"])
        dcc.update({"BTC": 0.01, "ETH": 0.012})
        assert dcc.correlation_matrix() is None


class TestIntradaySeasonality:
    """Intraday vol adjustment."""

    def test_us_open_higher_vol(self):
        from vol_forecaster import intraday_vol_multiplier
        # US market open: 14:30 UTC (9:30 ET)
        us_open = intraday_vol_multiplier(hour_utc=14, day_of_week=1)  # Tuesday
        # Quiet period: 06:00 UTC (1am ET)
        quiet = intraday_vol_multiplier(hour_utc=6, day_of_week=1)
        assert us_open > quiet

    def test_weekend_lower_vol(self):
        from vol_forecaster import intraday_vol_multiplier
        weekday = intraday_vol_multiplier(hour_utc=15, day_of_week=2)  # Wednesday
        weekend = intraday_vol_multiplier(hour_utc=15, day_of_week=6)  # Sunday
        assert weekend < weekday

    def test_multiplier_in_range(self):
        from vol_forecaster import intraday_vol_multiplier
        for hour in range(24):
            for day in range(7):
                mult = intraday_vol_multiplier(hour_utc=hour, day_of_week=day)
                assert 0.5 <= mult <= 1.5

    def test_asia_open_elevated(self):
        from vol_forecaster import intraday_vol_multiplier
        # Asia open: ~00:00-01:00 UTC (8-9pm ET, 9am JST)
        asia = intraday_vol_multiplier(hour_utc=0, day_of_week=1)
        quiet = intraday_vol_multiplier(hour_utc=6, day_of_week=1)
        assert asia >= quiet


class TestBidAskBounce:
    """Microstructure: bid-ask bounce correction."""

    def test_bounce_reduces_rv(self):
        from vol_forecaster import correct_bid_ask_bounce
        raw_rv = 0.50
        corrected = correct_bid_ask_bounce(raw_rv, spread_cents=5, n_observations=100)
        assert corrected < raw_rv

    def test_zero_spread_no_correction(self):
        from vol_forecaster import correct_bid_ask_bounce
        raw_rv = 0.50
        corrected = correct_bid_ask_bounce(raw_rv, spread_cents=0, n_observations=100)
        assert abs(corrected - raw_rv) < 0.001

    def test_correction_never_negative(self):
        from vol_forecaster import correct_bid_ask_bounce
        corrected = correct_bid_ask_bounce(0.01, spread_cents=50, n_observations=5)
        assert corrected >= 0


class TestGARCHInitialization:
    """GARCH should not double-count the initializing return."""

    def test_first_forecast_not_biased_by_last_return(self):
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster(min_observations=5)
        # Feed 4 calm returns, then 1 huge return
        for _ in range(4):
            garch.update(0.001)
        # 5th return triggers init — should NOT immediately apply GARCH recursion
        garch.update(0.001)
        vol_after_init = garch.forecast_vol()
        # Now feed a shock
        garch.update(0.15)
        vol_after_shock = garch.forecast_vol()
        # The shock should increase vol (not be double-counted with init)
        assert vol_after_shock > vol_after_init
        # And init vol should be based on sample variance of the 5 calm returns
        assert vol_after_init < 0.01  # sqrt(~0.001^2) is tiny


class TestDCCWarmup:
    """DCC should skip updates until GARCH is warmed up."""

    def test_early_updates_dont_corrupt_q_matrix(self):
        from vol_forecaster import DCCCorrelation
        import numpy as np
        dcc = DCCCorrelation(assets=["BTC", "ETH"], min_observations=20)
        # Feed 5 returns (GARCH needs 10 to warm up)
        for i in range(5):
            dcc.update({"BTC": 0.01, "ETH": 0.012})
        # Q matrix should still be identity (no real updates applied)
        assert np.allclose(dcc._Q, np.eye(2), atol=0.1)

    def test_produces_valid_corr_after_full_warmup(self):
        from vol_forecaster import DCCCorrelation
        import random
        random.seed(42)
        dcc = DCCCorrelation(assets=["BTC", "ETH"], min_observations=20)
        # Feed 50 correlated returns (enough for both GARCH + DCC warmup)
        for _ in range(50):
            base = random.gauss(0, 0.02)
            dcc.update({"BTC": base + random.gauss(0, 0.005),
                        "ETH": base * 0.8 + random.gauss(0, 0.008)})
        corr = dcc.correlation_matrix()
        assert corr is not None
        assert corr[0, 1] > 0.2  # should detect positive correlation


class TestGARCHVolCeiling:
    """GARCH vol should be bounded after extreme events (flash crashes)."""

    def test_garch_bounded_after_extreme_return(self):
        """GARCH vol should be bounded even after a 10% flash crash."""
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        # Feed 100 calm returns then a 10% crash
        for _ in range(100):
            garch.update(0.001, interval_seconds=300)
        garch.update(-0.10, interval_seconds=300)  # 10% crash
        vol = garch.forecast_vol(use_actual_interval=True)
        assert vol is not None
        assert vol <= 2.0, f"GARCH vol after crash should be bounded at 200%, got {vol*100:.0f}%"
        assert vol > 0.10, f"GARCH vol after crash should still reflect elevated risk, got {vol*100:.0f}%"

    def test_garch_bounded_after_multiple_shocks(self):
        """Multiple consecutive shocks should not cause vol explosion."""
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        for _ in range(15):
            garch.update(0.001, interval_seconds=300)
        # Multiple large shocks
        for _ in range(5):
            garch.update(-0.08, interval_seconds=300)
        vol = garch.forecast_vol(use_actual_interval=True)
        assert vol is not None
        assert vol <= 2.0, f"GARCH vol should be capped at 200%, got {vol*100:.0f}%"

    def test_garch_ceiling_allows_elevated_vol(self):
        """Vol should be elevated after a shock, just not exploded."""
        from vol_forecaster import GARCHForecaster
        garch = GARCHForecaster()
        for _ in range(20):
            garch.update(0.001, interval_seconds=300)
        calm_vol = garch.forecast_vol(use_actual_interval=True)
        garch.update(-0.05, interval_seconds=300)
        shock_vol = garch.forecast_vol(use_actual_interval=True)
        assert shock_vol > calm_vol, "Shock should increase vol"
        assert shock_vol <= 2.0, f"But bounded at 200%, got {shock_vol*100:.0f}%"

    def test_garch_max_annualized_vol_constant(self):
        """MAX_ANNUALIZED_VOL should be 2.0 (200%)."""
        from vol_forecaster import GARCHForecaster
        assert GARCHForecaster.MAX_ANNUALIZED_VOL == 2.0
