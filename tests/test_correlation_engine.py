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
