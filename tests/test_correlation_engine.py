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
