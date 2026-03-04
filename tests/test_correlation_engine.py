"""Tests for correlation_engine.py — portfolio risk modeling."""

import json
import math
import os
import tempfile
import pytest
from unittest.mock import MagicMock


class TestFactorMapping:
    """Test ticker -> factor group mapping."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine
        self.engine = CorrelationEngine()

    def test_cpi_tickers(self):
        assert self.engine.ticker_to_factor("KXCPI-26MAY-T20") == "CPI"
        assert self.engine.ticker_to_factor("KXCPI-26MAY-T21") == "CPI"

    def test_core_cpi_tickers(self):
        assert self.engine.ticker_to_factor("KXCORECPI-26JUN-T23") == "CORE_CPI"

    def test_gdp_tickers(self):
        assert self.engine.ticker_to_factor("KXGDP-26Q1-T20") == "GDP"

    def test_jobs_tickers(self):
        assert self.engine.ticker_to_factor("KXJOBS-26MAR-T200K") == "JOBS"

    def test_btc_tickers(self):
        assert self.engine.ticker_to_factor("KXBTC-26MAR3-T95000") == "BTC"

    def test_eth_tickers(self):
        assert self.engine.ticker_to_factor("KXETH-26MAR3-T3500") == "ETH"

    def test_weather_south_tx(self):
        assert self.engine.ticker_to_factor("KXHIGHHOU-26MAR3-T86") == "WEATHER_SOUTH_TX"
        assert self.engine.ticker_to_factor("KXHIGHAUS-26MAR3-T75") == "WEATHER_SOUTH_TX"

    def test_weather_northeast(self):
        assert self.engine.ticker_to_factor("KXHIGHNY-26MAR3-T55") == "WEATHER_NE"
        assert self.engine.ticker_to_factor("KXHIGHPHI-26MAR3-T50") == "WEATHER_NE"  # PHI = Philadelphia

    def test_weather_southeast(self):
        assert self.engine.ticker_to_factor("KXHIGHMIA-26MAR3-T85") == "WEATHER_SE"

    def test_weather_west(self):
        assert self.engine.ticker_to_factor("KXHIGHLAX-26MAR3-T75") == "WEATHER_W"

    def test_weather_midwest(self):
        assert self.engine.ticker_to_factor("KXHIGHCHI-26MAR3-T50") == "WEATHER_MW"

    def test_weather_mountain(self):
        assert self.engine.ticker_to_factor("KXHIGHDEN-26MAR3-T60") == "WEATHER_MT"

    def test_album_tickers(self):
        assert self.engine.ticker_to_factor("KXALBUM-ARTIST-T100K") == "ALBUM"

    def test_boxoffice_tickers(self):
        assert self.engine.ticker_to_factor("KXBOX-MOVIE-T50M") == "BOX_OFFICE"
        assert self.engine.ticker_to_factor("KXMOVIE-FILM-T100M") == "BOX_OFFICE"

    def test_unknown_ticker_returns_ticker_prefix(self):
        """Unknown tickers should return the prefix as a unique factor."""
        factor = self.engine.ticker_to_factor("KXNEWMARKET-26MAR-T50")
        assert factor == "KXNEWMARKET"

    def test_same_factor_means_correlated(self):
        """Two CPI tickers should have the same factor (= high correlation)."""
        f1 = self.engine.ticker_to_factor("KXCPI-26MAY-T20")
        f2 = self.engine.ticker_to_factor("KXCPI-26MAY-T21")
        assert f1 == f2

    def test_different_factor_means_independent(self):
        """CPI and BTC should have different factors."""
        f1 = self.engine.ticker_to_factor("KXCPI-26MAY-T20")
        f2 = self.engine.ticker_to_factor("KXBTC-26MAR3-T95000")
        assert f1 != f2


class TestTickerCorrelation:
    """Test ticker-level correlation lookup."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine
        self.engine = CorrelationEngine()

    def test_same_ticker_correlation_is_one(self):
        """A ticker is perfectly correlated with itself."""
        corr = self.engine.get_ticker_correlation("KXCPI-26MAY-T20", "KXCPI-26MAY-T20")
        assert corr == 1.0

    def test_same_factor_different_tickers_is_intra(self):
        """Two different tickers in same factor get intra-factor (0.90)."""
        corr = self.engine.get_ticker_correlation("KXCPI-26MAY-T20", "KXCPI-26MAY-T21")
        assert corr == 0.90


class TestClusterRisk:
    """Test cluster risk tracking and concentration limits."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(cluster_max_fraction=0.15))

    def test_record_trade_adds_to_cluster(self):
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        cluster_risk = self.engine.get_cluster_risk("CPI")
        assert cluster_risk == 50000

    def test_same_cluster_accumulates(self):
        """Two CPI trades should accumulate in the same cluster."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.record_trade("KXCPI-26MAY-T21", risk_cents=30000)
        assert self.engine.get_cluster_risk("CPI") == 80000

    def test_different_clusters_independent(self):
        """CPI and BTC trades should be in separate clusters."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.record_trade("KXBTC-26MAR3-T95000", risk_cents=20000)
        assert self.engine.get_cluster_risk("CPI") == 50000
        assert self.engine.get_cluster_risk("BTC") == 20000

    def test_check_cluster_limit_allows(self):
        """Under limit: $500 CPI risk with $5000 balance -> 10% < 15% limit."""
        allowed, reason = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T22", proposed_risk_cents=10000, available_balance_cents=500000
        )
        assert allowed

    def test_check_cluster_limit_blocks(self):
        """Over limit: $800 CPI risk with $5000 balance -> 16% > 15% limit."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=70000)
        allowed, reason = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=10000, available_balance_cents=500000
        )
        assert not allowed
        assert "cluster" in reason.lower()

    def test_cluster_limit_prevents_cpi_concentration(self):
        """The CPI blowup scenario: $1720 in CPI on $5090 balance = 33.8%.
        Should have been blocked after ~$763 (15% of $5090)."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=76000)
        allowed, _ = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=1000, available_balance_cents=509000
        )
        assert not allowed

    def test_correlated_factors_share_cluster(self):
        """CPI and CORE_CPI should share risk since they are correlated > 0.70."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=40000)
        self.engine.record_trade("KXCORECPI-26JUN-T23", risk_cents=40000)
        # If combined exposure is checked, 80k on 500k = 16% > 15%
        allowed, _ = self.engine.check_cluster_limit(
            "KXCPI-26MAY-T21", proposed_risk_cents=1000, available_balance_cents=500000
        )
        assert not allowed

    def test_weather_region_clusters(self):
        """Houston and Austin weather should share WEATHER_SOUTH_TX cluster."""
        self.engine.record_trade("KXHIGHHOU-26MAR3-T86", risk_cents=30000)
        self.engine.record_trade("KXHIGHAUS-26MAR3-T75", risk_cents=30000)
        assert self.engine.get_cluster_risk("WEATHER_SOUTH_TX") == 60000

    def test_reset_daily(self):
        """Daily reset should clear all cluster risk."""
        self.engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        self.engine.reset_daily()
        assert self.engine.get_cluster_risk("CPI") == 0


class TestPortfolioVaR:
    """Test portfolio VaR computation."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(var_confidence=0.99))

    def test_single_position_var(self):
        """VaR of a single position = its max loss * loss probability."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.10},
        ]
        var = self.engine.compute_portfolio_var(positions)
        assert var > 0
        assert var <= 50000  # VaR cannot exceed max loss

    def test_uncorrelated_positions_diversify(self):
        """Two uncorrelated positions should have lower VaR than sum."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var = self.engine.compute_portfolio_var(positions)
        # VaR should be less than 60000 (sum) due to diversification
        assert var < 60000

    def test_correlated_positions_no_diversification(self):
        """Two perfectly correlated positions: VaR ~ sum of individual VaRs."""
        positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXCPI-26MAY-T21", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var_correlated = self.engine.compute_portfolio_var(positions)

        uncorr_positions = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
            {"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        var_uncorr = self.engine.compute_portfolio_var(uncorr_positions)

        # Correlated VaR should be higher than uncorrelated VaR
        assert var_correlated > var_uncorr

    def test_empty_portfolio_var_zero(self):
        var = self.engine.compute_portfolio_var([])
        assert var == 0.0

    def test_var_increases_with_position_size(self):
        """Doubling position size should increase VaR."""
        pos_small = [{"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 10000, "loss_prob": 0.30}]
        pos_large = [{"ticker": "KXBTC-26MAR3-T95000", "risk_cents": 20000, "loss_prob": 0.30}]
        assert self.engine.compute_portfolio_var(pos_large) > self.engine.compute_portfolio_var(pos_small)


class TestMarginalVaR:
    """Test marginal VaR threshold check."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(
            marginal_var_limit_fraction=0.05
        ))

    def test_first_trade_allowed(self):
        """First trade should always be allowed (marginal VaR from 0)."""
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXBTC-26MAR3-T95000",
            proposed_risk_cents=5000,
            loss_prob=0.30,
            current_positions=[],
            available_balance_cents=500000,
        )
        assert allowed

    def test_large_correlated_trade_blocked(self):
        """Adding a large correlated position should be blocked."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXCPI-26MAY-T21",
            proposed_risk_cents=50000,
            loss_prob=0.20,
            current_positions=existing,
            available_balance_cents=500000,
        )
        assert not allowed

    def test_small_uncorrelated_trade_allowed(self):
        """A small uncorrelated position should be allowed."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 30000, "loss_prob": 0.20},
        ]
        allowed, reason = self.engine.check_marginal_var(
            ticker="KXBTC-26MAR3-T95000",
            proposed_risk_cents=5000,
            loss_prob=0.30,
            current_positions=existing,
            available_balance_cents=500000,
        )
        assert allowed


class TestTailDependence:
    """Test tail dependence computation and Kelly reduction."""

    def setup_method(self):
        from correlation_engine import CorrelationEngine, CorrelationConfig
        self.engine = CorrelationEngine(config=CorrelationConfig(
            tail_dep_kelly_threshold=0.15,
            tail_dep_kelly_cut=0.25,
            copula_df=5,
        ))

    def test_high_correlation_high_tail_dep(self):
        """Intra-factor correlation (0.90) should have high tail dependence."""
        td = self.engine.compute_tail_dependence("CPI", "CPI")
        assert td > 0.15  # Should trigger Kelly reduction

    def test_zero_correlation_zero_tail_dep(self):
        """Uncorrelated factors should have ~0 tail dependence."""
        td = self.engine.compute_tail_dependence("CPI", "BTC")
        assert td < 0.05

    def test_moderate_correlation_moderate_tail_dep(self):
        """BTC/ETH at 0.75 correlation should have moderate tail dependence."""
        td = self.engine.compute_tail_dependence("BTC", "ETH")
        assert 0.05 < td < 0.50

    def test_kelly_multiplier_no_existing_positions(self):
        """No existing positions -> full Kelly (multiplier = 1.0)."""
        mult = self.engine.get_tail_risk_multiplier("KXBTC-26MAR3-T95000", [])
        assert mult == 1.0

    def test_kelly_multiplier_with_correlated_position(self):
        """Adding to a factor with high tail dep -> 75% Kelly."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        mult = self.engine.get_tail_risk_multiplier("KXCPI-26MAY-T21", existing)
        assert mult == pytest.approx(1.0 - 0.25, rel=0.01)  # 0.75

    def test_kelly_multiplier_with_uncorrelated_position(self):
        """Adding to uncorrelated factor -> full Kelly."""
        existing = [
            {"ticker": "KXCPI-26MAY-T20", "risk_cents": 50000, "loss_prob": 0.20},
        ]
        mult = self.engine.get_tail_risk_multiplier("KXBTC-26MAR3-T95000", existing)
        assert mult == 1.0

    def test_tail_dep_formula_known_values(self):
        """Verify tail dependence formula against known analytical values.

        For nu=5, rho=0.9:
        lambda = 2 * t_6(-sqrt(6 * 0.1 / 1.9)) ~ substantial
        """
        td = self.engine.compute_tail_dependence("CPI", "CPI")  # rho=0.90
        assert td > 0.30  # Known to be substantial for high corr + low df


class TestStatePersistence:
    """Test save/load of correlation engine state."""

    def test_save_and_load_cluster_risk(self):
        from correlation_engine import CorrelationEngine
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = f.name

        engine1 = CorrelationEngine(state_path=state_path)
        engine1.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        engine1.record_trade("KXBTC-26MAR3-T95000", risk_cents=20000)
        engine1.save_state()

        engine2 = CorrelationEngine(state_path=state_path)
        engine2.load_state()
        assert engine2.get_cluster_risk("CPI") == 50000
        assert engine2.get_cluster_risk("BTC") == 20000
        os.unlink(state_path)

    def test_load_missing_file_is_noop(self):
        from correlation_engine import CorrelationEngine
        engine = CorrelationEngine(state_path="/tmp/nonexistent_corr.json")
        engine.load_state()  # Should not raise
        assert engine.get_cluster_risk("CPI") == 0

    def test_save_without_path_is_noop(self):
        from correlation_engine import CorrelationEngine
        engine = CorrelationEngine(state_path=None)
        engine.record_trade("KXCPI-26MAY-T20", risk_cents=50000)
        engine.save_state()  # Should not raise
