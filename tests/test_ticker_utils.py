"""Tests for shared ticker parsing utilities."""

import pytest
from ticker_utils import parse_weather_ticker, parse_crypto_ticker


# ===================================================================
# Weather Ticker Parser
# ===================================================================

class TestParseWeatherTicker:

    # --- Current YYMONDD format (2026-02-22 onwards) ---

    def test_current_format_threshold(self):
        r = parse_weather_ticker("KXHIGHMIA-26FEB28-T86")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-28"
        assert r["direction"] == "T"
        assert r["threshold"] == 86.0

    def test_current_format_bracket(self):
        r = parse_weather_ticker("KXHIGHAUS-26MAR01-B87.5")
        assert r is not None
        assert r["city"] == "AUS"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "B"
        assert r["threshold"] == 87.5

    def test_current_format_different_cities(self):
        for ticker, city, date in [
            ("KXHIGHCHI-26MAR01-T36", "CHI", "2026-03-01"),
            ("KXHIGHDEN-26FEB28-T64", "DEN", "2026-02-28"),
            ("KXHIGHLAX-26MAR01-T78", "LAX", "2026-03-01"),
            ("KXHIGHNY-26FEB28-T50", "NY", "2026-02-28"),
            ("KXHIGHPHIL-26FEB28-B55.5", "PHIL", "2026-02-28"),
        ]:
            r = parse_weather_ticker(ticker)
            assert r is not None, f"Failed to parse: {ticker}"
            assert r["city"] == city
            assert r["date"] == date

    # --- T-prefix cities (KXHIGHT<CITY>) ---

    def test_t_prefix_city_atl(self):
        r = parse_weather_ticker("KXHIGHTATL-26MAR01-T72")
        assert r is not None
        assert r["city"] == "ATL"
        assert r["date"] == "2026-03-01"

    def test_t_prefix_city_bos(self):
        r = parse_weather_ticker("KXHIGHTBOS-26FEB28-B49.5")
        assert r is not None
        assert r["city"] == "BOS"
        assert r["date"] == "2026-02-28"
        assert r["direction"] == "B"

    def test_t_prefix_various_cities(self):
        for ticker, city in [
            ("KXHIGHTDAL-26MAR01-T80", "DAL"),
            ("KXHIGHTDC-26FEB28-T59", "DC"),
            ("KXHIGHTHOU-26FEB28-T73", "HOU"),
            ("KXHIGHTLV-26MAR01-B65.5", "LV"),
            ("KXHIGHTMIN-26FEB28-T30", "MIN"),
            ("KXHIGHTNOLA-26MAR01-T72", "NOLA"),
            ("KXHIGHTOKC-26FEB28-T60", "OKC"),
            ("KXHIGHTPHX-26MAR01-T90", "PHX"),
            ("KXHIGHTSATX-26FEB28-B75.5", "SATX"),
            ("KXHIGHTSEA-26FEB28-T50", "SEA"),
            ("KXHIGHTSFO-26MAR01-T65", "SFO"),
        ]:
            r = parse_weather_ticker(ticker)
            assert r is not None, f"Failed to parse: {ticker}"
            assert r["city"] == city

    # --- Old DDMONYY format (backward compatibility for trade log parsing) ---

    def test_old_format_standard(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-T86")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "T"
        assert r["threshold"] == 86.0

    def test_old_format_bracket(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-B85.5")
        assert r is not None
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "B"
        assert r["threshold"] == 85.5

    def test_old_format_different_city(self):
        r = parse_weather_ticker("KXHIGHLAX-05MAR26-T72")
        assert r is not None
        assert r["city"] == "LAX"
        assert r["date"] == "2026-03-05"
        assert r["threshold"] == 72.0

    def test_old_format_chicago(self):
        r = parse_weather_ticker("KXHIGHCHI-15JAN26-T30")
        assert r is not None
        assert r["city"] == "CHI"
        assert r["date"] == "2026-01-15"

    # --- YYMONDD format heuristic ---

    def test_yymondd_year26(self):
        """26FEB16: g2=26 >= 25 -> YYMONDD -> year=26, day=16 -> 2026-02-16."""
        r = parse_weather_ticker("KXHIGHMIA-26FEB16-T86")
        assert r is not None
        assert r["date"] == "2026-02-16"

    def test_yymondd_year27(self):
        r = parse_weather_ticker("KXHIGHMIA-27JAN15-T80")
        assert r is not None
        assert r["date"] == "2027-01-15"

    # --- Invalid tickers ---

    def test_invalid_ticker_returns_none(self):
        assert parse_weather_ticker("KXBTC-21FEB26-T70000") is None

    def test_empty_string_returns_none(self):
        assert parse_weather_ticker("") is None

    def test_invalid_month_returns_none(self):
        assert parse_weather_ticker("KXHIGHMIA-21XYZ26-T86") is None

    def test_inflation_ticker_returns_none(self):
        assert parse_weather_ticker("KXHIGHINFLATION-26DEC-T3.5") is None


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
