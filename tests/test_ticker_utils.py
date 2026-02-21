"""Tests for shared ticker parsing utilities."""

import pytest
from ticker_utils import parse_weather_ticker, parse_crypto_ticker


# ===================================================================
# Weather Ticker Parser
# ===================================================================

class TestParseWeatherTicker:

    def test_standard_threshold_ticker(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-T86")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "T"
        assert r["threshold"] == 86.0

    def test_bracket_ticker(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-B85.5")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "B"
        assert r["threshold"] == 85.5

    def test_different_city(self):
        r = parse_weather_ticker("KXHIGHLAX-05MAR26-T72")
        assert r is not None
        assert r["city"] == "LAX"
        assert r["date"] == "2026-03-05"
        assert r["threshold"] == 72.0

    def test_chicago(self):
        r = parse_weather_ticker("KXHIGHCHI-15JAN26-T30")
        assert r is not None
        assert r["city"] == "CHI"
        assert r["date"] == "2026-01-15"

    def test_december(self):
        r = parse_weather_ticker("KXHIGHNY-31DEC25-T45")
        assert r is not None
        assert r["date"] == "2025-12-31"

    def test_invalid_ticker_returns_none(self):
        assert parse_weather_ticker("KXBTC-21FEB26-T70000") is None

    def test_empty_string_returns_none(self):
        assert parse_weather_ticker("") is None

    def test_invalid_month_returns_none(self):
        assert parse_weather_ticker("KXHIGHMIA-21XYZ26-T86") is None

    def test_date_order_correct(self):
        """Verify day/month/year are parsed in correct order (regression test for weather-bot bug)."""
        # Ticker: KXHIGHMIA-26FEB16 means Feb 26, 2016 (day=26, month=FEB, year=16)
        # The old weather-bot bug swapped yr and day: yr=26, day=16 -> 2026-02-16
        # Correct: day=26, month=02, year=16 -> 2016-02-26
        r = parse_weather_ticker("KXHIGHMIA-26FEB16-T86")
        assert r is not None
        assert r["date"] == "2016-02-26"

    def test_another_date_order(self):
        # 15JAN25 -> day=15, month=JAN=01, year=25 -> 2025-01-15
        r = parse_weather_ticker("KXHIGHMIA-15JAN25-T80")
        assert r is not None
        assert r["date"] == "2025-01-15"


# ===================================================================
# Crypto Ticker Parser
# ===================================================================

class TestParseCryptoTicker:

    def test_btc_threshold(self):
        r = parse_crypto_ticker("KXBTC-21FEB26-T70000")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "T"
        assert r["threshold"] == 70000.0

    def test_eth_bracket(self):
        r = parse_crypto_ticker("KXETH-21FEB26-B3500")
        assert r is not None
        assert r["asset"] == "ETH"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "B"
        assert r["threshold"] == 3500.0

    def test_sol(self):
        r = parse_crypto_ticker("KXSOL-10MAR26-T200")
        assert r is not None
        assert r["asset"] == "SOL"
        assert r["threshold"] == 200.0

    def test_simple_format_fallback(self):
        r = parse_crypto_ticker("KXBTC-T95000")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["direction"] == "T"
        assert r["threshold"] == 95000.0
        assert "date" not in r

    def test_invalid_ticker_returns_none(self):
        assert parse_crypto_ticker("KXHIGHMIA-21FEB26-T86") is None

    def test_empty_string_returns_none(self):
        assert parse_crypto_ticker("") is None

    def test_invalid_month_returns_none(self):
        assert parse_crypto_ticker("KXBTC-21XYZ26-T70000") is None

    def test_crypto_prefix_variant(self):
        r = parse_crypto_ticker("KXCRYPTO-21FEB26-T50000")
        assert r is not None
        assert r["asset"] == "CRYPTO"
